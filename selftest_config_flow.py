# -*- coding: utf-8 -*-
"""离线自检：apply_config_windows 的编排逻辑（monkeypatch 掉所有真实系统调用，零副作用）。

覆盖：Test/Default 两条路径实际下发的操作、VLAN 开关必须取 3 而不得取 1、未提供 VLAN ID 的
驱动要明确提示、全部 auto 时不写属性也不重启、非法 MAC/VLAN 不崩溃、restart_adapter=false 生效。
"""
import sys

sys.path.insert(0, ".")
import nic_driver as nd  # noqa: E402
import windows_config as wc  # noqa: E402

FAIL = []


def check(label, cond, extra=""):
    print("%-52s %s %s" % (label, "PASS" if cond else "FAIL", extra))
    if not cond:
        FAIL.append(label)


def eq(label, actual, expected):
    """相等断言（注意不能用 check，否则 0 / '' 这类假值会被误判）。"""
    ok = actual == expected
    print("%-52s %s got=%r want=%r" % (label, "PASS" if ok else "FAIL", actual, expected))
    if not ok:
        FAIL.append("%s (got=%r want=%r)" % (label, actual, expected))


ASIX_PROPS = [
    {"DisplayName": "网络地址", "RegistryKeyword": "NetworkAddress", "DisplayValue": "0A0B0C026969"},
    {"DisplayName": "Packet Priority & VLAN", "RegistryKeyword": "*PriorityVLANTag",
     "DisplayValue": "已启用数据包优先级和 VLAN",
     "ValidDisplayValues": ["已禁用数据包优先级和 VLAN", "已启用数据包优先级", "已启用 VLAN",
                            "已启用数据包优先级和 VLAN"],
     "ValidRegistryValues": [0, 1, 2, 3]},
    {"DisplayName": "VLAN标识", "RegistryKeyword": "VLAN_ID", "DisplayValue": "0"},
]


class FakeInspector:
    """模拟一张 ASIX 网卡（有 VLAN_ID 属性）。"""

    def __init__(self, iface, profiles=None, discover=True):
        self.iface = iface
        self.ok = True
        self.error = ""
        self.description = "ASIX USB to Gigabit Ethernet Family Adapter"
        self.profile_name = "ASIX USB to Gigabit Ethernet Family Adapter"
        self.adapter = {"InterfaceDescription": self.description}
        self.props = ASIX_PROPS
        self._by_role = {"mac": ASIX_PROPS[0], "vlan_mode": ASIX_PROPS[1], "vlan_id": ASIX_PROPS[2]}

    def prop(self, role):
        return self._by_role.get(role)

    def role_keyword(self, role):
        p = self.prop(role)
        return p["RegistryKeyword"] if p else None

    def role_display(self, role):
        p = self.prop(role)
        return p["DisplayName"] if p else None

    def current_value(self, role):
        p = self.prop(role)
        return p.get("DisplayValue") if p else None

    def role_candidates(self, role):
        p = self.prop(role)
        return [p] if p else []

    def role_source(self, role):
        return "profile" if self.prop(role) else "missing"

    @property
    def mac_prop(self):
        return self.prop("mac")

    @property
    def vlan_mode_prop(self):
        return self.prop("vlan_mode")

    @property
    def vlan_id_prop(self):
        return self.prop("vlan_id")


class FakeInspectorNoVlanId(FakeInspector):
    """模拟 Intel I219-V：有 MAC 与 VLAN 开关，但没有 VLAN ID。"""

    def __init__(self, iface, profiles=None, discover=True):
        FakeInspector.__init__(self, iface, profiles, discover)
        self.description = "Intel(R) Ethernet Connection (16) I219-V"
        self._by_role = {"mac": ASIX_PROPS[0], "vlan_mode": ASIX_PROPS[1], "vlan_id": None}


