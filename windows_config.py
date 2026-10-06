# windows_config.py
# Windows 网卡配置（GUI 调用）：IP / VLAN / MAC / ARP
#
# 相比旧版的关键改动
#   1. MAC、VLAN 开关、VLAN ID 一律按 RegistryKeyword + RegistryValue 读写，
#      不再用"显示名列表"匹配驱动属性（显示名随厂商和系统语言变化，必然漏网）。
#   2. 恢复默认改用 Reset-NetAdapterAdvancedProperty，不再猜 "--" 这类魔法值。
#   3. 所有属性写入合并成一次 PowerShell 调用；本文件自身只负责编排与日志。
#   4. MAC / VLAN 写入成功后自动重启网卡（可用配置项 "restart_adapter": false 关闭）。
#   5. 修复旧版 Default 模式里拼出 "-DisplayValue 'XXX Disable'" 的死循环逻辑。
#
# 入口：apply_config_windows(iface, cfg, log_target)
#       log_target 可以是 log(msg) 可调用对象，也可以直接传 Tk Text 控件（兼容旧调用）。

import ctypes
import re
import subprocess
import time

import nic_driver
from nic_driver import (
    ROLE_LABEL,
    AdapterInspector,
    apply_property_ops,
    as_log,
    choose_vlan_mode_value,
    mac_warning,
    normalize_mac,
)
from nic_driver import restart_adapter as _restart_adapter_ps


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def _creation_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _oem_encoding():
    """netsh/arp 等控制台程序输出用的是 OEM 代码页（中文系统 CP936），不是 UTF-8。"""
    try:
        return "cp%d" % ctypes.windll.kernel32.GetOEMCP()
    except Exception:
        return "utf-8"


