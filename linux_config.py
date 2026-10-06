# linux_config.py
# Linux 网口配置（GUI 调用）。
# 与 Windows 版保持一致的接口：apply_config_windows / apply_config_linux(iface, cfg, log_target)
# 第三个参数既可以是 log(msg) 可调用对象，也可以直接传 Tk Text 控件（兼容旧调用）。

import subprocess

from nic_driver import as_log, normalize_mac


def _run(cmd, timeout=20):
    """执行命令，返回 (returncode, stdout, stderr)。"""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return proc.returncode, out, err
    except FileNotFoundError as exc:
        return 127, "", "命令不存在: %s" % exc
    except subprocess.TimeoutExpired:
        return 1, "", "命令超时: %s" % " ".join(cmd)
    except Exception as exc:
        return 1, "", "执行异常: %s" % exc


def _run_logged(cmd, log, on_error=None, timeout=20):
    rc, out, err = _run(cmd, timeout=timeout)
    if rc != 0 and on_error:
        log("   ⚠️ %s（%s）" % (on_error, err or out or "返回码 %s" % rc))
    return rc == 0


def mask_to_cidr(mask):
    """点分掩码 -> 前缀长度；非法返回 None。"""
    try:
        parts = [int(x) for x in str(mask).strip().split(".")]
    except Exception:
        return None
    if len(parts) != 4 or any(not 0 <= p <= 255 for p in parts):
        return None
    bits = "".join(bin(p)[2:].zfill(8) for p in parts)
    if "01" in bits:  # 掩码必须左侧连续为 1
        return None
    return bits.count("1")


def _prefix(value, log):
    """把 '24' 或 '255.255.255.0' 统一成前缀长度。"""
    text = str(value).strip()
    if text.isdigit():
        return text
    cidr = mask_to_cidr(text)
    if cidr is None:
        log("   ⚠️ 无法识别的子网掩码 %s，按 /24 处理" % text)
        return "24"
    return str(cidr)


def apply_config_linux(iface, cfg, log_target):
    """按 cfg 配置 Linux 网口（IP / VLAN / 网关）。"""
    log = as_log(log_target)
    cfg = cfg or {}
    mode = str(cfg.get("mode", "test")).strip().lower()

    log("=" * 56)
    log("开始配置网口: %s ｜ 模式: %s" % (iface, mode))

    vlan_id = cfg.get("vlan_id", "auto")
    vlan_iface = None
    if mode != "default" and vlan_id is not None and str(vlan_id).strip().lower() != "auto":
        vlan_iface = "%s.%s" % (iface, vlan_id)
        if _run(["ip", "link", "show", vlan_iface])[0] != 0:
            _run_logged(["ip", "link", "add", "link", iface, "name", vlan_iface,
                         "type", "vlan", "id", str(vlan_id)], log,
                        on_error="创建 VLAN 子接口失败")
        _run_logged(["ip", "link", "set", vlan_iface, "up"], log,
                    on_error="启用 VLAN 子接口失败")
        log("ℹ️ 已使用 VLAN 子接口 %s" % vlan_iface)

    target = vlan_iface or iface
    _run_logged(["ip", "addr", "flush", "dev", target], log, on_error="清空旧地址失败")
    _run_logged(["ip", "link", "set", target, "up"], log, on_error="启用网口失败")

    if mode == "default":
        log("✅ 已恢复默认（地址已清空，请通过 dhclient / NetworkManager 获取地址）")
        return

    ip_cfg = cfg.get("ip", "auto")
    netmask = cfg.get("netmask", "auto")
    if ip_cfg and str(ip_cfg).strip().lower() != "auto" and str(netmask).strip().lower() != "auto":
        prefix = _prefix(netmask, log)
        if _run_logged(["ip", "addr", "add", "%s/%s" % (ip_cfg, prefix), "dev", target],
                       log, on_error="设置 IP 失败"):
            log("✅ IP 已设置为 %s/%s" % (ip_cfg, prefix))
    else:
        log("ℹ️ IP 为 auto，保持系统当前设置")

    gateway = cfg.get("gateway", "auto")
    if gateway and str(gateway).strip().lower() != "auto":
        _run(["ip", "route", "del", "default"])
        if _run_logged(["ip", "route", "add", "default", "via", str(gateway), "dev", target],
                       log, on_error="设置默认网关失败"):
            log("✅ 默认网关已设置为 %s" % gateway)

    mac_cfg = cfg.get("mac", "auto")
    if mac_cfg and str(mac_cfg).strip().lower() != "auto":
        mac_norm = normalize_mac(mac_cfg)
        if mac_norm is None:
            log("⚠️ MAC 格式不正确，已跳过 MAC 设置")
        else:
            mac_colon = ":".join(mac_norm[i:i + 2] for i in range(0, 12, 2))
            _run_logged(["ip", "link", "set", "dev", iface, "address", mac_colon], log,
                        on_error="设置 MAC 失败")
            log("ℹ️ 已尝试设置 MAC 为 %s（需重启网口生效）" % mac_colon)

    log("✅ 已完成 %s 的配置" % target)