def install_stubs(inspector_cls, log_sink, ops_sink):
    """替换所有会真正改动系统的函数。"""
    wc.AdapterInspector = inspector_cls
    wc.run = lambda cmd, timeout=None: (log_sink("  [stub] run: %s" % " ".join(map(str, cmd))) or (0, "", ""))
    wc.clear_arp_table = lambda target: (log_sink("  [stub] 清空 ARP 表") or True)
    wc.add_static_arp = lambda ip, mac, target, iface_name=None: (
        log_sink("  [stub] 静态 ARP %s -> %s @ %s" % (ip, mac, iface_name)) or True)
    wc.restart_adapter = lambda iface, target=None, wait_seconds=25: (
        log_sink("  [stub] 重启网卡 %s" % iface) or True)

    def fake_apply_property_ops(iface, ops, timeout=120):
        ops_sink.extend(ops)
        return [{"role": op["role"], "kw": op["kw"], "ok": True, "msg": "",
                 "readback": op.get("val"), "verified": True} for op in ops]

    # try_roles 在 nic_driver 内部调用 apply_property_ops，所以必须打在 nic_driver 上
    nd.apply_property_ops = fake_apply_property_ops

    def fake_persist(description, role_keywords, path=None, interface=None):
        log_sink("  [stub] 落盘档案 %s -> %s" % (description, role_keywords))
        return True

    nd.persist_profile = fake_persist  # 别在测试中真的写出 driver_profiles.local.json


# ================= TEST 模式：ASIX（应写 MAC + VLAN开关=3 + VLAN_ID=105，并重启） =================
lines, ops = [], []
install_stubs(FakeInspector, lines.append, ops)
wc.apply_config_windows("以太网 13", {
    "mode": "test", "ip": "172.16.105.105", "netmask": "255.255.255.0", "gateway": "auto",
    "vlan_id": 105, "mac": "0A0B0C026969",
    "arp": [{"ip": "172.16.105.26", "mac": "0A-0B-0C-02-00-06"}],
}, lines.append)

by_role = {op["role"]: op for op in ops}
check("TEST: 写入 MAC", by_role.get("mac", {}).get("val") == "0a0b0c026969")
eq("TEST: VLAN 开关取 3（不得取 1）", by_role.get("vlan_mode", {}).get("val"), 3)
eq("TEST: 写入 VLAN ID", by_role.get("vlan_id", {}).get("val"), 105)
check("TEST: 已自动重启网卡", any("[stub] 重启网卡" in x for x in lines))
check("TEST: 已设置静态 IP", any("static" in x and "172.16.105.105" in x for x in lines))
check("TEST: 已写入静态 ARP", any("172.16.105.26" in x for x in lines))
check("TEST: 探测到驱动型号", any("ASIX USB to Gigabit Ethernet" in x for x in lines))
check("TEST: 成功的关键字已自动落盘学习", any("[stub] 落盘档案" in x and "VLAN_ID" in x for x in lines))

# ================= TEST 模式：Intel I219-V（无 VLAN ID，应明确提示并跳过） =================
lines2, ops2 = [], []
install_stubs(FakeInspectorNoVlanId, lines2.append, ops2)
wc.apply_config_windows("以太网", {
    "mode": "test", "ip": "auto", "netmask": "auto", "gateway": "auto",
    "vlan_id": 105, "mac": "auto",
}, lines2.append)
roles2 = {op["role"] for op in ops2}
check("Intel: 只写 VLAN 开关，不写 VLAN ID", roles2 == {"vlan_mode"}, str(roles2))
check("Intel: 明确提示驱动未提供 VLAN ID",
      any("未提供可设置的 VLAN ID 属性" in x for x in lines2))

# ================= TEST 模式：全 auto（不应写入任何属性、不应重启） =================
lines3, ops3 = [], []
install_stubs(FakeInspector, lines3.append, ops3)
wc.apply_config_windows("以太网 13", {"mode": "test", "ip": "auto", "netmask": "auto",
                                      "gateway": "auto", "vlan_id": "auto", "mac": "auto"},
                        lines3.append)
check("全 auto: 不下发任何属性写入", len(ops3) == 0, "ops=%d" % len(ops3))
check("全 auto: 不重启网卡", not any("[stub] 重启网卡" in x for x in lines3))