def run(cmd, timeout=None):
    """执行外部命令，返回 (returncode, stdout, stderr)；输出按系统编码安全解码。"""
    cmd_list = cmd.split() if isinstance(cmd, str) else list(cmd)
    try:
        proc = subprocess.Popen(
            cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_creation_flags(),
        )
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return 1, "", "命令超时: %s" % " ".join(cmd_list)
    except Exception as exc:
        return 1, "", "run() 异常: %s" % exc

    def _decode(raw):
        if not isinstance(raw, (bytes, bytearray)):
            return str(raw or "")
        for enc in (_oem_encoding(), "utf-8", "latin-1"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", errors="replace")

    return proc.returncode, _decode(out_b).strip(), _decode(err_b).strip()


def cidr_to_netmask(cidr):
    """把 0~32 的前缀长度转成点分掩码；非法返回 None。"""
    bits = nic_driver._to_int(cidr)
    if bits is None or not 0 <= bits <= 32:
        return None
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return ".".join(str((mask >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def normalize_netmask(value):
    """支持 '24'、'255.255.255.0' 两种写法；'auto' 原样返回。"""
    if value is None or str(value).strip().lower() == "auto":
        return "auto"
    text = str(value).strip()
    if "." in text:
        return text
    return cidr_to_netmask(text) or text


# ----------------------------------------------------------------------------
# 接口索引与 ARP
# ----------------------------------------------------------------------------

def get_iface_index(iface_name):
    """
    取接口索引号。优先用 PowerShell（UTF-8 JSON，中文网卡名不会乱码），
    失败时退回 netsh 并按 OEM 代码页解码。
    """
    script = r"""
$name = __NAME__
try {
    $idx = (Get-NetAdapter -Name $name -ErrorAction Stop).ifIndex
    @{ ok = $true; index = [int]$idx } | ConvertTo-Json -Compress
} catch {
    @{ ok = $false; error = $_.Exception.Message } | ConvertTo-Json -Compress
}
""".replace("__NAME__", nic_driver._ps_str(iface_name))
    data = nic_driver.run_ps_json(script, timeout=30)
    if data.get("ok") and data.get("index") is not None:
        return str(data["index"])

    rc, out, err = run(["netsh", "interface", "ipv4", "show", "interfaces"])
    if rc == 0:
        for line in out.splitlines():
            match = re.match(r"^\s*(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$", line)
            if match and match.group(2).strip().lower() == str(iface_name).strip().lower():
                return match.group(1)
    return None


def add_static_arp(ip, mac, log_target, iface_name=None):
    """为指定网口添加静态 ARP 条目（先删旧条目再添加）。"""
    log = as_log(log_target)
    if not iface_name:
        log("⚠️ 添加静态 ARP 失败: 未提供网口名")
        return False
    if not ip or not mac:
        log("⚠️ 添加静态 ARP 失败: IP 或 MAC 为空 (%s -> %s)" % (ip, mac))
        return False

    index = get_iface_index(iface_name)
    if not index:
        log("⚠️ 添加静态 ARP 失败: 未找到网口 %s 的接口索引" % iface_name)
        return False

    mac_win = str(mac).replace(":", "-").replace(".", "-")
    run(["netsh", "interface", "ipv4", "delete", "neighbors", str(index), str(ip)])
    rc, out, err = run(["netsh", "interface", "ipv4", "add", "neighbors", str(index), str(ip), mac_win])
    if rc == 0:
        log("✅ 已添加静态 ARP: %s (索引 %s) %s -> %s" % (iface_name, index, ip, mac_win))
        return True
    log("⚠️ 添加静态 ARP 失败: %s" % (err or out))
    return False


def clear_arp_table(log_target):
    """清空 ARP 表；`arp -d *` 失败时退回逐条删除静态项。"""
    log = as_log(log_target)
    rc, out, err = run(["arp", "-d", "*"])
    if rc == 0:
        log("ℹ️ 已清空 ARP 表")
        return True

    log("⚠️ arp -d * 失败(%s)，尝试逐条删除……" % (err or out))
    rc2, out2, err2 = run(["arp", "-a"])
    if rc2 != 0 or not out2:
        log("⚠️ 无法列出 ARP 表: %s" % (err2 or out2))
        return False

    deleted = False
    for line in out2.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
            ip, mac = parts[0], parts[1]
            if mac and mac.lower() != "ff-ff-ff-ff-ff-ff":
                if run(["arp", "-d", ip])[0] == 0:
                    log("ℹ️ 已删除 ARP %s" % ip)
                    deleted = True
    if not deleted:
        log("⚠️ 未能删除任何 ARP 条目（可能需要管理员权限或驱动限制）")
    return deleted


# ----------------------------------------------------------------------------
# 网卡重启
# ----------------------------------------------------------------------------

def restart_adapter(iface, log_target=None, wait_seconds=25):
    """
    重启网卡使高级属性生效。兼容旧签名 restart_adapter(iface, output_text, wait_seconds)。
    返回 True/False。
    """
    log = as_log(log_target) if log_target is not None else (lambda msg: print(msg))
    log("⏳ 正在重启网卡 %s 以使设置生效（链路会短暂断开）……" % iface)
    result = _restart_adapter_ps(iface, wait_up=wait_seconds)
    if not result.get("ok"):
        log("⚠️ 重启网卡失败: %s" % (result.get("error") or "未知错误"))
        return False
    status = result.get("status")
    speed = result.get("speed") or ""
    if status == "Up":
        log("✅ 网卡已重启并恢复连接（%s %s，方式: %s）" % (status, speed, result.get("method")))
        return True
    log("⚠️ 网卡已重启但链路状态为 %s（可能是网线未连接或对端未就绪）" % status)
    return True


# ----------------------------------------------------------------------------
# 内部：日志与结果汇总
# ----------------------------------------------------------------------------

def _report_ops(results, log):
    """把批量写入结果写进日志，返回是否有任意一项成功。"""
    if not results:
        return False
    any_ok = False
    for item in results:
        role = item.get("role")
        label = ROLE_LABEL.get(role, role or "?")
        keyword = item.get("kw")
        if item.get("ok"):
            any_ok = True
            log("   ✅ %s (%s) 写入成功" % (label, keyword))
        else:
            log("   ⚠️ %s (%s) 写入失败: %s" % (label, keyword, item.get("msg")))
    return any_ok


def _probe(iface, log):
    """探测驱动属性并打印识别结果，返回 AdapterInspector。"""
    start = time.time()
    log("[探测] 正在读取网卡驱动高级属性……")
    inspector = AdapterInspector(iface)
    if not inspector.ok:
        log("[探测] ⚠️ 读取失败: %s" % inspector.error)
        log("[探测] 将跳过 MAC/VLAN 高级属性设置（IP 与 ARP 仍会执行）")
        return inspector
    log("[探测] 驱动型号: %s" % (inspector.description or "未知"))
    log("[探测] 匹配档案: %s" % (inspector.profile_name or "（无，使用内置关键字表）"))
    for role in ("mac", "vlan_mode", "vlan_id"):
        keyword = inspector.role_keyword(role)
        if keyword:
            log("[探测] %s -> %s ｜ 显示名: %s ｜ 当前值: %s" % (
                ROLE_LABEL[role], keyword, inspector.role_display(role), inspector.current_value(role)))
        else:
            log("[探测] %s -> 该驱动未提供此属性" % ROLE_LABEL[role])
    log("[探测] 耗时 %.2fs" % (time.time() - start))
    return inspector


# ----------------------------------------------------------------------------
# 主逻辑
# ----------------------------------------------------------------------------

def apply_config_windows(iface, cfg, log_target):
    """
    应用配置到指定网口。

    cfg 字段：
      mode            : "default"（恢复默认/上网）或 "test"（按配置固定 IP/VLAN/MAC/ARP）
      ip/netmask/gateway : 静态地址；"auto" 表示自动
      vlan_id         : 整数或 "auto"
      mac             : MAC 字符串或 "auto"
      arp             : [{"ip": "...", "mac": "..."}] 或 "auto"
      restart_adapter : 可选，False 表示写入后不自动重启网卡（默认 True）
    """
    log = as_log(log_target)
    cfg = cfg or {}
    mode = str(cfg.get("mode", "test")).strip().lower()
    auto_restart = bool(cfg.get("restart_adapter", True))
    netmask = normalize_netmask(cfg.get("netmask", "auto"))
    started = time.time()

    log("=" * 56)
    log("开始配置网口: %s ｜ 模式: %s" % (iface, mode))

    inspector = _probe(iface, log)
    needs_restart = False

    # ======================= DEFAULT：恢复默认 =======================
    if mode == "default":
        log("[1/5] 切换 IP/DNS 为自动获取(DHCP)……")
        rc, out, err = run(["netsh", "interface", "ip", "set", "address",
                            "name=%s" % iface, "source=dhcp"])
        if rc == 0:
            log("   ✅ IP 已切换为 DHCP（插上线后系统自动获取地址）")
        else:
            log("   ⚠️ 切换 DHCP 失败: %s" % (err or out))
        rc, out, err = run(["netsh", "interface", "ip", "set", "dns",
                            "name=%s" % iface, "source=dhcp"])
        if rc != 0:
            log("   ⚠️ 切换 DNS 为自动失败: %s" % (err or out))

        log("[2/5] 恢复 MAC / VLAN 驱动属性为默认……")
        ops = []
        if inspector.ok:
            if inspector.mac_prop:
                ops.append({"role": "mac",
                            "kw": inspector.role_keyword("mac"),
                            "mode": "reset"})
            if inspector.vlan_mode_prop:
                value, text = choose_vlan_mode_value(inspector.vlan_mode_prop, enable=False)
                if value is None:
                    ops.append({"role": "vlan_mode",
                                "kw": inspector.role_keyword("vlan_mode"),
                                "mode": "reset"})
                else:
                    ops.append({"role": "vlan_mode",
                                "kw": inspector.role_keyword("vlan_mode"),
                                "mode": "set", "val": value})
                    log("   将 VLAN 开关设为: %s" % text)
            if inspector.vlan_id_prop:
                ops.append({"role": "vlan_id",
                            "kw": inspector.role_keyword("vlan_id"),
                            "mode": "set", "val": 0})
        if ops:
            needs_restart = _report_ops(apply_property_ops(iface, ops), log)
        else:
            log("   ℹ️ 无需恢复的高级属性（未识别到 MAC/VLAN 属性）")

        log("[3/5] 清空 ARP 表……")
        clear_arp_table(log)

        if needs_restart and auto_restart:
            log("[4/5] 重启网卡使默认值生效……")
            restart_adapter(iface, log)
        else:
            log("[4/5] 未修改需要重启才能生效的属性，跳过重启")

        log("[5/5] ✅ 已完成 %s 的 Default 配置，可以连接 DDT/DPS（总耗时 %.2fs）"
            % (iface, time.time() - started))
        return

    # ======================= TEST：按配置应用 =======================
    ops = []
    expected = []

    # ---- MAC ----
    mac_cfg = cfg.get("mac", "auto")
    if mac_cfg and str(mac_cfg).strip().lower() != "auto":
        mac_norm = normalize_mac(mac_cfg)
        if mac_norm is None:
            log("⚠️ MAC 格式不正确（需 12 位十六进制，如 0A0B0C026969），已跳过")
        elif not inspector.ok or not inspector.mac_prop:
            log("⚠️ 未找到可写的网络地址(MAC)属性，已跳过 MAC 设置")
        else:
            warn = mac_warning(mac_norm)
            if warn:
                log("ℹ️ MAC 提示: %s" % warn)
            ops.append({"role": "mac", "kw": inspector.role_keyword("mac"),
                        "mode": "set", "val": mac_norm})
            expected.append("MAC=%s" % mac_norm)

    # ---- VLAN ----
    vlan_cfg = cfg.get("vlan_id", "auto")
    want_vlan = vlan_cfg is not None and str(vlan_cfg).strip().lower() != "auto"
    if want_vlan:
        vlan_value = nic_driver._to_int(vlan_cfg)
        if vlan_value is None or not 0 < vlan_value < 4095:
            log("⚠️ VLAN ID 不合法(%s)，应为 1~4094 的整数，已跳过 VLAN 设置" % vlan_cfg)
            want_vlan = False

    if want_vlan:
        mode_ok = False
        if not inspector.ok or not inspector.vlan_mode_prop:
            log("⚠️ 该驱动没有 VLAN 开关属性，无法启用 VLAN 标签")
        else:
            value, text = choose_vlan_mode_value(inspector.vlan_mode_prop, enable=True)
            if value is None:
                log("⚠️ 无法从合法取值中确定 VLAN 开关的启用值，跳过")
            else:
                ops.append({"role": "vlan_mode", "kw": inspector.role_keyword("vlan_mode"),
                            "mode": "set", "val": value})
                log("ℹ️ VLAN 开关将设为: %s (RegistryValue=%s)" % (text, value))
        if not inspector.ok or not inspector.vlan_id_prop:
            log("⚠️ 该网卡驱动未提供可设置的 VLAN ID 属性"
                "（例如 Intel I219-V；Realtek 为 RegVlanID、ASIX 为 VLAN_ID）")
            log("   若设备必须带 VLAN，请改用支持 VLAN ID 的网卡或在交换机侧配置")
        else:
            ops.append({"role": "vlan_id", "kw": inspector.role_keyword("vlan_id"),
                        "mode": "set", "val": vlan_value})
            expected.append("VLAN=%s" % vlan_value)

    # ---- 写入高级属性 ----
    if ops:
        log("[1/5] 写入 MAC / VLAN 高级属性……")
        needs_restart = _report_ops(apply_property_ops(iface, ops), log)
    else:
        log("[1/5] 无 MAC/VLAN 属性需要写入")

    # ---- 重启网卡 ----
    if needs_restart and auto_restart:
        log("[2/5] 重启网卡使 MAC/VLAN 生效……")
        restart_adapter(iface, log)
    elif needs_restart:
        log("[2/5] 已写入 MAC/VLAN，但配置要求不自动重启；请手动重启网卡后生效")
    else:
        log("[2/5] 跳过网卡重启")

    # ---- IP ----
    ip_cfg = cfg.get("ip", "auto")
    gateway_cfg = cfg.get("gateway", "auto")
    if ip_cfg and str(ip_cfg).strip().lower() != "auto" and netmask != "auto":
        gateway = "none" if str(gateway_cfg).strip().lower() == "auto" else str(gateway_cfg)
        rc, out, err = run(["netsh", "interface", "ip", "set", "address", iface,
                            "static", str(ip_cfg), str(netmask), gateway])
        if rc == 0:
            log("[3/5] ✅ IP 已设置为 %s/%s%s"
                % (ip_cfg, netmask, "" if gateway == "none" else " 网关 %s" % gateway))
        else:
            log("[3/5] ⚠️ 设置 IP 失败: %s" % (err or out))
    else:
        log("[3/5] IP 为 auto，保持系统当前设置")

    # ---- ARP ----
    arp_cfg = cfg.get("arp", "auto")
    if isinstance(arp_cfg, list) and arp_cfg:
        log("[4/5] 写入静态 ARP……")
        for entry in arp_cfg:
            if isinstance(entry, dict):
                add_static_arp(entry.get("ip"), entry.get("mac"), log, iface)
    else:
        log("[4/5] 无静态 ARP 需要写入")

    log("[5/5] ✅ 已完成 %s 的 Test 配置%s（总耗时 %.2fs）"
        % (iface,
           "：" + "、".join(expected) if expected else "",
           time.time() - started))
    log("✅ 可以尝试连接 172.16.105.26")
