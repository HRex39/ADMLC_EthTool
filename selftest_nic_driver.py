# -*- coding: utf-8 -*-
"""临时自检脚本（验证后删除）：不接触真实网卡，只验证解析与取值逻辑。"""
import sys

sys.path.insert(0, ".")
import nic_driver as nd  # noqa: E402

FAIL = []


def check(label, got, want):
    ok = got == want
    print("%-58s %s  got=%r" % (label, "PASS" if ok else "FAIL", got))
    if not ok:
        FAIL.append("%s: got %r want %r" % (label, got, want))


# ---- 中文系统下 *PriorityVLANTag 的真实取值 ----
vlan_prop = {
    "DisplayName": "Packet Priority & VLAN",
    "RegistryKeyword": "*PriorityVLANTag",
    "ValidDisplayValues": ["已禁用数据包优先级和 VLAN", "已启用数据包优先级", "已启用 VLAN", "已启用数据包优先级和 VLAN"],
    "ValidRegistryValues": [0, 1, 2, 3],
}
check("VLAN 启用 -> 取 3 而非 1（旧版 bug）", nd.choose_vlan_mode_value(vlan_prop, True)[0], 3)
check("VLAN 禁用 -> 取 0", nd.choose_vlan_mode_value(vlan_prop, False)[0], 0)

# 英文系统同驱动
vlan_en = dict(vlan_prop, ValidDisplayValues=[
    "Packet Priority & VLAN Disabled", "Packet Priority Enabled",
    "VLAN Enabled", "Packet Priority & VLAN Enabled"])
check("英文系统 VLAN 启用 -> 取 3", nd.choose_vlan_mode_value(vlan_en, True)[0], 3)
check("英文系统 VLAN 禁用 -> 取 0", nd.choose_vlan_mode_value(vlan_en, False)[0], 0)

# 纯数值型驱动（无显示名信息）
check("无显示名信息时启用 -> 取 3",
      nd.choose_vlan_mode_value({"ValidDisplayValues": None, "ValidRegistryValues": [0, 1, 2, 3]}, True)[0], 3)

# ---- 三级匹配：ASIX（档案命中） ----
profiles = nd.load_profiles()
check("driver_profiles.json 条目数", len(profiles), 4)

insp = nd.AdapterInspector("以太网 13", profiles=profiles, discover=False)
insp.ok = True
insp.description = "ASIX USB to Gigabit Ethernet Family Adapter"
insp.profile_name, insp.profile = insp._match_profile()
insp.props = [
    {"DisplayName": "网络地址", "RegistryKeyword": "NetworkAddress"},
    {"DisplayName": "Packet Priority & VLAN", "RegistryKeyword": "*PriorityVLANTag"},
    {"DisplayName": "VLAN标识", "RegistryKeyword": "VLAN_ID"},
    {"DisplayName": "流量控制", "RegistryKeyword": "*FlowControl"},
]
for role in nd.ROLE_KEYWORDS:
    insp._roles[role] = insp._resolve_role(role)
check("ASIX 档案命中", insp.profile_name, "ASIX USB to Gigabit Ethernet Family Adapter")
check("ASIX mac", insp.role_keyword("mac"), "NetworkAddress")
check("ASIX vlan_mode", insp.role_keyword("vlan_mode"), "*PriorityVLANTag")
check("ASIX vlan_id", insp.role_keyword("vlan_id"), "VLAN_ID")

# ---- 三级匹配：Realtek（RegVlanID 关键字，任何名字列表都猜不到） ----
insp2 = nd.AdapterInspector("以太网 5", profiles=profiles, discover=False)
insp2.ok = True
insp2.description = "Realtek USB GbE Family Controller"
insp2.profile_name, insp2.profile = insp2._match_profile()
insp2.props = [
    {"DisplayName": "Network Address", "RegistryKeyword": "NetworkAddress"},
    {"DisplayName": "Priority & VLAN", "RegistryKeyword": "*PriorityVLANTag"},
    {"DisplayName": "VLAN ID", "RegistryKeyword": "RegVlanID"},
]
for role in nd.ROLE_KEYWORDS:
    insp2._roles[role] = insp2._resolve_role(role)
check("Realtek vlan_id=RegVlanID", insp2.role_keyword("vlan_id"), "RegVlanID")

# ---- 三级匹配：Intel I219-V（档案声明 vlan_id 不支持 + 显示名是"本地管理地址"） ----
insp3 = nd.AdapterInspector("以太网", profiles=profiles, discover=False)
insp3.ok = True
insp3.description = "Intel(R) Ethernet Connection (16) I219-V"
insp3.profile_name, insp3.profile = insp3._match_profile()
insp3.props = [
    {"DisplayName": "本地管理地址", "RegistryKeyword": "NetworkAddress"},
    {"DisplayName": "数据包优先级和 VLAN", "RegistryKeyword": "*PriorityVLANTag"},
]
for role in nd.ROLE_KEYWORDS:
    insp3._roles[role] = insp3._resolve_role(role)
