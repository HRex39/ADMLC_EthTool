# 网口配置工具 & 售后数据下载器

一个用于产线/售后的网口调试工具：一键把 PC 网卡配置成连接目标设备所需的状态，并直接
通过 SFTP 拉取设备日志与标定数据。

## 功能

| 功能 | 说明 |
|---|---|
| 选择网口 | 通过 `Get-NetAdapter` 列出「接口名 — 驱动型号（状态）」，自动过滤虚拟网卡、Wi-Fi Direct、WAN Miniport、蓝牙等非物理网口 |
| 应用配置 | 按 `configs.json` 中的预设写入 IP / 子网掩码 / 网关 / VLAN / MAC / 静态 ARP |
| 自动重启网卡 | MAC 与 VLAN 属于驱动高级属性，写入后自动重启网卡使其立即生效 |
| 探测驱动属性 | 导出选中网卡的全部高级属性，用于适配新网卡（见 `docs/drivers.md`） |
| SFTP 浏览器 | 先检测可达性再连接，支持浏览/下载/一键下载售后数据（`/log`、`/backlog`、`/alglog`）与标定数据（`/f120calib`、`/params`） |

## 技术栈

- Python 3.9+（本机实测 3.14）
- Tkinter（跨平台 GUI）
- paramiko（SFTP）
- psutil（PowerShell 不可用时的网口列表回退）
- Windows 网卡读写：PowerShell `NetAdapter` 模块（Windows 8 / Server 2012 及以上）

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python main.py
```

- Windows：程序会自动申请管理员权限（UAC），拒绝提权会给出明确提示。
- Linux：请使用 `sudo python3 main.py`。

## 配置文件

| 文件 | 作用 |
|---|---|
| `configs.json` | 配置预设（`mode` / `ip` / `netmask` / `gateway` / `vlan_id` / `mac` / `arp`）。`"auto"` 表示交给系统自动协商 |
| `driver_profiles.json` | **网卡驱动关键字档案**：按驱动型号指定 MAC / VLAN 属性对应的注册表关键字，遇到新网卡只加一行即可，不用改代码 |

配置项补充说明：

- `mode: "default"`：恢复默认（DHCP + 恢复 MAC、关闭 VLAN、清空 ARP），用于连接 DDT/DPS。
- `mode: "test"`：按配置写入静态 IP / VLAN / MAC / ARP。
- `restart_adapter: false`（可选）：写入 MAC/VLAN 后不自动重启网卡。

## 驱动适配（核心设计）

不同厂商网卡的"高级属性"**显示名各不相同且随系统语言变化**，因此本工具不使用显示名匹配，
而是按注册表关键字 `RegistryKeyword` 识别，并按 `RegistryValue` 数值写入：

| 网卡驱动（实测） | MAC 关键字（显示名） | VLAN 开关关键字 | VLAN ID 关键字 |
|---|---|---|---|
| ASIX USB to Gigabit Ethernet | `NetworkAddress`（网络地址） | `*PriorityVLANTag` | `VLAN_ID`（VLAN标识） |
| Realtek USB GbE Family Controller | `NetworkAddress`（Network Address） | `*PriorityVLANTag` | `RegVlanID`（VLAN ID） |
| Intel Ethernet Connection I219-V | `NetworkAddress`（**本地管理地址**） | `*PriorityVLANTag` | 无（该驱动不支持） |
| USB2.0 Ethernet Adapter | `NetworkAddress`（NetworkAddress） | `PriorityVLANTag`（无 `*`） | 无 |

> 注意 `RegVlanID` 这类关键字无法通过"名字列表"猜到，这正是必须用关键字探测 + 档案兜底的原因。

匹配优先级：`driver_profiles.json` → 内置关键字候选表 → 显示名多语言兜底。
完整说明与新增网卡的步骤见 [`docs/drivers.md`](docs/drivers.md)。

## 目录结构

```
main.py               主界面（Tkinter）
nic_driver.py         网卡高级属性解析引擎（关键字优先，跨厂商/跨语言）
windows_config.py     Windows 配置编排（IP/MAC/VLAN/ARP + 网卡重启）
linux_config.py       Linux 配置编排（ip 命令）
sftp_browser.py       SFTP 浏览器窗口
driver_profiles.json  驱动关键字档案（可按需扩充）
configs.json          配置预设
docs/drivers.md       驱动适配说明与关键字对照表
selftest_nic_driver.py   离线自检：关键字匹配与取值逻辑（不接触真实网卡）
selftest_config_flow.py  离线自检：配置编排流程（monkeypatch 掉系统调用，零副作用）
```

## 自检

```bash
python selftest_nic_driver.py     # 解析逻辑：34 项
python selftest_config_flow.py    # 编排流程：21 项
```

两个脚本都不依赖真实网卡、不改动系统，改完代码跑一遍即可回归。覆盖：

- 各厂商 + 中英文显示名的关键字匹配（含 `RegVlanID` 这类猜不到的关键字）；
- **VLAN 取值陷阱**：必须选 `3`/`2`，绝不能选中"已启用数据包优先级"（值 1，不打标签）；
- 未提供 VLAN ID 的驱动要明确提示而不是静默失败；
- Test / Default 两条路径实际下发的操作、自动重启开关、非法 MAC/VLAN 不崩溃；
- MAC 规范化与合法性、CIDR 掩码换算、旧版 Tk Text 接口兼容。
