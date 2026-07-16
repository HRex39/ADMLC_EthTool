# windows_config.py
# 稳健的 Windows 网卡配置脚本（适用于 GUI 调用）
# 功能：在 admin 权限下应用 default/test 配置（IP, VLAN, MAC, ARP）
# 要点：
#  - PowerShell 输出强制 UTF-8，避免中文乱码
#  - subprocess 输出安全解码（utf-8, errors='replace'）
#  - 先检测高级属性再写入，写入失败时给出友好提示
#  - VLAN: 优先写入 "VLAN Enable"（若存在），再写 VLAN ID 到 "VLAN标识"
#  - MAC: 规范化为 12 位十六进制（无分隔符），写入 NetworkAddress 类属性
#  - ARP: Windows 要求连字符格式（00-11-22-33-44-55）
#  - 所有输出通过 output_text.insert 写回 GUI（调用者需传入 Tk Text 控件）
# 使用前提：以管理员权限运行 Python 程序

import subprocess
import re
import time

global NETWORK_ADDRESSES, VLANID_PROPERTIES, PACKET_PRIORITY_PROPERTIES
NETWORK_ADDRESSES = ["Network Address", "网络地址", "NetworkAddress", "网络 地址"]
VLANID_PROPERTIES = ["VLAN ID", "VLAN标识"]
PACKET_PRIORITY_PROPERTIES = ["Packet Priority & VLAN", "数据包优先级 & VLAN", "优先级 & VLAN", "Priority & VLAN"]

# -------------------- 基础工具函数 --------------------

def run(cmd):
    if isinstance(cmd, str):
        cmd_list = cmd.split()
    else:
        cmd_list = cmd

    try:
        p = subprocess.Popen(
            cmd_list,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW  # 关键：隐藏窗口
        )
        out_bytes, err_bytes = p.communicate()
        out = out_bytes.decode('utf-8', errors='replace') if isinstance(out_bytes, (bytes, bytearray)) else str(out_bytes or "")
        err = err_bytes.decode('utf-8', errors='replace') if isinstance(err_bytes, (bytes, bytearray)) else str(err_bytes or "")
        return p.returncode, out.strip(), err.strip()
    except Exception as e:
        return 1, "", f"run() exception: {e}"

def pw(ps_cmd):
    """
    在 PowerShell 中执行一行命令，并强制设置控制台输出为 UTF-8，避免编码问题。
    ps_cmd: PowerShell 命令字符串（不包含 powershell 前缀）
    """
    full_cmd = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + ps_cmd
    cmd = ["powershell", "-NoProfile", "-Command", full_cmd]
    return run(cmd)

def normalize_name(s):
    """清理属性名中的 BOM / 控制字符并去除首尾空白"""
    if not s:
        return s
    s = s.replace('\ufeff', '')
    s = re.sub(r'[\x00-\x1f\x7f]+', '', s)
    return s.strip()

# -------------------- 网卡高级属性检测与写入 --------------------

def find_property(iface, keyword):
    """
    查找网卡高级属性中 DisplayName 匹配 keyword 的项（正则匹配，大小写不敏感）。
    返回 DisplayName 列表（已规范化）。
    """
    kw = keyword.replace("'", "''")
    cmd = f"Get-NetAdapterAdvancedProperty -Name '{iface}' | Where-Object {{$_.DisplayName -match '{kw}'}} | Select-Object -ExpandProperty DisplayName"
    rc, out, err = pw(cmd)
    if rc != 0:
        return []
    lines = [normalize_name(line) for line in out.splitlines() if line.strip()]
    return lines

def get_valid_values(iface, display_name):
    """
    获取某个高级属性的 ValidDisplayValues（如果有）。
    返回字符串列表（可能为空）。
    """
    dn = display_name.replace("'", "''")
    cmd = f"(Get-NetAdapterAdvancedProperty -Name '{iface}' -DisplayName '{dn}').ValidDisplayValues"
    rc, out, err = pw(cmd)
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]

