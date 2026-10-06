# -*- coding: utf-8 -*-
"""
nic_driver.py —— Windows 网卡高级属性解析引擎（关键字优先 / 跨厂商 / 跨语言）

为什么需要这个模块
------------------
Windows 网卡"高级属性"在不同厂商驱动 + 不同系统语言下完全不同：

  MAC 地址属性
    Intel I219-V : RegistryKeyword=NetworkAddress , DisplayName="本地管理地址"
    ASIX USB     : RegistryKeyword=NetworkAddress , DisplayName="网络地址"
    Realtek USB  : RegistryKeyword=NetworkAddress , DisplayName="Network Address"
    USB2.0 网卡  : RegistryKeyword=NetworkAddress , DisplayName="NetworkAddress"

  VLAN 开关属性
    Intel/ASIX/Realtek : RegistryKeyword=*PriorityVLANTag 或 PriorityVLANTag
                         中文系统 DisplayName="数据包优先级和 VLAN"，英文="Packet Priority & VLAN"
                         合法值：0=禁用 / 1=只启用数据包优先级 / 2=启用 VLAN / 3=两者都启用
                         ⚠️ 值 1 不打 VLAN 标签，绝不能用它"启用 VLAN"

  VLAN ID 属性
    ASIX     : RegistryKeyword=VLAN_ID    , DisplayName="VLAN标识"
    Realtek  : RegistryKeyword=RegVlanID  , DisplayName="VLAN ID"
    Intel I219-V : 根本没有该属性（驱动不支持单独设置 VLAN ID）

结论：**只有 RegistryKeyword 是稳定的**，DisplayName 与 ValidDisplayValues 都会随
厂商和系统语言变化，任何"名字列表"都不可能覆盖（例如 RegVlanID 这种关键字猜不到）。

本模块的做法
------------
1. 一次 PowerShell 调用取出该网卡全部高级属性（JSON），按 RegistryKeyword 三级匹配：
   ① driver_profiles.json 中按驱动型号写死的关键字（用户可自行添加，无需改代码）
   ② 内置关键字候选表（覆盖主流厂商）
   ③ DisplayName 多语言正则兜底（含中文/英文/繁体写法）
2. 写值一律用 `-RegistryValue`（数值/字符串），并通过
   ValidDisplayValues + ValidRegistryValues 的平行数组做"语义值 → 数值"映射，
   彻底摆脱中英文显示名差异。
3. 恢复默认用 `Get-NetAdapterAdvancedProperty | Reset-NetAdapterAdvancedProperty`，
   不再猜测 "--" 之类的魔法字符串。
4. 所有写入合并成 1 次 PowerShell 调用，避免每次写属性都重启一个 powershell.exe
   （原实现最坏情况会拉起几十个进程，一次配置要 10~30 秒）。
"""

import base64
import json
import os
import re
import subprocess
import sys
import time

# ----------------------------------------------------------------------------
# 常量配置
# ----------------------------------------------------------------------------

def app_dir():
    """
    程序所在目录——所有「用户可编辑/可写」的文件都放这里。

    PyInstaller 打包后 __file__ 指向临时解包目录（每次启动都不同），必须改用 exe
    所在目录，否则 configs.json / driver_profiles.json 会读不到或写进临时目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


_HERE = app_dir()
PROFILE_FILE = os.path.join(_HERE, "driver_profiles.json")
# 运行时自动学习出的档案（每台机器独有，不进仓库）；优先级高于上面的公共档案
LOCAL_PROFILE_FILE = os.path.join(_HERE, "driver_profiles.local.json")

# 角色 -> RegistryKeyword 候选（按优先级排列；比较时忽略大小写与前导 *）
ROLE_KEYWORDS = {
    "mac": [
        "NetworkAddress", "NetworkAddress2", "LocallyAdministeredAddress",
        "EthernetAddress", "MACAddress", "PhysicalAddress",
    ],
    "vlan_mode": [
        "*PriorityVLANTag", "PriorityVLANTag", "*VlanTag", "VlanTag",
        "PacketPriorityVlanTag", "VlanTagging", "VLanTag",
    ],
    "vlan_id": [
        "VLAN_ID", "RegVlanID", "VlanID", "*VlanID", "VLANID", "VlanId", "VLanID",
        "VlanTagId", "*VlanId",
    ],
}

# 角色 -> 中文文档名（用于日志）
ROLE_LABEL = {
    "mac": "MAC/网络地址",
    "vlan_mode": "VLAN 开关",
    "vlan_id": "VLAN ID",
}

# 虚拟/非物理网卡的描述关键字（用于过滤下拉框，避免误选）
VIRTUAL_DESC_RE = re.compile(
    r"(wan\s*miniport|bluetooth|蓝牙|wi-?fi\s*direct|kernel\s*debug|内核调试|"
    r"virtualbox|hyper-?v|vmware|tap-?windows|loopback|回环|npcap|wintun|"
    r"teredo|isatap|ras\s*async|sangfor|easyconnect|openvpn|forticlient|"
    r"anyconnect|proton|zerotier|tailscale)",
    re.IGNORECASE,
)

_ENABLE_RE = re.compile(r"(enable|enabled|启用|已启用|开启|開啟|开启|打开|開啟)")
_DISABLE_RE = re.compile(r"(disable|disabled|禁用|已禁用|停用|关闭|關閉)")
_PS_TIMEOUT = 60


# ----------------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------------

def as_log(target):
    """
    把日志目标统一成 log(msg) 可调用对象。

    兼容旧接口：如果传入的是 Tk Text 控件（旧版 main.py 的用法），
    会自动包装成"直接写控件"的函数，保证老调用点不会崩。
    新版 main.py 传入的是线程安全的队列日志器。
    """
    if callable(target):
        return target
    if hasattr(target, "insert"):  # Tk Text / ScrolledText
        def _log(msg):
            try:
                text = msg if str(msg).endswith("\n") else str(msg) + "\n"
                target.insert("end", text)
                target.see("end")
                target.update_idletasks()
            except Exception:
                pass
        return _log
    return lambda msg: print(msg)


def _ps_str(value):
    """把 Python 字符串转成 PowerShell 单引号字面量（内部单引号转义为两个）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _norm_kw(keyword):
    """规范化 RegistryKeyword：去空白、去前导 *、转小写。"""
    if keyword is None:
        return ""
    return str(keyword).strip().lstrip("*").lower()