check("Intel 档案命中", insp3.profile_name, "Intel(R) Ethernet Connection (16) I219-V")
check("Intel mac 靠档案识别（显示名是本地管理地址）", insp3.role_keyword("mac"), "NetworkAddress")
check("Intel vlan_id 明确不支持", insp3.role_keyword("vlan_id"), None)

# ---- 三级匹配：未知驱动（无档案，靠内置关键字表 + 无星号关键字） ----
insp4 = nd.AdapterInspector("以太网 99", profiles={}, discover=False)
insp4.ok = True
insp4.description = "Some Unknown Vendor Adapter"
insp4.profile, insp4.profile_name = {}, None
insp4.props = [
    {"DisplayName": "网络地址", "RegistryKeyword": "NetworkAddress"},
    {"DisplayName": "Packet Priority & VLAN", "RegistryKeyword": "PriorityVLANTag"},
    {"DisplayName": "VLAN标识", "RegistryKeyword": "VLAN_ID"},
]
for role in nd.ROLE_KEYWORDS:
    insp4._roles[role] = insp4._resolve_role(role)
check("未知驱动 mac 兜底", insp4.role_keyword("mac"), "NetworkAddress")
check("未知驱动 vlan_mode 兜底(无星号)", insp4.role_keyword("vlan_mode"), "PriorityVLANTag")
check("未知驱动 vlan_id 兜底", insp4.role_keyword("vlan_id"), "VLAN_ID")

# ---- 档案键支持正则（含括号的真实型号也无需转义） ----
insp5 = nd.AdapterInspector("以太网 13",
                            profiles={"ASIX.*Gigabit": {"vlan_id": "VLAN_ID"}},
                            discover=False)
insp5.ok = True
insp5.description = "ASIX USB to Gigabit Ethernet Family Adapter"
insp5.profile_name, insp5.profile = insp5._match_profile()
check("档案键支持正则子串", insp5.profile_name, "ASIX.*Gigabit")

# ---- 显示名多语言兜底（连关键字都不同名时） ----
check("显示名兜底 mac(本地管理地址)", nd._display_match("mac", "本地管理地址"), True)
check("显示名兜底 vlan_id(VLAN ID)", nd._display_match("vlan_id", "VLAN ID"), True)
check("显示名兜底 vlan_id(VLAN标识)", nd._display_match("vlan_id", "VLAN标识"), True)
check("显示名兜底不误判(流量控制)", nd._display_match("mac", "流量控制"), False)

# ---- MAC 规范化与合法性 ----
check("normalize_mac 冒号写法", nd.normalize_mac("0A:0B:0C:02:69:69"), "0a0b0c026969")
check("normalize_mac 连字符写法", nd.normalize_mac("0A-0B-0C-02-69-69"), "0a0b0c026969")
check("normalize_mac 非法(短)", nd.normalize_mac("0A0B0C"), None)
check("mac_warning 合法 LAA", nd.mac_warning("0a0b0c026969"), None)
check("mac_warning 组播地址被警告", nd.mac_warning("010b0c026969") is not None, True)

# ---- 掩码转换 ----
import windows_config as wc  # noqa: E402

check("cidr 24 -> 掩码", wc.cidr_to_netmask("24"), "255.255.255.0")
check("cidr 26 -> 掩码", wc.cidr_to_netmask("26"), "255.255.255.192")
check("掩码 '24' 归一化", wc.normalize_netmask("24"), "255.255.255.0")
check("掩码原样保留", wc.normalize_netmask("255.255.0.0"), "255.255.0.0")
check("掩码 auto 原样", wc.normalize_netmask("auto"), "auto")

# ---- 旧接口兼容：传 Tk Text 风格对象给 as_log ----
class FakeText:
    def __init__(self):
        self.buf = []

    def insert(self, _where, text):
        self.buf.append(text)

    def see(self, _where):
        pass

    def update_idletasks(self):
        pass


fake = FakeText()
logger = nd.as_log(fake)
logger("兼容旧调用")
check("as_log 兼容 Tk Text 控件", "".join(fake.buf).strip(), "兼容旧调用")

# ---- 驱动档案文件结构 ----
check("profiles 结构正确", all(isinstance(v, dict) for v in profiles.values()), True)

print("\n结果:", "全部通过" if not FAIL else "失败项 -> %s" % FAIL)
sys.exit(1 if FAIL else 0)