def safe_set_property(iface, display_name, display_value, output_text):
    """
    安全设置高级属性并把结果写回 output_text。
    display_value 可以是 int 或 str；函数会根据类型构造 PowerShell 表达式。
    返回 True 表示设置成功，False 表示失败（并在 output_text 中写入错误信息）。
    """
    if isinstance(display_value, int):
        val_expr = str(display_value)
    else:
        v = str(display_value).replace("'", "''")
        val_expr = f"'{v}'"
    dn = display_name.replace("'", "''")
    cmd = f"Set-NetAdapterAdvancedProperty -Name '{iface}' -DisplayName '{dn}' -DisplayValue {val_expr}"
    try:
        rc, out, err = pw(cmd)
    except Exception as e:
        output_text.insert("end", f"⚠️ 设置 {display_name} 时发生异常: {e}\n")
        output_text.see("end")
        output_text.update_idletasks()
        return False
    if rc == 0:
        output_text.insert("end", f"ℹ️ 设置 {display_name} 成功\n")
        output_text.see("end")
        output_text.update_idletasks()
        return True
    else:
        msg = err or out or "未知错误"
        display_msg = "此 -DisplayName 不匹配 "
        output_text.insert("end", f"⚠️ 设置 {display_name} 失败: {display_msg}\n")
        output_text.see("end")
        output_text.update_idletasks()
        return False

# -------------------- VLAN 启用与 VLAN ID 写入 --------------------

def enable_vlan_if_possible(iface, output_text):
    """
    尝试启用 VLAN。优先选择 ValidDisplayValues 中明确包含 'VLAN Enable' 的值；
    若不存在则谨慎选择其它 enable 值，但避免只启用 Packet Priority（除非没有其它选择）。
    返回 True 表示已启用 VLAN（或驱动接受了启用写入），False 表示未启用。
    """
    candidates = find_property(iface, "VLAN|Vlan|Packet Priority|Priority|PacketPriority|VLAN标识")
    if not candidates:
        output_text.insert("end", "⚠️ 未检测到 VLAN/Packet/Priority 相关属性，跳过 VLAN 启用\n")
        output_text.update_idletasks()
        return False

    # 1) 优先寻找明确包含 "vlan enable" 的值
    for prop in candidates:
        vals = get_valid_values(iface, prop)
        for v in vals:
            if "vlan enable" in v.lower():
                if safe_set_property(iface, prop, v, output_text):
                    output_text.insert("end", f"ℹ️ 已将 {prop} 设置为 {v}（VLAN 已启用）\n")
                    output_text.update_idletasks()
                    return True
                else:
                    output_text.insert("end", f"⚠️ 尝试将 {prop} 设置为 {v} 失败，继续尝试其他属性\n")
                    output_text.update_idletasks()
                    break

    # 2) 再尝试其它 enable 文本，但避免优先选择仅表示 Packet Priority 的值
    for prop in candidates:
        vals = get_valid_values(iface, prop)
        for v in vals:
            low = v.lower()
            # 如果值明确只表示 Packet Priority（且不包含 vlan），跳过优先级选择
            if "packet priority" in low and "vlan" not in low:
                continue
            if "enable" in low or "on" in low or "启用" in low:
                if safe_set_property(iface, prop, v, output_text):
                    output_text.insert("end", f"ℹ️ 已将 {prop} 设置为 {v}\n")
                    output_text.update_idletasks()
                    return True
                else:
                    output_text.insert("end", f"⚠️ 尝试设置 {prop} 为 {v} 失败，继续尝试\n")
                    output_text.update_idletasks()

    # 3) 若仍未启用，尝试写入常见文本（驱动可能接受）
    for prop in candidates:
        for try_val in ["VLAN Enable", "Enable", "On", "Enabled", "启用"]:
            if safe_set_property(iface, prop, try_val, output_text):
                output_text.insert("end", f"ℹ️ 通过尝试写入 '{try_val}' 启用了 {prop}\n")
                output_text.update_idletasks()
                return True

    # 4) 未能启用，输出候选信息供调试
    output_text.insert("end", "⚠️ 未能通过高级属性启用 VLAN。请在设备管理器或厂商工具中手动启用。\n")
    output_text.update_idletasks()
    for p in candidates:
        vals = get_valid_values(iface, p)
        output_text.insert("end", f"ℹ️ 候选属性: {p} ; ValidDisplayValues: {vals}\n")
        output_text.update_idletasks()
    return False