# ================= TEST 模式：非法 MAC / 非法 VLAN（应跳过且不崩溃） =================
lines4, ops4 = [], []
install_stubs(FakeInspector, lines4.append, ops4)
wc.apply_config_windows("以太网 13", {"mode": "test", "ip": "auto", "netmask": "auto",
                                      "gateway": "auto", "vlan_id": 9999, "mac": "ZZZZ"},
                        lines4.append)
check("非法输入: 无属性写入", len(ops4) == 0, "ops=%d" % len(ops4))
check("非法 MAC 有提示", any("MAC 格式不正确" in x for x in lines4))
check("非法 VLAN 有提示", any("VLAN ID 不合法" in x for x in lines4))

# ================= TEST 模式：restart_adapter=false（应不重启） =================
lines5, ops5 = [], []
install_stubs(FakeInspector, lines5.append, ops5)
wc.apply_config_windows("以太网 13", {"mode": "test", "ip": "auto", "netmask": "auto",
                                      "gateway": "auto", "vlan_id": 105, "mac": "auto",
                                      "restart_adapter": False}, lines5.append)
check("关闭自动重启: 确实未重启", not any("[stub] 重启网卡" in x for x in lines5))
check("关闭自动重启: 有手动提示", any("请手动重启网卡" in x for x in lines5))

# ================= DEFAULT 模式（恢复：DHCP + 关闭 VLAN + VLAN_ID=0 + 清 ARP + 重启） =================
lines6, ops6 = [], []
install_stubs(FakeInspector, lines6.append, ops6)
wc.apply_config_windows("以太网 13", {"mode": "default", "ip": "auto", "netmask": "auto",
                                      "gateway": "auto", "vlan_id": "auto", "mac": "auto"},
                        lines6.append)
roles6 = {op["role"]: op for op in ops6}
eq("DEFAULT: 恢复 MAC（reset 而非猜 '--'）", roles6.get("mac", {}).get("mode"), "reset")
eq("DEFAULT: 关闭 VLAN 开关取 0", roles6.get("vlan_mode", {}).get("val"), 0)
eq("DEFAULT: VLAN ID 归零", roles6.get("vlan_id", {}).get("val"), 0)
check("DEFAULT: 已切 DHCP", any("source=dhcp" in x for x in lines6))
check("DEFAULT: 已清空 ARP", any("清空 ARP 表" in x for x in lines6))
check("DEFAULT: 已重启网卡", any("[stub] 重启网卡" in x for x in lines6))

# ================= MTU：范围校验 / PowerShell 下发 / netsh 兜底 / 回读 =================
mtu_lines = []


def stub_mtu(ps_result, readback_seq, run_result=(0, "", "")):
    """替换 MTU 相关的所有系统调用，返回 netsh/PS 调用记录。"""
    calls = []
    nd.run_ps_json = lambda script, timeout=60: ps_result
    wc.run = lambda cmd, timeout=None: (calls.append(cmd) or run_result)
    seq = list(readback_seq)

    def fake_get_mtu(iface):
        return seq.pop(0) if seq else None

    wc.get_mtu = fake_get_mtu
    return calls


calls = stub_mtu({"ok": True, "mtu": 1400}, [1500, 1400])
check("MTU: 合法值 1400 下发成功", wc.set_mtu("以太网 13", 1400, mtu_lines.append), True)
check("MTU: 成功路径不触发 netsh 兜底", not calls, str(calls))

stub_mtu({"ok": True, "mtu": 1400}, [])
eq("MTU: 越界 9000 直接拒绝", wc.set_mtu("以太网 13", 9000, mtu_lines.append), False)

calls = stub_mtu({"ok": False, "error": "拒绝访问"}, [1500, 1400])
check("MTU: PowerShell 失败时 netsh 兜底成功",
      wc.set_mtu("以太网 13", 1400, mtu_lines.append), True)
check("MTU: 兜底命令带 store=persistent",
      any(isinstance(cmd, (list, tuple)) and "store=persistent" in cmd
          for cmd in calls), True)

stub_mtu({"ok": True, "mtu": 1400}, [1500, 1500])
eq("MTU: 回读不一致判失败", wc.set_mtu("以太网 13", 1400, mtu_lines.append), False)

