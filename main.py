# -*- coding: utf-8 -*-
"""
网口配置工具 & 售后数据下载器 —— 主界面

在旧版基础上的主要改动：
  1. 网口列表改用 Get-NetAdapter 获取「接口名 — 驱动型号（状态）」，并过滤虚拟/WiFi-Direct/
     WAN Miniport/蓝牙等非物理网口，避免选错（旧版直接用 psutil 会把这些都列出来）。
  2. 选中下拉框不再立刻改网卡；改为显式点击【应用配置】，并在应用期间禁用按钮。
  3. 工作线程不再直接操作 Tk 控件（旧版在工作线程里 insert 日志，属未定义行为），
     统一走队列 + 主线程泵送。
  4. configs.json 用脚本所在目录的绝对路径读取（旧版按当前工作目录，快捷方式启动会崩）。
  5. 新增【探测驱动属性】：一键导出该网卡全部高级属性，遇到新驱动无需改代码即可适配。
  6. UAC 提权被拒绝时给出明确提示（旧版静默 exit(0)）。
"""

import ctypes
import functools
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

import nic_driver
from sftp_browser import SFTPBrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "configs.json")
APP_VERSION = "2.0"

STATUS_CN = {
    "Up": "已连接",
    "Disconnected": "未连接",
    "Disabled": "已禁用",
    "Not Present": "不存在",
}


# ----------------------------------------------------------------------------
# 平台相关
# ----------------------------------------------------------------------------

def _native_error(title, text):
    """在 Tk 尚未创建时弹系统错误框（用于提权失败等场景）。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x10)
    except Exception:
        print("[%s] %s" % (title, text))


def run_as_admin():
    """Windows 下自动申请管理员权限（UAC 被拒绝时给出提示，不再静默退出）。"""
    if platform.system().lower() != "windows":
        return
    try:
        import ctypes as _ctypes
        if _ctypes.windll.shell32.IsUserAnAdmin():
            return
        script = os.path.abspath(sys.argv[0])
        params = " ".join('"%s"' % item for item in sys.argv[1:])
        result = _ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, '"%s" %s' % (script, params), None, 1
        )
        if int(result) <= 32:
            _native_error(
                "需要管理员权限",
                "提权被拒绝或失败（错误码 %s）。\n\n本工具需要修改网卡属性，请右键选择"
                "「以管理员身份运行」。" % result,
            )
            sys.exit(1)
        sys.exit(0)  # 已交给提权后的新进程
    except SystemExit:
        raise
    except Exception as exc:
        _native_error("提权失败", "无法申请管理员权限: %s" % exc)
        sys.exit(1)


def check_admin():
    """兜底检查：不是管理员就提示并退出。返回 True 表示权限正常。"""
    system = platform.system().lower()
    if system in ("linux", "darwin"):
        if os.geteuid() != 0:
            print("❌ 请使用 sudo 运行此程序: sudo python main.py")
            return False
        return True
    if system == "windows":
        try:
            if not ctypes.windll.shell32.IsUserAnAdmin():
                _native_error("需要管理员权限",
                              "请以管理员身份运行本程序（右键 -> 以管理员身份运行）。")
                return False
        except Exception:
            _native_error("权限检测失败", "无法检测管理员权限，请确保以管理员身份运行。")
            return False
    return True


def load_configs():
    """读取配置文件（绝对路径）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data
    except FileNotFoundError:
        messagebox.showerror("配置缺失", "找不到配置文件:\n%s" % CONFIG_PATH)
    except Exception as exc:
        messagebox.showerror("配置错误", "读取 %s 失败:\n%s" % (CONFIG_PATH, exc))
    return {}


system_name = platform.system().lower()
if system_name == "windows":
    from windows_config import apply_config_windows as apply_config
    import windows_config as platform_config
elif system_name == "linux":
    from linux_config import apply_config_linux as apply_config
    import linux_config as platform_config
else:
    platform_config = None

    def apply_config(iface, cfg, log):
        log("暂不支持此系统: %s" % system_name)


# ----------------------------------------------------------------------------
# 线程安全日志
# ----------------------------------------------------------------------------