# -------------------- ARP 处理 --------------------

def add_static_arp(ip, mac_raw, output_text):
    """
    在 Windows 上添加静态 ARP：把 mac_raw 规范化为连字符格式并调用 arp -s。
    返回 True/False 并在 output_text 中写入详细信息。
    """
    if not ip or not mac_raw:
        output_text.insert("end", f"⚠️ ARP 条目不完整，跳过: ip={ip}, mac={mac_raw}\n")
        output_text.update_idletasks()
        return False

    # 规范化 MAC：把冒号/点换成连字符，去掉空白，转大写
    mac = mac_raw.replace(":", "-").replace(".", "-").replace(" ", "").upper()
    # 如果用户给的是连续12位十六进制，插入连字符
    compact = re.sub(r'[^0-9A-Fa-f]', '', mac_raw)
    if re.fullmatch(r"[0-9A-Fa-f]{12}", compact):
        s = compact.upper()
        mac = "-".join([s[i:i+2] for i in range(0, 12, 2)])

    # 校验最终格式
    if not re.fullmatch(r"([0-9A-F]{2}-){5}[0-9A-F]{2}", mac):
        output_text.insert("end", f"⚠️ ARP MAC 格式不合法: {mac_raw} -> {mac}\n")
        output_text.update_idletasks()
        return False

    # 执行 arp -s
    rc, out, err = run(["arp", "-s", ip, mac])
    if rc == 0:
        output_text.insert("end", f"ℹ️ ARP 已添加 {ip} -> {mac}\n")
        output_text.update_idletasks()
        return True
    else:
        output_text.insert("end", f"⚠️ 添加 ARP {ip} -> {mac} 失败: {err or out}\n")
        output_text.update_idletasks()
        return False

def clear_arp_table(output_text):
    """
    尝试清空 ARP 表。若 arp -d * 失败，尝试列出并逐条删除静态项（best-effort）。
    """
    rc, out, err = run(["arp", "-d", "*"])
    if rc == 0:
        output_text.insert("end", "ℹ️ 已清空 ARP 表\n")
        output_text.update_idletasks()
        return True

    output_text.insert("end", f"⚠️ arp -d * 失败: {err or out}\n尝试逐条删除（若无权限或驱动限制可能仍失败）...\n")
    rc2, out2, err2 = run(["arp", "-a"])
    if rc2 != 0 or not out2:
        output_text.insert("end", f"⚠️ 无法列出 ARP 表: {err2 or out2}\n")
        output_text.update_idletasks()
        return False

    deleted_any = False
    for line in out2.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"\d+\.\d+\.\d+\.\d+", parts[0]):
            ip = parts[0]
            mac = parts[1]
            if mac and mac != "ff-ff-ff-ff-ff-ff":
                rc3, o3, e3 = run(["arp", "-d", ip])
                if rc3 == 0:
                    output_text.insert("end", f"ℹ️ 已删除 ARP {ip}\n")
                    output_text.update_idletasks()
                    deleted_any = True
                else:
                    output_text.insert("end", f"⚠️ 删除 ARP {ip} 失败: {e3 or o3}\n")
                    output_text.update_idletasks()
    if not deleted_any:
        output_text.insert("end", "⚠️ 未能删除任何 ARP 条目（可能需要管理员权限或驱动限制）\n")
        output_text.update_idletasks()
    return deleted_any

# -------------------- 网卡重启（可选） --------------------