stub_mtu({"ok": True, "mtu": 1400}, [1400])
check("MTU: 已是目标值时跳过", wc.set_mtu("以太网 13", 1400, mtu_lines.append), True)

# ================= 配置结构：三套配置与 8255 的两跳链路 =================
import json as _json  # noqa: E402
import os as _os  # noqa: E402

with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "configs.json"),
          "r", encoding="utf-8") as _fh:
    cfgs = _json.load(_fh)

check("配置：存在 Auto", "Auto" in cfgs)
check("配置：存在 ADMLC_DebugTool_8155", "ADMLC_DebugTool_8155" in cfgs)
check("配置：旧名 ADMLC_DebugTool 已移除", "ADMLC_DebugTool" not in cfgs)
eq("配置：8155 网卡参数保持原样",
   (cfgs["ADMLC_DebugTool_8155"]["ip"],
    cfgs["ADMLC_DebugTool_8155"]["netmask"],
    cfgs["ADMLC_DebugTool_8155"]["vlan_id"],
    cfgs["ADMLC_DebugTool_8155"]["mac"]),
   ("172.16.105.105", "255.255.255.0", 105, "0A0B0C026969"))
eq("配置：8155 的静态 ARP 未变",
   cfgs["ADMLC_DebugTool_8155"]["arp"],
   [{"ip": "172.16.105.26", "mac": "0A-0B-0C-02-00-06"}])
check("配置：8155 不带多跳链路（走原有直连）",
      "access" not in cfgs["ADMLC_DebugTool_8155"])
eq("配置：8255 网卡基线 = Auto(default)", cfgs["ADMLC_DebugTool_8255"]["mode"], "default")

hops_8255 = cfgs["ADMLC_DebugTool_8255"]["access"]
eq("配置：8255 是两跳", len(hops_8255), 2)
eq("配置：8255 第一跳 = 网关", hops_8255[0]["host"], "169.254.1.0")
eq("配置：8255 第二跳 = 目标", hops_8255[1]["host"], "172.16.4.26")
eq("配置：8255 第二跳走跳板", hops_8255[1].get("via"), "jump")
check("配置：8255 第一跳要求先拿到链路本地地址",
      hops_8255[0].get("wait_link_local") is True)
eq("配置：8255 第一跳端口 22", hops_8255[0]["port"], 22)
eq("配置：8255 第二跳端口 22", hops_8255[1]["port"], 22)

import device_access as da  # noqa: E402

eq("hops_from_config: 8255 解析出 2 跳",
   len(da.hops_from_config(cfgs["ADMLC_DebugTool_8255"])), 2)
eq("hops_from_config: 8155 返回空（不改变原行为）",
   da.hops_from_config(cfgs["ADMLC_DebugTool_8155"]), [])
eq("hops_from_config: 脏数据被过滤",
   da.hops_from_config({"access": [None, {"host": ""}, {"host": "1.2.3.4"}]}),
   [{"host": "1.2.3.4"}])
eq("hops_from_config: 无 access 返回空", da.hops_from_config({}), [])

# 等待 169.254.x.x：用桩函数验证三条分支（不打真实网络）
_orig_local_ipv4 = da.local_ipv4
da.local_ipv4 = lambda iface: ["192.168.1.5"]
eq("等链路本地：非 APIPA 时超时返回 False",
   da.wait_link_local("X", timeout=0.2, interval=0.05), False)
da.local_ipv4 = lambda iface: ["169.254.7.8"]
eq("等链路本地：命中时返回地址",
   da.wait_link_local("X", timeout=1, interval=0.05), "169.254.7.8")
da.local_ipv4 = lambda iface: None
eq("等链路本地：读不到地址返回 None（继续尝试）",
   da.wait_link_local("X", timeout=0.1, interval=0.05), None)
da.local_ipv4 = _orig_local_ipv4

print("\n结果:", "全部通过" if not FAIL else "失败项 -> %s" % FAIL)
sys.exit(1 if FAIL else 0)