class UiLog:
    """
    把工作线程的日志安全送到 Tk 文本框：
    工作线程只往 queue 里放字符串，主线程用 after 定期泵送，绝不跨线程碰控件。
    """

    MAX_LINES = 3000

    def __init__(self, text_widget, root):
        self.text = text_widget
        self.root = root
        self.queue = queue.Queue()
        self.job_done = threading.Event()
        self.on_idle = None
        self._pump()

    def write(self, message):
        self.queue.put(str(message))

    __call__ = write  # 支持 log("...") 直接调用

    def call(self, func, *args, **kwargs):
        """让工作线程安全地请求主线程执行一段 UI 代码（弹窗、开窗口等）。"""
        self.queue.put(functools.partial(func, *args, **kwargs))

    def _pump(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if callable(item):
                    try:
                        item()
                    except Exception:
                        pass
                    continue
                message = str(item)
                if not message.endswith("\n"):
                    message += "\n"
                self.text.insert("end", message)
        except queue.Empty:
            pass
        except Exception:
            pass
        try:
            self.text.see("end")
            if int(self.text.index("end-1c").split(".")[0]) > self.MAX_LINES:
                self.text.delete("1.0", "500.0")
        except Exception:
            pass
        if self.job_done.is_set() and self.on_idle:
            self.job_done.clear()
            try:
                self.on_idle()
            except Exception:
                pass
        self.root.after(120, self._pump)


# ----------------------------------------------------------------------------
# 网口列表
# ----------------------------------------------------------------------------

def _fallback_interfaces():
    """PowerShell 不可用时退回 psutil（仅名称，不过滤）。"""
    names = []
    try:
        import psutil
        names = list(psutil.net_if_addrs().keys())
    except Exception:
        pass
    return [{"name": name, "description": "", "status": "", "mac": "", "speed": "",
             "virtual": False} for name in names]


def list_interfaces():
    adapters = nic_driver.list_adapters()
    if adapters:
        return adapters
    return _fallback_interfaces()


def format_adapter(adapter):
    status = STATUS_CN.get(adapter.get("status") or "", adapter.get("status") or "")
    parts = [adapter["name"]]
    if adapter.get("description"):
        parts.append("— %s" % adapter["description"])
    extra = "，".join(x for x in (status, adapter.get("speed")) if x)
    if extra:
        parts.append("（%s）" % extra)
    return " ".join(parts)


# ----------------------------------------------------------------------------
# 主界面
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root, configs):
        self.root = root
        self.configs = configs
        self.adapters = []
        self._adapter_map = {}
        self._busy = False
        self._lock = threading.Lock()

        root.title("网口配置工具 & 售后数据下载器  v%s" % APP_VERSION)
        root.geometry("760x680")
        root.minsize(680, 560)

        self._build_menu()
        self._build_widgets()
        self.refresh_interfaces(first=True)

    # ---------- 界面 ----------
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="帮助", command=self.show_help)
        help_menu.add_command(label="驱动适配说明", command=self.show_driver_help)
        help_menu.add_separator()
        help_menu.add_command(label="打开配置目录", command=self.open_config_dir)
        help_menu.add_separator()
        help_menu.add_command(label="关于", command=self.show_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        try:
            self.root.config(menu=menubar)
        except Exception:
            pass

    def _build_widgets(self):
        top = ttk.Frame(self.root)
        top.pack(fill=tk.X, padx=12, pady=(10, 4))

        ttk.Label(top, text="选择网口:").grid(row=0, column=0, sticky="w")
        self.iface_var = tk.StringVar()
        self.iface_combo = ttk.Combobox(top, textvariable=self.iface_var, width=70, state="readonly")
        self.iface_combo.grid(row=0, column=1, columnspan=3, sticky="we", padx=6, pady=3)
        self.iface_combo.bind("<<ComboboxSelected>>", self.on_iface_select)

        ttk.Label(top, text="选择配置:").grid(row=1, column=0, sticky="w")
        self.config_var = tk.StringVar()
        config_names = [k for k in self.configs.keys() if not str(k).startswith("_")]
        self.config_combo = ttk.Combobox(top, textvariable=self.config_var, width=70,
                                         state="readonly", values=config_names)
        self.config_combo.grid(row=1, column=1, columnspan=3, sticky="we", padx=6, pady=3)
        self.config_combo.bind("<<ComboboxSelected>>", self.on_config_select)

        ttk.Button(top, text="刷新网口", command=self.refresh_interfaces).grid(
            row=0, column=4, padx=4)
        ttk.Button(top, text="探测驱动属性", command=self.probe_driver).grid(
            row=1, column=4, padx=4)
        top.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill=tk.X, padx=12, pady=6)
        self.apply_btn = ttk.Button(buttons, text="应用配置", command=self.apply_config_clicked)
        self.apply_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.restart_btn = ttk.Button(buttons, text="重启网卡", command=self.restart_iface)
        self.restart_btn.pack(side=tk.LEFT, padx=6)
        self.sftp_btn = ttk.Button(buttons, text="打开 SFTP 浏览器", command=self.open_sftp_window)
        self.sftp_btn.pack(side=tk.LEFT, padx=6)
        self.clear_btn = ttk.Button(buttons, text="清屏", command=self.clear_log)
        self.clear_btn.pack(side=tk.RIGHT)

        text_frame = ttk.Frame(self.root)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))
        self.output_text = tk.Text(text_frame, height=18, wrap="word")
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=self.output_text.yview)
        self.output_text.configure(yscrollcommand=scrollbar.set)
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.log = UiLog(self.output_text, self.root)
        self.log.on_idle = self._on_job_finished

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                  relief="sunken").pack(fill=tk.X, side=tk.BOTTOM)
        author = ttk.Frame(self.root)
        author.pack(fill=tk.X, side=tk.BOTTOM, padx=8, pady=(0, 2))
        ttk.Label(author, text="作者: Chenrui Huang    版本: %s" % APP_VERSION,
                  anchor="w").pack(side=tk.LEFT)

        self.log.write("网口配置工具 v%s 已启动（管理员权限正常）" % APP_VERSION)
        self.log.write("提示：选择网口与配置后，点击【应用配置】才会真正修改网卡。")
        self.log.write("首次使用新网卡时，可先点【探测驱动属性】查看识别结果。\n")

    # ---------- 网口 / 配置 选择 ----------
    def refresh_interfaces(self, first=False):
        self.adapters = list_interfaces()
        self._adapter_map = {}
        displays = []
        for adapter in self.adapters:
            text = format_adapter(adapter)
            while text in self._adapter_map:  # 保证显示项唯一
                text += " "
            self._adapter_map[text] = adapter["name"]
            displays.append(text)
        self.iface_combo["values"] = displays
        if displays and not self.iface_var.get():
            self.iface_var.set(displays[0])
        elif self.iface_var.get() not in displays:
            self.iface_var.set(displays[0] if displays else "")
        if not first:
            self.log.write("已刷新网口列表，共 %d 个可用物理网口" % len(displays))

    def selected_iface(self):
        return self._adapter_map.get(self.iface_var.get())

    def on_iface_select(self, _event=None):
        adapter = next((a for a in self.adapters
                        if a["name"] == self.selected_iface()), None)
        if not adapter:
            return
        self.log.write("\n选择了网口: %s" % adapter["name"])
        self.log.write("  驱动型号: %s" % (adapter.get("description") or "未知"))
        self.log.write("  状态: %s ｜ MAC: %s ｜ 速率: %s"
                       % (STATUS_CN.get(adapter.get("status"), adapter.get("status") or "-"),
                          adapter.get("mac") or "-", adapter.get("speed") or "-"))

    def on_config_select(self, _event=None):
        """只做预览，不修改网卡（旧版是选中即改，很危险）。"""
        name = self.config_var.get()
        cfg = self.configs.get(name)
        if not isinstance(cfg, dict):
            return
        self.log.write("\n选择了配置: %s" % name)
        for key, value in cfg.items():
            if value == "auto":
                self.log.write("  %s: 自动协商/系统默认" % key)
            elif isinstance(value, list):
                self.log.write("  %s: %s" % (key, value))
            else:
                self.log.write("  %s: %s" % (key, value))
        self.log.write("  → 点击【应用配置】后生效")

    # ---------- 应用配置 ----------
    def apply_config_clicked(self):
        iface = self.selected_iface()
        name = self.config_var.get()
        cfg = self.configs.get(name)
        if not iface:
            messagebox.showwarning("提示", "请先选择网口")
            return
        if not isinstance(cfg, dict):
            messagebox.showwarning("提示", "请先选择配置")
            return
        if not messagebox.askyesno(
                "确认应用",
                "确定要将配置【%s】应用到网口【%s】吗？\n\n"
                "该操作会修改 IP / MAC / VLAN / ARP，并可能重启网卡导致链路短暂断开。"
                % (name, iface)):
            return
        self._start_job(apply_config, iface, dict(cfg), self.log)

    def restart_iface(self):
        iface = self.selected_iface()
        if not iface:
            messagebox.showwarning("提示", "请先选择网口")
            return
        if platform_config is None or not hasattr(platform_config, "restart_adapter"):
            messagebox.showinfo("提示", "当前系统不支持在此重启网卡")
            return
        if not messagebox.askyesno("确认", "确定要重启网口【%s】吗？链路会短暂断开。" % iface):
            return
        self._start_job(platform_config.restart_adapter, iface, self.log)

    def probe_driver(self):
        iface = self.selected_iface()
        if not iface:
            messagebox.showwarning("提示", "请先选择网口")
            return

        def _job():
            path, payload = nic_driver.export_probe(iface)
            if not path:
                self.log.write("⚠️ 探测导出失败: %s" % payload.get("error"))
                return
            self.log.write("\n[探测] 网卡: %s" % (payload.get("adapter", {}).get(
                "InterfaceDescription") or iface))
            for role, info in payload.get("resolved_roles", {}).items():
                self.log.write("  %s: %s" % (nic_driver.ROLE_LABEL.get(role, role),
                                             info.get("RegistryKeyword") or "未找到"))
            self.log.write("  已导出完整属性到: %s" % path)
            self.log.write("  若需适配该网卡，把该文件里的 profile_suggestion 抄进 "
                           "driver_profiles.json 即可。")
            self.log.call(messagebox.showinfo, "探测完成", "已导出驱动属性:\n%s" % path)

        self._start_job(_job)

    def clear_log(self):
        try:
            self.output_text.delete("1.0", "end")
        except Exception:
            pass

    def open_config_dir(self):
        try:
            if platform.system().lower() == "windows":
                os.startfile(BASE_DIR)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", BASE_DIR])
        except Exception as exc:
            messagebox.showerror("错误", "无法打开目录: %s" % exc)

    # ---------- 任务调度 ----------
    def _start_job(self, func, *args):
        """在后台线程执行任务，期间禁用按钮；完成后由日志泵回调恢复。"""
        with self._lock:
            if self._busy:
                messagebox.showinfo("请稍候", "已有任务正在执行，请等待完成。")
                return
            self._busy = True
        self._set_controls(False)
        self.status_var.set("正在执行…")

        def _worker():
            try:
                func(*args)
            except Exception:
                self.log.write("❌ 任务异常:\n%s" % traceback.format_exc())
            finally:
                self.log.job_done.set()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_job_finished(self):
        self._busy = False
        self._set_controls(True)
        self.status_var.set("就绪")

    def _set_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self.apply_btn, self.restart_btn, self.sftp_btn):
            try:
                button.config(state=state)
            except Exception:
                pass

    # ---------- SFTP ----------
    def ping_host(self, host, count=2, timeout=2):
        if platform.system().lower() == "windows":
            cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
        else:
            cmd = ["ping", "-c", str(count), "-W", str(int(timeout)), host]
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.run(cmd, **kwargs).returncode == 0
        except Exception:
            return False

    def tcp_port_open(self, host, port=22, timeout=3):
        import socket
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False

    def open_sftp_window(self):
        """先检测可达性，再打开 SFTP 浏览器。"""
        host, user, password, port = "172.16.105.26", "root", "bY6dCcBBZ0HsM3m", 22

        check_win = tk.Toplevel(self.root)
        check_win.title("连接检测")
        check_win.geometry("340x110")
        check_win.resizable(False, False)
        ttk.Label(check_win, text="正在检测 %s:%s 可达性……" % (host, port)).pack(padx=12, pady=10)
        progress_label = ttk.Label(check_win, text="正在 ping 主机…")
        progress_label.pack(padx=12, pady=6)
        check_win.protocol("WM_DELETE_WINDOW", lambda: None)
        check_win.transient(self.root)
        check_win.grab_set()

        def _open_browser():
            try:
                SFTPBrowser(self.root, host=host, user=user, password=password, port=port)
            except Exception as exc:
                messagebox.showerror("错误", "打开 SFTP 窗口失败: %s" % exc)

        def _worker():
            ping_ok = self.ping_host(host, count=2, timeout=2)
            self.log.call(progress_label.config, text="正在检测 SFTP 端口…")
            tcp_ok = self.tcp_port_open(host, port=port, timeout=3)
            time.sleep(0.2)

            def _finish():
                try:
                    check_win.destroy()
                except Exception:
                    pass
                if tcp_ok:
                    self.log.write("\nSFTP 端口 %s:%s 可达，正在打开浏览器…" % (host, port))
                    _open_browser()
                elif ping_ok:
                    messagebox.showwarning(
                        "端口不可达",
                        "%s:%s 端口不可达，但 ping 成功。SFTP 可能未启动或被防火墙阻止。"
                        % (host, port))
                else:
                    messagebox.showerror(
                        "连接失败",
                        "无法连通 %s（ping 与 TCP 端口检测均失败）。\n请检查网口配置与设备 IP。"
                        % host)

            self.log.call(_finish)

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- 帮助 ----------
    def show_help(self):
        text = (
            "使用说明：\n"
            "1. 选择网口（下拉框已过滤虚拟网卡，显示「接口名 — 驱动型号（状态）」）。\n"
            "2. 选择配置后点击【应用配置】——选中下拉框不会改动网卡。\n"
            "3. 应用配置会写入 IP / MAC / VLAN / ARP，MAC 与 VLAN 写入后会自动重启网卡。\n"
            "4. 遇到新网卡先点【探测驱动属性】，导出 JSON 后按提示补充 driver_profiles.json。\n"
            "5. 【打开 SFTP 浏览器】会先检测连通性，再进入远程文件系统下载售后数据。\n\n"
            "常见问题：\n"
            "- 提示「该驱动未提供此属性」：该网卡确实不支持（如 Intel I219-V 无 VLAN ID）。\n"
            "- 权限问题：Windows 需管理员身份，Linux 需 sudo。\n"
            "- 联系邮箱：hcr2077@outlook.com"
        )
        self.log.write("\n[帮助]\n" + text)
        messagebox.showinfo("帮助", text)

    def show_driver_help(self):
        text = (
            "驱动适配说明（为什么不再按「属性名字」匹配）：\n\n"
            "同一功能在不同驱动上显示名完全不同，且随系统语言变化：\n"
            "  MAC 属性：Intel=「本地管理地址」/ ASIX=「网络地址」/ Realtek=「Network Address」\n"
            "  VLAN ID ：ASIX 关键字 VLAN_ID / Realtek 关键字 RegVlanID / Intel I219-V 无此属性\n\n"
            "因此本工具按注册表关键字(RegistryKeyword)识别，并按 RegistryValue 数值写入，\n"
            "完全不受厂商命名和中文/英文系统影响。\n\n"
            "适配新网卡（无需改代码）：\n"
            "1. 选中网口 → 点【探测驱动属性】→ 生成 driver_probe_*.json\n"
            "2. 把其中 profile_suggestion 的内容合并进 driver_profiles.json\n"
            "3. 重新点【应用配置】即可"
        )
        self.log.write("\n[驱动适配说明]\n" + text)
        messagebox.showinfo("驱动适配说明", text)

    def show_about(self):
        text = (
            "网口配置工具 & 售后数据下载器\n"
            "版本: %s\n"
            "作者: Chenrui Huang\n"
            "邮箱: hcr2077@outlook.com\n\n"
            "说明: 按注册表关键字适配各厂商网卡驱动，用于网口配置与远程售后数据下载。"
            % APP_VERSION
        )
        self.log.write("\n[关于]\n" + text)
        messagebox.showinfo("关于", text)


def main():
    run_as_admin()
    if not check_admin():
        sys.exit(1)

    root = tk.Tk()
    configs = load_configs()
    if not configs:
        try:
            root.destroy()
        except Exception:
            pass
        sys.exit(1)
    App(root, configs)
    root.mainloop()


if __name__ == "__main__":
    main()
