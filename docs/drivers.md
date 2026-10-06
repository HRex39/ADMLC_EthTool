# 网卡驱动适配说明

## 1. 为什么不能按"属性名字"适配

Windows 网卡的高级属性由各厂商驱动自己注册，**显示名（DisplayName）各不相同，而且随系统
语言本地化**。同一件事（改 MAC）在三种驱动上的显示名完全不同：

| 驱动 | MAC 属性显示名 |
|---|---|
| Intel Ethernet Connection I219-V | `本地管理地址` |
| ASIX USB to Gigabit Ethernet | `网络地址` |
| Realtek USB GbE | `Network Address` |
| USB2.0 Ethernet Adapter | `NetworkAddress` |

VLAN ID 更极端——关键字本身就因厂商而异：

| 驱动 | VLAN ID 关键字 | VLAN 开关关键字 |
|---|---|---|
| ASIX USB to Gigabit Ethernet | `VLAN_ID` | `*PriorityVLANTag` |
| Realtek USB GbE | `RegVlanID` | `*PriorityVLANTag` |
| USB2.0 Ethernet Adapter | 无 | `PriorityVLANTag`（注意没有 `*`） |
| Intel Ethernet Connection I219-V | **无此属性** | `*PriorityVLANTag` |

所以"穷举名字"的方案必然失败（`RegVlanID` 这种关键字猜不到）。本工具改为：

```
driver_profiles.json（按驱动型号写死关键字）
        ↓ 未命中
内置关键字候选表 ROLE_KEYWORDS（覆盖主流厂商）
        ↓ 未命中
DisplayName 多语言正则兜底（中/英/繁体）
```

写值一律使用 `-RegistryKeyword` + `-RegistryValue`（数值），
并通过 `ValidDisplayValues` 与 `ValidRegistryValues` 的**平行数组**做语义映射，
因此中文系统与英文系统行为完全一致。

## 2. VLAN 开关的取值陷阱（重要）

`*PriorityVLANTag` 的合法取值（本机实测，中文系统）：

| RegistryValue | 中文显示名 | 是否插入 VLAN 标签 |
|---|---|---|
| 0 | 已禁用数据包优先级和 VLAN | 否 |
| 1 | 已启用数据包优先级 | **否**（只启用优先级，不打标签） |
| 2 | 已启用 VLAN | 是 |
| 3 | 已启用数据包优先级和 VLAN | 是 |

旧版代码按 `"启用" in 显示名` 匹配，在中文系统上会优先命中**值 1**，于是"配置成功"但
VLAN 标签根本没打上，设备自然也连不上。本工具的规则是：

1. 显示名必须同时包含 `vlan` 与「启用/Enabled」，且不含「禁用/Disabled」；
2. 在候选中优先取数值 `3`，其次 `2`；
3. 绝不使用 `1`。

## 3. 阈值与不支持的网卡

识别不到某个角色时，工具会明确报出来而不是静默失败：

- `VLAN 开关 -> 该驱动未提供此属性`
- `VLAN ID -> 该网卡驱动未提供可设置的 VLAN ID 属性（例如 Intel I219-V）`

Intel I219-V 这类板载网卡确实无法通过驱动属性设置 VLAN ID，需要改用支持该属性的网卡，
或在交换机侧配置 VLAN。

## 4. 新增一张网卡的适配步骤（无需改代码）

1. 插入网卡，在界面下拉框中选中它（显示为「以太网 X — 驱动型号」）。
2. 点击【探测驱动属性】，程序会生成 `driver_probe_<网口>_<时间>.json`，其中包含：
   - `resolved_roles`：当前识别到的三个角色及其关键字；
   - `all_advanced_properties`：该网卡的全部高级属性（DisplayName / RegistryKeyword /
     RegistryValue / ValidDisplayValues / ValidRegistryValues）；
   - `profile_suggestion`：可以直接抄进档案的内容。
3. 打开 `driver_profiles.json`，把 `profile_suggestion` 合并进 `profiles`：

```json
"profiles": {
  "你的驱动型号（InterfaceDescription 原文）": {
    "mac": "NetworkAddress",
    "vlan_mode": "*PriorityVLANTag",
    "vlan_id": "VlanID"
  },
  "某驱动型号": { "vlan_id": null }
}
```

   - 键名按「字面量优先、正则兜底」匹配：写成驱动型号原文（含括号，如
     `Intel(R) Ethernet Connection (16) I219-V`）即可，**括号无需转义**；
     也支持正则，如 `"ASIX.*Gigabit"`。整串匹配优先于子串匹配；
   - 值为 `null` 表示该驱动确实没有这个属性，跳过并打印说明。
4. 保存后重新点击【应用配置】即可生效，**不需要修改任何 Python 代码**。

## 5. 相关 PowerShell 实现要点

- 探测：`Get-NetAdapterAdvancedProperty -Name <网口> -IncludeHidden | ConvertTo-Json`
- 写入：`Set-NetAdapterAdvancedProperty -Name <网口> -RegistryKeyword <关键字> -RegistryValue <值> -NoRestart`
- 恢复默认：`Get-NetAdapterAdvancedProperty ... -RegistryKeyword <关键字> | Reset-NetAdapterAdvancedProperty -NoRestart`
- 生效：`Restart-NetAdapter -Name <网口> -Confirm:$false`（失败退回 `Disable-NetAdapter` + `Enable-NetAdapter`）

所有脚本通过 `-EncodedCommand`（UTF-16LE + Base64）执行并强制 UTF-8 输出，
避免中文网卡名、引号与重定向编码问题；写入操作合并为**一次** PowerShell 调用，
一次配置的耗时从旧版的 10~30 秒降到约 1~3 秒。

## 6. 排查清单

| 现象 | 原因与处理 |
|---|---|
| 提示"该驱动未提供此属性" | 驱动确实不支持（如 I219-V 无 VLAN ID），或需要在档案中补充关键字 |
| MAC 写入成功但无效 | 网卡未重启；确认配置未设置 `"restart_adapter": false`，或手动点【重启网卡】 |
| 提示"该 MAC 未设置本地管理位(LAA)" | 首字节 bit1 必须为 1（如 `0A`、`02`），否则部分驱动拒绝写入 |
| 下拉框看不到网卡 | 该网卡被识别为虚拟网卡（VirtualBox/Hyper-V/蓝牙/Wi-Fi Direct）；或 PowerShell 不可用导致回退到 psutil 列表 |