def restart_adapter(iface, output_text, wait_seconds=2):
    """
    禁用并启用网卡以尝试使某些设置（如 MAC/VLAN）生效。
    需要管理员权限。
    """
    rc, out, err = pw(f"Disable-NetAdapter -Name '{iface}' -Confirm:$false")
    if rc == 0:
        output_text.insert("end", f"ℹ️ 已禁用网卡 {iface}\n")
        output_text.update_idletasks()
    else:
        output_text.insert("end", f"⚠️ 禁用网卡失败: {err or out}\n")
        output_text.update_idletasks()
        return False
    time.sleep(wait_seconds)
    rc, out, err = pw(f"Enable-NetAdapter -Name '{iface}' -Confirm:$false")
    if rc == 0:
        output_text.insert("end", f"ℹ️ 已启用网卡 {iface}\n")
        output_text.update_idletasks()
        return True
    else:
        output_text.insert("end", f"⚠️ 启用网卡失败: {err or out}\n")
        output_text.update_idletasks()
        return False

# -------------------- 主逻辑：应用配置 --------------------

def apply_config_windows(iface, cfg, output_text):
    """
    根据 cfg 应用配置到 iface，并把过程写回 output_text（Tk Text 控件）。
    cfg 字段：
      - mode: "default" 或 "test"
      - ip, netmask, gateway
      - vlan_id (整数或 "auto")
      - mac (字符串或 "auto")
      - arp: 列表 [{"ip":"x.x.x.x","mac":"xx-xx-xx-xx-xx-xx"}] 或 "auto"
    """
    netmask = cfg.get("netmask", "auto")
    cidr_to_mask = {"24": "255.255.255.0", "16": "255.255.0.0", "8": "255.0.0.0"}
    if str(netmask) in cidr_to_mask:
        netmask = cidr_to_mask[str(netmask)]

    mode = cfg.get("mode", "test")

    # ---------- DEFAULT 模式（安全恢复） ----------
    if mode == "default":
        # 1) IP/DNS → 自动获取
        output_text.insert("end", "⏳ 正在切换到 DHCP...\n")
        output_text.update_idletasks()
        run(["netsh", "interface", "ip", "set", "address", f"name={iface}", "source=dhcp"])
        run(["netsh", "interface", "ip", "set", "dns", f"name={iface}", "source=dhcp"])
        output_text.insert("end", "[1/5] 已设置为自动获取 IP 和 DNS\n")
        output_text.see("end")
        output_text.update_idletasks()

        # ⚠️ 不再调用 release/renew，避免断开时卡住
        output_text.insert("end", "[2/5] 已切换到 DHCP，插上线后系统会自动获取地址\n")
        output_text.see("end")
        output_text.update_idletasks()

        # 2) VLAN → 默认禁用
        for vlan_name in PACKET_PRIORITY_PROPERTIES:
            if safe_set_property(iface, vlan_name, vlan_name + " Disable", output_text):
                output_text.insert("end", f"[2/5] 已禁用 VLAN 属性 ({vlan_name})\n")
                output_text.see("end")
                output_text.update_idletasks()
                break
        for vlan_id in VLANID_PROPERTIES:
            if safe_set_property(iface, vlan_id, '0', output_text):
                output_text.insert("end", f"[2/5] 已恢复默认 VLAN ID (属性名:{vlan_id})\n")
                output_text.see("end")
                output_text.update_idletasks()
                break

        # 3) MAC → 恢复硬件默认（清空 Network Address）
        for mac_name in NETWORK_ADDRESSES:
            if safe_set_property(iface, mac_name, "--", output_text):
                output_text.insert("end", f"[3/5] 已恢复默认 网络地址(属性名: {mac_name})\n")
                output_text.see("end")
                output_text.update_idletasks()  
                break
            # if pw(f"Set-NetAdapterAdvancedProperty -Name '{iface}' -DisplayName '{mac_name}' -DisplayValue '--'"):
            #     output_text.insert("end", f"[3/5] 已恢复默认 MAC（属性名: {mac_name}）\n")
            #     output_text.see("end")
            #     output_text.update_idletasks()  
            #     break
        
        # 4) ARP → 清空
        run(["arp", "-d", "*"])
        output_text.insert("end", "[4/5] 已清空 ARP 表\n")
        output_text.see("end")
        output_text.update_idletasks()

        output_text.insert("end", f"[5/5]✅ 已完成{iface} Default配置 \n✅ 可以连接DDT和DPS\n")
        output_text.see("end")
        output_text.update_idletasks()
        return

    # ---------- TEST 模式（全部应用） ----------
    # 1) IP / gateway
    if cfg.get("ip", "auto") != "auto" and netmask != "auto":
        rc, out, err = run([
            "netsh", "interface", "ip", "set", "address", iface,
            "static", str(cfg["ip"]), str(netmask),
            str(cfg["gateway"]) if cfg.get("gateway", "auto") != "auto" else "none"
        ])
        if rc == 0:
            output_text.insert("end", f"[1/4] IP 已设置为 {cfg['ip']}/{cfg.get('netmask')}\n[Setting Network...]\n")
            output_text.see("end")
            output_text.update_idletasks()
        else:
            output_text.insert("end", f"⚠️ 设置 IP 失败: {err or out}\n")
            output_text.see("end")
            output_text.update_idletasks()

    # 2) 启用 VLAN（更稳健）
    vlan_enabled = enable_vlan_if_possible(iface, output_text)

    # 3) 设置 VLAN ID（仅当检测到可设置且 cfg 提供）
    if vlan_enabled and cfg.get("vlan_id", "auto") != "auto":
        try:
            vid = int(cfg["vlan_id"])
            for vlan_prop in VLANID_PROPERTIES:
                if safe_set_property(iface, vlan_prop, vid, output_text):
                    output_text.insert("end", f"[2/4] VLAN ID 已设置为 {vid} (属性名: {vlan_prop})\n[Setting Network...]\n")
                    output_text.see("end")
                    output_text.update_idletasks()
                    break
            else:
                output_text.insert("end", "⚠️ 设置 VLAN ID 失败，请检查驱动或手动设置\n")
                output_text.see("end")
                output_text.update_idletasks()
        except ValueError:
            output_text.insert("end", "⚠️ vlan_id 不是整数，跳过 VLAN ID 设置\n")
            output_text.see("end")
            output_text.update_idletasks()

    # 4) 设置 MAC（如果提供且格式正确）
    mac = cfg.get("mac", "auto")
    if mac != "auto" and mac:
        norm = ''.join(ch for ch in mac if ch.isalnum()).lower()
        if len(norm) == 12 and all(c in "0123456789abcdef" for c in norm):
            for mac_prop in NETWORK_ADDRESSES:
                if safe_set_property(iface, mac_prop, norm, output_text):
                    output_text.insert("end", f"[3/4] 写入 MAC 后已重启网卡以生效 (属性名: {mac_prop})\n")
                    output_text.see("end")
                    output_text.update_idletasks()
                    break
            else:
                output_text.insert("end", "⚠️ 未找到可写的 NetworkAddress 属性，跳过 MAC 设置\n")
                output_text.see("end")
                output_text.update_idletasks()
        else:
            output_text.insert("end", "⚠️ 提供的 MAC 格式不正确，需为 12 位十六进制，例如 02:11:22:33:44:55\n")
            output_text.see("end")
            output_text.update_idletasks()

    # 5) 设置静态 ARP（使用 add_static_arp）
    if cfg.get("arp", "auto") != "auto" and isinstance(cfg.get("arp"), list):
        for entry in cfg["arp"]:
            ip = entry.get("ip")
            mac_entry = entry.get("mac")
            add_static_arp(ip, mac_entry, output_text)

    output_text.insert("end", f"[4/4]✅ 已完成{iface}Test 配置\n✅ 可以尝试连接172.16.105.26\n")
    output_text.see("end")
    output_text.update_idletasks()