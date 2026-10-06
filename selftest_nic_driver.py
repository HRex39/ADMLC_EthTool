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

# ---- 语义推断：不看固定名字，只看「类型 + 取值域」（新网卡自动适配的关键） ----
import json as _json  # noqa: E402
import os as _os  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402


def _prop(display, keyword, enum=False):
    item = {"DisplayName": display, "RegistryKeyword": keyword}
    if enum:
        item["ValidDisplayValues"] = ["已禁用", "已启用"]
        item["ValidRegistryValues"] = [0, 1]
    return item


tricky = [
    _prop("VLAN ID", "RegVlanID"),
    _prop("VLAN标识", "VLAN_ID"),
    _prop("VLAN Tag Id", "VlanTagId"),
    _prop("Packet Priority & VLAN", "*PriorityVLANTag", enum=True),
    _prop("Priority & VLAN", "PriorityVLANTag", enum=True),
    _prop("Network Address", "NetworkAddress"),
    _prop("本地管理地址", "NetworkAddress2"),
    _prop("流量控制", "*FlowControl", enum=True),
    _prop("Wake on Magic Packet", "*WakeOnMagicPacket", enum=True),
]

inferred_ids = sorted(p["RegistryKeyword"] for p in nd._semantic_infer("vlan_id", tricky))
check("语义推断 vlan_id：命中 3 个 ID 类、不含开关", inferred_ids,
      ["RegVlanID", "VLAN_ID", "VlanTagId"])

inferred_modes = [p["RegistryKeyword"] for p in nd._semantic_infer("vlan_mode", tricky)]
check("语义推断 vlan_mode：只命中两个开关", inferred_modes,
      ["*PriorityVLANTag", "PriorityVLANTag"])

inferred_macs = [p["RegistryKeyword"] for p in nd._semantic_infer("mac", tricky)]
check("语义推断 mac：只命中 address 类，排除干扰项", inferred_macs,
      ["NetworkAddress", "NetworkAddress2"])

# ---- 候选列表与识别来源 ----
insp_c = nd.AdapterInspector("以太网 13", profiles={}, discover=False)
insp_c.ok = True
insp_c.profile = {}
insp_c.props = tricky
for role in nd.ROLE_KEYWORDS:
    insp_c._roles[role] = insp_c._resolve_role(role)
check("候选列表首选 == 识别结果", insp_c.role_candidates("vlan_id")[0]["RegistryKeyword"],
      insp_c.role_keyword("vlan_id"))
check("候选列表按关键字去重",
      len(insp_c.role_candidates("vlan_mode"))
      == len({nd._norm_kw(p["RegistryKeyword"]) for p in insp_c.role_candidates("vlan_mode")}),
      True)
check("识别来源：内置关键字表", insp_c.role_source("vlan_id"), "keyword")

# 全新的、任何候选表里都没有的关键字，也必须能靠语义推断认出来
insp_i = nd.AdapterInspector("以太网 99", profiles={}, discover=False)
insp_i.ok = True
insp_i.profile = {}
insp_i.props = [_prop("未知属性A", "FooVlanIdNo"),
                _prop("未知属性B", "BarPriorityVlanTag", enum=True)]
for role in nd.ROLE_KEYWORDS:
    insp_i._roles[role] = insp_i._resolve_role(role)
check("全新关键字也能认出 VLAN ID", insp_i.role_keyword("vlan_id"), "FooVlanIdNo")
check("全新关键字也能认出 VLAN 开关", insp_i.role_keyword("vlan_mode"), "BarPriorityVlanTag")
check("识别来源：语义推断", insp_i.role_source("vlan_id"), "inferred")

# 档案里写 null = 明确声明该驱动不支持，候选列表必须为空（不再瞎猜）
insp_null = nd.AdapterInspector("以太网", profiles={}, discover=False)
insp_null.profile = {"vlan_id": None}
insp_null.props = tricky
check("档案声明 null 时候选列表为空", insp_null.role_candidates("vlan_id"), [])

# ---- 写入回读校验 ----
check("回读校验：数值一致", nd._readback_matches({"val": 105}, "105"), True)
check("回读校验：MAC 忽略大小写与分隔符",
      nd._readback_matches({"val": "0a0b0c026969"}, "0A-0B-0C-02-69-69"), True)
check("回读校验：不一致判失败", nd._readback_matches({"val": 105}, "0"), False)
check("回读校验：回读为空判失败", nd._readback_matches({"val": "0a0b0c026969"}, None), False)
check("回读校验：reset 模式视为成功", nd._readback_matches({"mode": "reset"}, None), True)

# ---- 自动落盘学习（用程序目录下的临时文件，绝不碰真实的 driver_profiles.local.json） ----
_here = _os.path.dirname(_os.path.abspath(__file__))
_fd, tmp_file = _tempfile.mkstemp(prefix="_admlc_profile_test_", suffix=".json", dir=_here)
_os.close(_fd)
_os.remove(tmp_file)  # persist_profile 会自己创建该文件
first_write = nd.persist_profile("Test NIC", {"mac": "NetworkAddress", "vlan_id": "VLAN_ID"},
                                 path=tmp_file, interface="以太网 9")
second_write = nd.persist_profile("Test NIC", {"mac": "NetworkAddress", "vlan_id": "VLAN_ID"},
                                  path=tmp_file, interface="以太网 9")
reloaded = nd.load_profiles(path=tmp_file)
check("落盘：首次写入返回已更新", first_write, True)
check("落盘：重复写入返回无变化", second_write, False)
check("落盘：内容可按档案格式读回", reloaded.get("Test NIC"),
      {"mac": "NetworkAddress", "vlan_id": "VLAN_ID"})
with open(tmp_file, "r", encoding="utf-8") as handle:
    raw_profile = _json.load(handle)
check("落盘：记录了使用过的网口名",
      raw_profile["_meta"]["Test NIC"]["interfaces"], ["以太网 9"])
for _leftover in (tmp_file, tmp_file + ".tmp"):
    try:
        _os.remove(_leftover)
    except OSError:
        pass

# ---- 驱动档案文件结构 ----
check("profiles 结构正确", all(isinstance(v, dict) for v in profiles.values()), True)

print("\n结果:", "全部通过" if not FAIL else "失败项 -> %s" % FAIL)
sys.exit(1 if FAIL else 0)