def _as_list(value):
    """ConvertTo-Json 会把单元素数组退化成标量，这里统一成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_ps(script, timeout=_PS_TIMEOUT):
    """
    执行 PowerShell 脚本，返回 (returncode, stdout, stderr)。

    使用 -EncodedCommand（UTF-16LE + Base64），彻底规避引号、空格、中文、
    重定向编码等一切转义问题；输出强制 UTF-8。
    """
    prelude = (
        "$ProgressPreference='SilentlyContinue'; "
        "$ConfirmPreference='None'; "  # GUI 无控制台，禁止任何 cmdlet 弹出确认提示
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "try { $OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}; "
    )
    payload = base64.b64encode((prelude + script).encode("utf-16-le")).decode("ascii")
    cmd = [
        "powershell", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", payload,
    ]
    kwargs = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        out_b, err_b = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        return 1, "", "PowerShell 执行超时（%ss）" % timeout
    except Exception as exc:  # PowerShell 不存在 / 被策略拦截
        return 1, "", "无法启动 PowerShell: %s" % exc

    out = out_b.decode("utf-8", errors="replace") if isinstance(out_b, (bytes, bytearray)) else str(out_b or "")
    err = err_b.decode("utf-8", errors="replace") if isinstance(err_b, (bytes, bytearray)) else str(err_b or "")
    # PowerShell 会把 CLIXML 进度流混进 stderr，正常成功时直接忽略
    return proc.returncode, out.strip(), err.strip()


def run_ps_json(script, timeout=_PS_TIMEOUT):
    """执行 PowerShell 并解析其输出的 JSON；失败时返回 {'ok': False, 'error': ...}。"""
    rc, out, err = run_ps(script, timeout=timeout)
    text = (out or "").strip()
    if text:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            return {"ok": True, "data": data}
        except Exception:
            for line in reversed(text.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except Exception:
                        continue
    reason = text or err or ("PowerShell 返回码 %s" % rc)
    return {"ok": False, "error": reason[:600]}


# ----------------------------------------------------------------------------
# 驱动档案（driver_profiles.json）
# ----------------------------------------------------------------------------

def _read_profile_file(path):
    """读单个档案文件，返回 {驱动型号: {角色: 关键字}}。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}
    profiles = raw.get("profiles", raw) if isinstance(raw, dict) else {}
    if not isinstance(profiles, dict):
        return {}
    return {k: v for k, v in profiles.items()
            if not str(k).startswith("_") and isinstance(v, dict)}


def load_profiles(path=None):
    """
    读取驱动档案。path 为 None 时合并两个文件（本地自动学习的优先）：

      ① driver_profiles.local.json —— 程序运行时自动探测/校验成功后落盘，每台机器独有，
         用户完全无感知；插过的网卡下次直接命中。
      ② driver_profiles.json       —— 随程序分发的公共档案，可手工补充、可分享给同事。

    结构：
        {
          "profiles": {
            "ASIX USB to Gigabit Ethernet Family Adapter": {
              "mac": "NetworkAddress", "vlan_mode": "*PriorityVLANTag", "vlan_id": "VLAN_ID"
            },
            "某驱动型号": {"vlan_id": null}      # null = 明确声明该驱动不支持此角色
          }
        }
    """
    if path is not None:
        return _read_profile_file(path)
    merged = _read_profile_file(PROFILE_FILE)
    merged.update(_read_profile_file(LOCAL_PROFILE_FILE))
    return merged


