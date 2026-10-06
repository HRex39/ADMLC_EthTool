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

## 4. 人工适配步骤（最后手段，通常用不到）

正常情况下请优先用**第 5 节的自动适配**；只有自动识别也失败（驱动确实不提供该属性）
才需要下面这套人工流程：

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

## 5. 全自动适配（默认行为，用户零操作）

插上一张新网卡时，用户只需要点【一键适配此网卡】+【应用配置】，不需要编辑任何文件。

### 5.1 四级识别顺序

| 级别 | 依据 | 说明 |
|---|---|---|
| ① | `driver_profiles.local.json` | 本机自动学习出的档案（用过一次的网卡） |
| ② | `driver_profiles.json` | 随程序分发的公共档案 |
| ③ | 内置关键字候选表 | `NetworkAddress` / `*PriorityVLANTag` / `VLAN_ID` / `RegVlanID` / `VlanId` … |
| ④ | 显示名多语言匹配 | 中/英/繁体显示名，如「本地管理地址」「VLAN标识」「Packet Priority & VLAN」 |
| ⑤ | **语义推断** | 不看固定名字，只看「关键字段 + 属性类型 + 取值域」 |

### 5.2 语义推断规则（第五级兜底）

| 角色 | 判定规则 |
|---|---|
| MAC | 编辑型属性（无合法取值列表）且名字含 `address` / `地址`，并排除 IP/DNS/唤醒/分载 等干扰词 |
| VLAN 开关 | 枚举型属性（有合法取值列表）且名字含 `vlan`；`Priority/VLAN Tag` 类排最前 |
| VLAN ID | 名字含 `vlan` 且不是"优先级"类，并且名字含 `id` / `标识`（整数型优先） |

这样 `VLAN_ID`、`RegVlanID`、`VlanTagId`、`*PriorityVLANTag`、`PriorityVLANTag`
这类关键字都能自动区分，无需人工维护任何列表。

### 5.3 写入时回读校验 + 候选自动重试

1. 一次 PowerShell 调用写入所有角色（每个角色先用最优候选）；
2. 写完**立刻回读注册表值**，与写入值比对（忽略大小写与分隔符）；
3. 回读不一致或写入报错的角色，自动换下一个候选属性重试（最多 3 个候选）；
4. 成功的关键字自动写入 `driver_profiles.local.json`，下次直接命中；
5. 所有候选都失败才判定"该驱动不支持"，并在日志里明确写出原因。

### 5.4 本地档案文件

- 路径：程序所在目录下的 `driver_profiles.local.json`（`build.txt` 打包后为 exe 同目录）；
- 结构：与 `driver_profiles.json` 相同，额外多一个 `_meta` 记录识别时间与用过的网口名；
- 想分享给同事：把 `profiles` 里的条目合并进 `driver_profiles.json` 即可；
- 删掉该文件不会影响程序，只是下次需要重新学习一遍。

## 6. 相关 PowerShell 实现要点

- 探测：`Get-NetAdapterAdvancedProperty -Name <网口> -IncludeHidden | ConvertTo-Json`
- 写入：`Set-NetAdapterAdvancedProperty -Name <网口> -RegistryKeyword <关键字> -RegistryValue <值> -NoRestart`
- 恢复默认：`Get-NetAdapterAdvancedProperty ... -RegistryKeyword <关键字> | Reset-NetAdapterAdvancedProperty -NoRestart`
- 生效：`Restart-NetAdapter -Name <网口> -Confirm:$false`（失败退回 `Disable-NetAdapter` + `Enable-NetAdapter`）

所有脚本通过 `-EncodedCommand`（UTF-16LE + Base64）执行并强制 UTF-8 输出，
避免中文网卡名、引号与重定向编码问题；写入操作合并为**一次** PowerShell 调用，
一次配置的耗时从旧版的 10~30 秒降到约 1~3 秒。

## 7. 排查清单

| 现象 | 原因与处理 |
|---|---|
| 提示"该驱动未提供此属性" | 驱动确实不支持（如 I219-V 无 VLAN ID），或需要在档案中补充关键字 |
| MAC 写入成功但无效 | 网卡未重启；确认配置未设置 `"restart_adapter": false`，或手动点【重启网卡】 |
| 提示"该 MAC 未设置本地管理位(LAA)" | 首字节 bit1 必须为 1（如 `0A`、`02`），否则部分驱动拒绝写入 |
| 下拉框看不到网卡 | 该网卡被识别为虚拟网卡（VirtualBox/Hyper-V/蓝牙/Wi-Fi Direct）；或 PowerShell 不可用导致回退到 psutil 列表 |