def persist_profile(description, role_keywords, path=LOCAL_PROFILE_FILE, interface=None):
    """
    把某张网卡识别成功的关键字写入本地档案，实现「用过一次就永远记住，用户零操作」。

    返回 True 表示本次确实写入且内容有更新；False 表示无变化或写入失败。
    任何异常都被吞掉——绝不能让档案写入影响配置流程。
    """
    description = (description or "").strip()
    learned = {role: kw for role, kw in (role_keywords or {}).items() if kw}
    if not description or not learned:
        return False
    try:
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle) or {}
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        profiles = data.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        entry = profiles.get(description)
        if not isinstance(entry, dict):
            entry = {}
        changed = any(entry.get(role) != kw for role, kw in learned.items())
        entry.update(learned)
        profiles[description] = entry
        data["profiles"] = profiles

        meta = data.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
        record = meta.get(description)
        if not isinstance(record, dict):
            record = {}
        interfaces = record.get("interfaces")
        if not isinstance(interfaces, list):
            interfaces = []
        if interface and interface not in interfaces:
            interfaces.append(interface)
        record["interfaces"] = interfaces
        record["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        meta[description] = record
        data["_meta"] = meta

        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return changed
    except Exception:
        return False


# ----------------------------------------------------------------------------
# 网卡信息
# ----------------------------------------------------------------------------

def list_adapters(include_virtual=False):
    """
    列出本机网卡（一次 PowerShell 调用）。返回列表，元素为 dict：
        name / description / status / mac / speed / media / virtual / index
    PowerShell 或 CIM 不可用时返回空列表，调用方应回退到 psutil。
    """
    script = r"""
try {
    $items = @(Get-NetAdapter -ErrorAction Stop | Select-Object `
        Name, InterfaceDescription, Status, MacAddress, LinkSpeed, MediaType, Virtual, ifIndex)
    @{ ok = $true; adapters = @($items) } | ConvertTo-Json -Depth 5 -Compress
} catch {
    @{ ok = $false; error = $_.Exception.Message; adapters = @() } | ConvertTo-Json -Depth 5 -Compress
}
"""
    data = run_ps_json(script, timeout=30)
    if not data.get("ok"):
        return []
    adapters = []
    for item in _as_list(data.get("adapters")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "").strip()
        if not name:
            continue
        description = str(item.get("InterfaceDescription") or "").strip()
        is_virtual = bool(item.get("Virtual")) or bool(VIRTUAL_DESC_RE.search(description))
        adapters.append({
            "name": name,
            "description": description,
            "status": str(item.get("Status") or "").strip(),
            "mac": str(item.get("MacAddress") or "").strip(),
            "speed": str(item.get("LinkSpeed") or "").strip(),
            "media": str(item.get("MediaType") or "").strip(),
            "virtual": is_virtual,
            "index": item.get("ifIndex"),
        })
    if not include_virtual:
        adapters = [a for a in adapters if not a["virtual"]]
    order = {"Up": 0, "Disconnected": 1, "Disabled": 2}
    adapters.sort(key=lambda a: (order.get(a["status"], 9), a["name"]))
    return adapters


def _discover_advanced(iface, include_hidden=True):
    """取某网卡的全部高级属性（含隐藏项）。"""
    hidden = " -IncludeHidden" if include_hidden else ""
    script = r"""
$name = __NAME__
try {
    $props = @(Get-NetAdapterAdvancedProperty -Name $name__HIDDEN__ -ErrorAction Stop |
        Select-Object DisplayName, RegistryKeyword, RegistryValue, DisplayValue,
                      ValidDisplayValues, ValidRegistryValues)
    $hw = @(Get-NetAdapter -Name $name -ErrorAction SilentlyContinue |
        Select-Object Name, InterfaceDescription, Status, MacAddress, LinkSpeed)
    @{ ok = $true; props = @($props); adapter = @($hw) } | ConvertTo-Json -Depth 6 -Compress
} catch {
    @{ ok = $false; error = $_.Exception.Message; props = @(); adapter = @() } | ConvertTo-Json -Depth 6 -Compress
}
"""
    script = script.replace("__NAME__", _ps_str(iface)).replace("__HIDDEN__", hidden)
    data = run_ps_json(script)
    if not data.get("ok") and include_hidden:
        # 个别老系统不支持 -IncludeHidden
        return _discover_advanced(iface, include_hidden=False)
    return data


class AdapterInspector:
    """
    网卡高级属性解析器：一次探测，三级匹配，暴露 mac / vlan_mode / vlan_id 三个角色属性。
    """

    def __init__(self, iface, profiles=None, discover=True):
        self.iface = iface
        self.profiles = load_profiles() if profiles is None else profiles
        self.ok = False
        self.error = ""
        self.props = []
        self.adapter = {}
        self.description = ""
        self.profile_name = None
        self.profile = {}
        self._roles = {}
        self._role_source = {}
        if discover:
            self.discover()

    # ---------- 探测 ----------
    def discover(self):
        data = _discover_advanced(self.iface)
        if not data.get("ok"):
            self.ok = False
            self.error = str(data.get("error") or "未知错误")
            return self
        self.ok = True
        self.props = [p for p in _as_list(data.get("props")) if isinstance(p, dict)]
        adapter_list = _as_list(data.get("adapter"))
        self.adapter = adapter_list[0] if adapter_list and isinstance(adapter_list[0], dict) else {}
        self.description = str(self.adapter.get("InterfaceDescription") or "").strip()
        self.profile_name, self.profile = self._match_profile()
        for role in ROLE_KEYWORDS:
            self._roles[role] = self._resolve_role(role)
        return self

    def _match_profile(self):
        """
        按驱动型号匹配档案。

        匹配顺序（重要）：字面量整串 → 正则整串 → 字面量子串 → 正则子串（子串取最长者）。
        之所以字面量优先：驱动型号几乎都带括号（如 Intel(R) ... (16) I219-V），
        若直接当正则处理，括号会变成分组导致永远匹配不上；字面量优先则用户可原样粘贴。
        """
        candidates = [t for t in ((self.description or ""), (self.iface or "")) if t]
        lowered = [(text, text.lower()) for text in candidates]
        best = (None, {})

        # ① 字面量整串匹配
        for pattern, conf in self.profiles.items():
            low = str(pattern).lower()
            for _text, text_low in lowered:
                if low == text_low:
                    return pattern, conf

        # ② 正则整串匹配
        for pattern, conf in self.profiles.items():
            for text, _text_low in lowered:
                try:
                    if re.fullmatch(pattern, text, re.IGNORECASE):
                        return pattern, conf
                except re.error:
                    pass

        # ③ 字面量子串匹配
        for pattern, conf in self.profiles.items():
            low = str(pattern).lower()
            if not low:
                continue
            for _text, text_low in lowered:
                if low in text_low and (best[0] is None or len(low) > len(best[0])):
                    best = (pattern, conf)

        # ④ 正则子串匹配
        for pattern, conf in self.profiles.items():
            for text, _text_low in lowered:
                try:
                    if re.search(pattern, text, re.IGNORECASE) and (
                            best[0] is None or len(pattern) > len(best[0])):
                        best = (pattern, conf)
                except re.error:
                    pass
        return best

    # ---------- 四级匹配 ----------
    def _resolve_role(self, role):
        prop, source = self._resolve_role_best(role)
        self._role_source[role] = source
        return prop

    def _resolve_role_best(self, role):
        """返回 (属性, 来源)。来源用于日志展示与是否需要落盘学习。"""
        # ① 驱动档案指定（null / "" 表示该驱动不支持此角色）
        if role in self.profile:
            keyword = self.profile.get(role)
            if keyword is None or str(keyword).strip() == "":
                return None, "unsupported"
            wanted = _norm_kw(keyword)
            for prop in self.props:
                if _norm_kw(prop.get("RegistryKeyword")) == wanted:
                    return prop, "profile"
            for prop in self.props:  # 允许档案写子串
                if wanted and wanted in _norm_kw(prop.get("RegistryKeyword")):
                    return prop, "profile"
        # ② 内置关键字候选表（按候选顺序取最优）
        best, best_rank = None, 10 ** 6
        for prop in self.props:
            rank = self._keyword_rank(role, prop.get("RegistryKeyword"))
            if rank is not None and rank < best_rank:
                best, best_rank = prop, rank
        if best is not None:
            return best, "keyword"
        # ③ 显示名多语言匹配
        for prop in self.props:
            if _display_match(role, prop.get("DisplayName")):
                return prop, "display"
        # ④ 语义推断：不看具体名字，只看关键字片段 + 属性类型 + 取值域
        inferred = _semantic_infer(role, self.props)
        if inferred:
            return inferred[0], "inferred"
        return None, "missing"

    def role_source(self, role):
        """该角色是靠什么识别出来的：profile/keyword/display/inferred/unsupported/missing。"""
        return self._role_source.get(role, "missing")

    def role_candidates(self, role):
        """
        返回该角色的有序候选属性列表（按 RegistryKeyword 去重）。

        顺序：档案/内置表/显示名命中的首选 → 内置表其它命中 → 显示名命中 → 语义推断命中。
        用途：写入失败或回读校验不一致时，自动换下一个候选重试——这就是"猜不到就试出来"。
        """
        if role in self.profile and (self.profile.get(role) is None
                                     or str(self.profile.get(role)).strip() == ""):
            return []  # 档案明确声明该驱动不支持此角色

        result, seen = [], set()

        def _add(prop):
            if not isinstance(prop, dict):
                return
            key = _norm_kw(prop.get("RegistryKeyword"))
            if not key or key in seen:
                return
            seen.add(key)
            result.append(prop)

        _add(self.prop(role))
        for rank in range(len(ROLE_KEYWORDS.get(role, []))):
            for prop in self.props:
                if self._keyword_rank(role, prop.get("RegistryKeyword")) == rank:
                    _add(prop)
        for prop in self.props:
            if _display_match(role, prop.get("DisplayName")):
                _add(prop)
        for prop in _semantic_infer(role, self.props):
            _add(prop)
        return result

    @staticmethod
    def _keyword_rank(role, keyword):
        norm = _norm_kw(keyword)
        if not norm:
            return None
        for index, cand in enumerate(ROLE_KEYWORDS.get(role, [])):
            if _norm_kw(cand) == norm:
                return index
        return None

    # ---------- 对外属性 ----------
    def prop(self, role):
        return self._roles.get(role)

    def role_keyword(self, role):
        prop = self._roles.get(role)
        return str(prop.get("RegistryKeyword")) if prop else None

    def role_display(self, role):
        prop = self._roles.get(role)
        return str(prop.get("DisplayName")) if prop else None

    @property
    def mac_prop(self):
        return self.prop("mac")

    @property
    def vlan_mode_prop(self):
        return self.prop("vlan_mode")

    @property
    def vlan_id_prop(self):
        return self.prop("vlan_id")

    def current_value(self, role):
        prop = self.prop(role)
        if not prop:
            return None
        return prop.get("DisplayValue", prop.get("RegistryValue"))


# ----------------------------------------------------------------------------
# 取值语义映射
# ----------------------------------------------------------------------------

def _display_match(role, display_name):
    """DisplayName 多语言兜底匹配（中/英/繁体）。"""
    if not display_name:
        return False
    low = str(display_name).strip().lower()
    compact = re.sub(r"[\s\u00a0_\-]+", "", low)
    if role == "mac":
        hints = (
            "networkaddress", "网络地址", "網路位址", "本地管理地址", "本机管理地址",
            "本機管理位址", "locallyadministered", "macaddress", "physicaladdress",
        )
        return any(h in compact for h in hints) or "管理地址" in compact or "管理位址" in compact
    if role == "vlan_id":
        has_vlan = ("vlan" in compact) or ("标识" in compact) or ("標識" in compact)
        has_id = ("id" in compact) or ("标识" in compact) or ("標識" in compact) \
            or ("标签" in compact) or ("標籤" in compact) or ("tag" in compact)
        return has_vlan and has_id
    if role == "vlan_mode":
        has_vlan = "vlan" in compact
        has_tag = ("priority" in compact) or ("优先" in compact) or ("優先" in compact) \
            or ("packet" in compact) or ("tag" in compact)
        return has_vlan and has_tag
    return False


# ----------------------------------------------------------------------------
# 语义推断：完全不比对固定名字，只看「关键字段 + 属性类型 + 取值域」
# ----------------------------------------------------------------------------

# MAC 属性的排除词（含这些词的多半是 IP / DNS / 唤醒 / 分载 等其它属性）
_MAC_EXCLUDE_RE = re.compile(
    r"(ipv4|ipv6|dns|gateway|mask|wake|wol|arp|checksum|offload|jumbo|flow|buffer|"
    r"power|vlan|speed|duplex|moderation|timestamp)",
    re.IGNORECASE,
)
_VLAN_PRIORITY_RE = re.compile(r"(priority|优先|優先|packetpriority)", re.IGNORECASE)


def _is_enum_prop(prop):
    """枚举型属性（带合法取值列表）；NetworkAddress 这类 edit 型属性没有取值列表。"""
    return bool(_as_list(prop.get("ValidDisplayValues"))) or \
        bool(_as_list(prop.get("ValidRegistryValues")))


def _looks_like_vlan_id(keyword, display):
    kw = _norm_kw(keyword)
    low = str(display or "").lower()
    compact = re.sub(r"[\s\u00a0_\-]+", "", low)
    if "vlan" not in kw and "vlan" not in compact \
            and "标识" not in compact and "標識" not in compact:
        return False
    if "id" in kw or "标识" in compact or "標識" in compact:
        return True
    return bool(re.search(r"\bid\b", low))


def _semantic_infer(role, props):
    """
    语义推断（第四级，兜底中的兜底）。

    不比对任何固定名字，而是按「关键字段 + 属性类型 + 取值域」判断，因此
    RegVlanID / VLAN_ID / VlanTagId / *PriorityVLANTag / PriorityVLANTag 这类
    五花八门的关键字都能命中，不需要人工维护列表：

      VLAN ID   : 名字含 vlan 且不是"优先级"类；名字含 id/标识（整数型优先）
      VLAN 开关 : 名字含 vlan 且是枚举型（有合法取值）；优先级类(VLAN Tag)排最前
      MAC       : 编辑型(非枚举) 且名字含 address/地址，且不含 IP/DNS/唤醒/分载 等排除词
    """
    found = []
    for prop in props:
        keyword = _norm_kw(prop.get("RegistryKeyword"))
        display = str(prop.get("DisplayName") or "")
        low = display.lower()
        compact = re.sub(r"[\s\u00a0_\-]+", "", low)
        has_vlan = ("vlan" in keyword) or ("vlan" in compact) \
            or ("标识" in compact) or ("標識" in compact)
        is_enum = _is_enum_prop(prop)

        if role == "vlan_id":
            if not has_vlan or _VLAN_PRIORITY_RE.search(keyword) \
                    or _VLAN_PRIORITY_RE.search(compact):
                continue
            if _looks_like_vlan_id(keyword, display):
                found.append(prop)
            elif (not is_enum) and ("tag" in keyword or "tag" in compact):
                found.append(prop)

        elif role == "vlan_mode":
            if not has_vlan or not is_enum:
                continue
            if _VLAN_PRIORITY_RE.search(keyword) or _VLAN_PRIORITY_RE.search(compact):
                found.append(prop)      # *PriorityVLANTag 这一类，最可靠的 VLAN 开关
            elif _looks_like_vlan_id(keyword, display):
                continue                # 更像 VLAN ID，不是开关
            elif "tag" in keyword or "tag" in compact or "enable" in keyword:
                found.append(prop)

        elif role == "mac":
            if is_enum:
                continue
            if _MAC_EXCLUDE_RE.search(keyword) or _MAC_EXCLUDE_RE.search(compact):
                continue
            if ("address" in keyword) or ("address" in compact) \
                    or ("地址" in compact) or ("位址" in compact):
                found.append(prop)

    def _score(prop):
        keyword = _norm_kw(prop.get("RegistryKeyword")).replace("_", "")
        if role == "mac":
            if "networkaddress" in keyword:
                return 0
            return 1 if "address" in keyword else 2
        if role == "vlan_id":
            if "vlanid" in keyword:
                return 0
            return 1 if "id" in keyword else 2
        if role == "vlan_mode":
            if "priorityvlantag" in keyword:
                return 0
            return 1 if "vlantag" in keyword else 2
        return 3

    return sorted(found, key=_score)


# 识别来源的中文标注（日志用）
SOURCE_LABEL = {
    "profile": "驱动档案",
    "keyword": "内置关键字表",
    "display": "显示名多语言匹配",
    "inferred": "语义推断（自动学习）",
    "unsupported": "档案标注：该驱动不支持",
    "missing": "未找到",
}


def choose_vlan_mode_value(prop, enable=True):
    """
    在 VLAN 开关属性的合法取值中，挑出"启用 VLAN 标签"/"禁用"对应的 RegistryValue。

    返回 (registry_value, display_text)；找不到时返回 (None, None)。
    规则（重要）：
      * 只有显示名里同时出现 vlan 与"启用/Enabled"（且不含"禁用/Disabled"）才算启用，
        这样中文系统下不会误选"已启用数据包优先级"(值=1，不打 VLAN 标签)。
      * 数值上优先 3=Packet Priority & VLAN Enabled，其次 2=VLAN Enabled。
    """
    if not prop:
        return None, None
    displays = [str(x) for x in _as_list(prop.get("ValidDisplayValues"))]
    regs = _as_list(prop.get("ValidRegistryValues"))

    picked = []
    for index, text in enumerate(displays):
        low = text.strip().lower()
        compact = re.sub(r"[\s\u00a0_\-]+", "", low)
        if "vlan" not in compact:
            continue  # "Packet Priority Enabled"(值=1) / "已启用数据包优先级" 在此被排除
        if enable:
            if _ENABLE_RE.search(low) and not _DISABLE_RE.search(low):
                picked.append((index, _to_int(regs[index]) if index < len(regs) else None))
        else:
            if _DISABLE_RE.search(low):
                picked.append((index, _to_int(regs[index]) if index < len(regs) else None))

    if picked:
        preferred = (3, 2) if enable else (0,)
        for want in preferred:
            for index, value in picked:
                if value == want:
                    return value, displays[index]
        index, value = picked[0]
        return value, displays[index]

    # 没有显示名信息（纯数值型驱动）：按约定兜底
    numbers = [n for n in (_to_int(r) for r in regs) if n is not None]
    if enable:
        for want in (3, 2, 1):
            if want in numbers:
                return want, "RegistryValue=%s" % want
    else:
        if 0 in numbers:
            return 0, "RegistryValue=0"
        if numbers:
            return min(numbers), "RegistryValue=%s" % min(numbers)
    return None, None


def normalize_mac(mac):
    """把任意写法的 MAC 规范成 12 位小写十六进制；非法时返回 None。"""
    if mac is None:
        return None
    norm = re.sub(r"[^0-9a-fA-F]", "", str(mac)).lower()
    if len(norm) != 12:
        return None
    return norm


def mac_warning(mac_norm):
    """检查 MAC 的合法性（组播位/本地管理位），返回警告文本或 None。"""
    if not mac_norm or len(mac_norm) != 12:
        return None
    first = int(mac_norm[0:2], 16)
    if first & 0x01:
        return "该 MAC 是组播地址（第一字节最低位为 1），网卡通常不接受，请确认配置"
    if not (first & 0x02):
        return "该 MAC 未设置本地管理位(LAA, 第一字节 bit1)，部分驱动会拒绝写入"
    return None


# ----------------------------------------------------------------------------
# 属性写入（批量）
# ----------------------------------------------------------------------------

def apply_property_ops(iface, ops, timeout=120):
    """
    批量写入网卡高级属性（一次 PowerShell 调用）。

    ops: [{"role": "mac", "kw": "NetworkAddress", "mode": "set", "val": "0a0b0c026969"},
          {"role": "vlan_id", "kw": "VLAN_ID", "mode": "set", "val": 105},
          {"role": "mac", "kw": "NetworkAddress", "mode": "reset"}]
    返回 [{"role","kw","ok","msg"}]。
    """
    if not ops:
        return []
    payload = json.dumps(ops, ensure_ascii=False)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    script = r"""
$name = __NAME__
$json = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('__B64__'))
# 注意：PowerShell 5.1 的 ConvertFrom-Json 对顶层数组会整体返回一个对象，
# 不能写 @($json | ConvertFrom-Json)（会得到 1 个元素，属性变成数组），必须显式赋值后再 foreach。
$parsed = ConvertFrom-Json -InputObject $json
$results = @()
foreach ($op in $parsed) {
    $readback = $null
    try {
        if ($op.mode -eq 'reset') {
            Get-NetAdapterAdvancedProperty -Name $name -RegistryKeyword $op.kw -IncludeHidden -ErrorAction Stop |
                Reset-NetAdapterAdvancedProperty -NoRestart -Confirm:$false -ErrorAction Stop
        } else {
            Set-NetAdapterAdvancedProperty -Name $name -RegistryKeyword $op.kw -RegistryValue $op.val -NoRestart -ErrorAction Stop
        }
        $one = Get-NetAdapterAdvancedProperty -Name $name -RegistryKeyword $op.kw -IncludeHidden -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($one) { $readback = [string]$one.RegistryValue }
        $results += @{ role = [string]$op.role; kw = [string]$op.kw; ok = $true; msg = ''; readback = $readback }
    } catch {
        $results += @{ role = [string]$op.role; kw = [string]$op.kw; ok = $false; msg = $_.Exception.Message; readback = $null }
    }
}
@{ ok = $true; results = @($results) } | ConvertTo-Json -Depth 6 -Compress
"""
    script = script.replace("__NAME__", _ps_str(iface)).replace("__B64__", b64)
    data = run_ps_json(script, timeout=timeout)
    if not data.get("ok"):
        return [{"role": op.get("role", "?"), "kw": op.get("kw", "?"), "ok": False,
                 "msg": str(data.get("error") or "批量写入失败"),
                 "readback": None, "verified": False} for op in ops]
    results = [r for r in _as_list(data.get("results")) if isinstance(r, dict)]
    if not results:
        results = [{"role": op.get("role", "?"), "kw": op.get("kw", "?"), "ok": False,
                    "msg": "未返回结果"} for op in ops]
    # 写进去还不够：回读注册表值，确认真的变成了我们写入的值
    for index, item in enumerate(results):
        op = ops[index] if index < len(ops) else {}
        item.setdefault("readback", None)
        item["verified"] = bool(item.get("ok")) and _readback_matches(op, item.get("readback"))
    return results


def _readback_matches(op, readback):
    """回读值是否等于写入值（忽略大小写与分隔符；恢复默认只需写入未报错）。"""
    if op.get("mode") == "reset":
        return True
    want = op.get("val")
    if want is None:
        return True
    clean = lambda value: re.sub(r"[^0-9a-z]", "", str(value).lower())  # noqa: E731
    got_clean, want_clean = clean(readback), clean(want)
    if not got_clean:
        return False
    return got_clean == want_clean or want_clean in got_clean


def _build_op(request, prop):
    """按请求与候选属性构造一次写入操作；无法构造时返回 None。"""
    keyword = prop.get("RegistryKeyword")
    if not keyword:
        return None
    kind = request.get("kind", "set")
    if kind == "reset":
        return {"role": request["role"], "kw": keyword, "mode": "reset", "verify": True}
    if kind == "set":
        return {"role": request["role"], "kw": keyword, "mode": "set",
                "val": request.get("value"), "verify": True}
    if kind in ("enable", "disable"):
        value, text = choose_vlan_mode_value(prop, enable=(kind == "enable"))
        if value is None:
            if request.get("fallback_reset"):
                return {"role": request["role"], "kw": keyword, "mode": "reset", "verify": True}
            return None
        return {"role": request["role"], "kw": keyword, "mode": "set",
                "val": value, "verify": True, "text": text}
    return None


def try_roles(iface, inspector, requests, log=None, max_attempts=3, timeout=140):
    """
    按候选顺序试写角色属性，写入后回读校验，失败自动换下一个候选——"猜不到就试出来"。

    requests: [{"role": "mac", "kind": "set", "value": "0a0b0c026969"},
               {"role": "vlan_mode", "kind": "enable"},
               {"role": "vlan_id", "kind": "set", "value": 105}]
              kind 可为 set / reset / enable / disable。

    返回 {role: {"ok", "keyword", "readback", "source", "message", ...}}。
    多个请求会合并成一次 PowerShell 调用；只有失败的才进入下一轮候选。
    """
    log = as_log(log) if log is not None else (lambda message: None)
    results = {}
    pending = [dict(request) for request in requests]
    for attempt in range(max_attempts):
        if not pending:
            break
        ops, groups, retry = [], [], []
        for request in pending:
            role = request["role"]
            candidates = inspector.role_candidates(role)
            if attempt >= len(candidates):
                continue  # 该角色没有第 attempt 个候选，稍后按失败处理
            prop = candidates[attempt]
            op = _build_op(request, prop)
            if op is None:
                if attempt + 1 < len(candidates):
                    retry.append(request)
                continue
            ops.append(op)
            groups.append((request, prop, op))
        if not ops:
            pending = retry
            continue
        batch = apply_property_ops(iface, ops, timeout=timeout)
        for (request, prop, op), item in zip(groups, batch):
            role = request["role"]
            keyword = op.get("kw")
            if item.get("ok") and item.get("verified"):
                results[role] = {
                    "role": role, "ok": True, "keyword": keyword,
                    "display": prop.get("DisplayName"), "value": op.get("val"),
                    "mode": op.get("mode"), "readback": item.get("readback"),
                    "text": op.get("text"), "source": inspector.role_source(role),
                    "attempt": attempt, "message": "",
                }
                log("   ✅ %s → %s 写入成功且回读一致%s"
                    % (ROLE_LABEL.get(role, role), keyword,
                       "" if attempt == 0 else "（第 %d 个候选）" % (attempt + 1)))
            else:
                reason = item.get("msg") or "回读值 %r 与写入值不一致" % (item.get("readback"),)
                log("   ⚠️ %s → %s 未生效: %s" % (ROLE_LABEL.get(role, role), keyword, reason))
                if attempt + 1 < len(inspector.role_candidates(role)):
                    retry.append(request)
                else:
                    results[role] = {"role": role, "ok": False, "keyword": keyword,
                                     "source": inspector.role_source(role), "message": reason}
        pending = retry
    for request in pending:
        role = request["role"]
        if role not in results:
            results[role] = {"role": role, "ok": False, "keyword": None,
                             "source": inspector.role_source(role),
                             "message": "所有候选属性都未通过写入/回读校验"}
    return results


# ----------------------------------------------------------------------------
# 一键适配：探测 + 语义推断 + 自动落盘学习（不改动网卡任何设置）
# ----------------------------------------------------------------------------

def adapt_adapter(iface, log=None, inspector=None):
    """
    一键适配此网卡：探测驱动属性 → 逐角色识别（档案/关键字表/显示名/语义推断）
    → 把识别结果写入本地档案 driver_profiles.local.json。

    整个过程**不修改网卡任何设置**，纯读操作；用户下一次点【应用配置】即可直接生效。
    返回 {"ok", "description", "keywords": {...}, "sources": {...}, "written"}。
    """
    log = as_log(log) if log is not None else (lambda message: None)
    inspector = inspector or AdapterInspector(iface)
    if not inspector.ok:
        log("⚠️ 探测失败: %s" % inspector.error)
        return {"ok": False, "error": inspector.error}

    log("网卡: %s ｜ 接口: %s" % (inspector.description or "未知", iface))
    learned, sources = {}, {}
    for role in ("mac", "vlan_mode", "vlan_id"):
        prop = inspector.prop(role)
        source = inspector.role_source(role)
        sources[role] = source
        if prop:
            learned[role] = prop.get("RegistryKeyword")
            log("  %s: %s（来源: %s ｜ 显示名: %s）"
                % (ROLE_LABEL[role], prop.get("RegistryKeyword"),
                   SOURCE_LABEL.get(source, source), prop.get("DisplayName")))
        else:
            log("  %s: 该驱动未提供（%s）" % (ROLE_LABEL[role], SOURCE_LABEL.get(source, source)))

    written = persist_profile(inspector.description or iface, learned, interface=iface)
    if learned:
        log("本地档案 driver_profiles.local.json: %s"
            % ("已更新（下次插同一张网卡自动命中）" if written else "已是最新，无需更新"))
    else:
        log("⚠️ 未能识别出任何属性，请把导出的探测文件发给开发者补充档案")
    return {"ok": True, "description": inspector.description, "keywords": learned,
            "sources": sources, "written": written}


def restart_adapter(iface, wait_up=25, timeout=120):
    """
    重启网卡使 MAC / VLAN 等高级属性生效（优先 Restart-NetAdapter，失败退回 Disable+Enable），
    然后轮询等待链路 Up。返回 dict：ok / error / method / status / speed。
    """
    script = r"""
$name = __NAME__
$method = 'Restart-NetAdapter'
$err = ''
try {
    Restart-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop
} catch {
    $err = $_.Exception.Message
    $method = 'Disable/Enable'
    try {
        Disable-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop
        Start-Sleep -Seconds 2
        Enable-NetAdapter -Name $name -Confirm:$false -ErrorAction Stop
        $err = ''
    } catch {
        $err = $_.Exception.Message
    }
}
$deadline = (Get-Date).AddSeconds(__WAIT__)
$status = 'Unknown'
$speed = ''
do {
    Start-Sleep -Milliseconds 700
    $adapter = Get-NetAdapter -Name $name -ErrorAction SilentlyContinue
    if ($adapter) { $status = [string]$adapter.Status; $speed = [string]$adapter.LinkSpeed }
} while ($status -ne 'Up' -and (Get-Date) -lt $deadline)
@{ ok = ($err -eq ''); error = $err; method = $method; status = $status; speed = $speed } |
    ConvertTo-Json -Compress
"""
    script = script.replace("__NAME__", _ps_str(iface)).replace("__WAIT__", str(int(wait_up)))
    data = run_ps_json(script, timeout=timeout)
    if not isinstance(data, dict) or "ok" not in data:
        data = {"ok": False, "error": str(data.get("error") if isinstance(data, dict) else "重启失败"),
                "method": "?", "status": "Unknown", "speed": ""}
    return data


# ----------------------------------------------------------------------------
# 驱动属性探测导出（遇到新网卡时把结果发给开发者即可适配）
# ----------------------------------------------------------------------------

def export_probe(iface, out_dir=None):
    """
    导出指定网卡的全部高级属性到 JSON 文件，便于为新驱动补充关键字。
    返回 (文件路径, 数据字典)。
    """
    inspector = AdapterInspector(iface)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_iface = re.sub(r"[^\w\u4e00-\u9fa5\-]+", "_", str(iface))
    out_dir = out_dir or _HERE
    path = os.path.join(out_dir, "driver_probe_%s_%s.json" % (safe_iface, stamp))

    roles = {}
    for role in ROLE_KEYWORDS:
        prop = inspector.prop(role)
        roles[role] = {
            "RegistryKeyword": prop.get("RegistryKeyword") if prop else None,
            "DisplayName": prop.get("DisplayName") if prop else None,
            "DisplayValue": prop.get("DisplayValue") if prop else None,
            "RegistryValue": prop.get("RegistryValue") if prop else None,
            "ValidDisplayValues": _as_list(prop.get("ValidDisplayValues")) if prop else [],
            "ValidRegistryValues": _as_list(prop.get("ValidRegistryValues")) if prop else [],
        }

    payload = {
        "generated_at": stamp,
        "interface": iface,
        "ok": inspector.ok,
        "error": inspector.error,
        "adapter": inspector.adapter,
        "matched_profile": inspector.profile_name,
        "resolved_roles": roles,
        "all_advanced_properties": inspector.props,
        "profile_suggestion": {
            inspector.description or iface: {
                role: (roles[role]["RegistryKeyword"] or None) for role in ROLE_KEYWORDS
            }
        },
    }
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        return None, {"ok": False, "error": "写入失败: %s" % exc}
    return path, payload
