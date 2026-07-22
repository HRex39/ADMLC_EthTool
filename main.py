import tkinter as tk
from tkinter import ttk, messagebox
import psutil, json, platform, os, sys
import threading, traceback, time, socket, subprocess
from sftp_browser import SFTPBrowser

def run_as_admin():
    if platform.system().lower() == "windows":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                # 重新启动自身并申请管理员权限
                script = os.path.abspath(sys.argv[0])
                params = " ".join([f'"{x}"' for x in sys.argv[1:]])
                ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, f'"{script}" {params}', None, 1
                )
                sys.exit(0)
        except Exception as e:
            print(f"⚠️ 提权失败: {e}")
            sys.exit(1)

def check_admin():
    system = platform.system().lower()
    if system in ["linux", "darwin"]:
        if os.geteuid() != 0:
            print("❌ 请使用 sudo 运行此程序: sudo python main.py")
            sys.exit(1)
    elif system == "windows":
        try:
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                print("❌ 请以管理员身份运行此程序 (右键 -> 以管理员身份运行)")
                sys.exit(1)
        except:
            print("⚠️ 无法检测管理员权限，请确保以管理员身份运行")
            sys.exit(1)

def load_configs():
    with open("configs.json", "r") as f:
        return json.load(f)

configs = load_configs()

def list_interfaces():
    return list(psutil.net_if_addrs().keys())

system = platform.system().lower()
if system == "linux":
    from linux_config import apply_config_linux as apply_config
elif system == "windows":
    from windows_config import apply_config_windows as apply_config
else:
    def apply_config(iface, cfg, output_text):
        output_text.insert(tk.END, f"暂不支持此系统: {system}\n")
        output_text.update_idletasks()

def on_iface_select(event):
    selected = iface_var.get()
    output_text.insert(tk.END, f"选择了网口: {selected}\n")
    output_text.update_idletasks()

def on_config_select(event):
    selected = config_var.get()
    cfg = configs[selected]
    iface = iface_var.get()
    output_text.insert(tk.END, f"\n选择了配置: {selected}\n")
    output_text.see("end")
    output_text.update_idletasks()
    for key, value in cfg.items():
        if value == "auto":
            output_text.insert(tk.END, f"{key}: 自动协商/系统默认\n")
        else:
            output_text.insert(tk.END, f"{key}: {value}\n")
        output_text.see("end")
        output_text.update_idletasks()
    if iface:
        # 在后台线程里执行，避免阻塞 Tkinter 主线程
        threading.Thread(target=lambda: apply_config(iface, cfg, output_text)).start()

import subprocess, platform

def ping_host(host, count=2, timeout=2):
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout*1000)), host]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(int(timeout)), host]

    try:
        if system == "windows":
            # Windows: 隐藏控制台窗口
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        else:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        return res.returncode == 0
    except Exception:
        return False

def tcp_port_open(host, port=22, timeout=3):
    """
    尝试 TCP 连接指定端口，返回 True/False。
    这是判断 SFTP 服务是否可达的更可靠方式（即使 ICMP 被屏蔽）。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

def open_sftp_window():
    """
    在后台先检测 host 是否可达（ping + tcp），UI 显示检测窗口。
    检测通过后在主线程打开 SFTPBrowser；否则提示错误。
    修改点：把 host/user/password/port 提取到这里，便于复用/修改。
    """
    host = "172.16.105.26"
    user = "root"
    password = "bY6dCcBBZ0HsM3m"
    port = 22

    # 创建一个小窗口显示检测进度（主线程）
    check_win = tk.Toplevel(root)
    check_win.title("连接检测")
    check_win.geometry("320x100")
    check_win.resizable(False, False)
    ttk.Label(check_win, text=f"正在检测 {host}:{port} 可达性，请稍候...").pack(padx=12, pady=10)
    progress_label = ttk.Label(check_win, text="正在 ping 主机...")
    progress_label.pack(padx=12, pady=6)
    # 禁用关闭按钮（避免用户误操作）
    check_win.protocol("WM_DELETE_WINDOW", lambda: None)
    check_win.transient(root)
    check_win.grab_set()

    def _worker():
        try:
            # 先 ping（快速判断）
            progress_label_text = "正在 ping 主机..."
            root.after(0, lambda: progress_label.config(text=progress_label_text))
            ping_ok = ping_host(host, count=2, timeout=2)

            # 再尝试 TCP 端口（更可靠）
            root.after(0, lambda: progress_label.config(text="正在检测 SFTP 端口..."))
            tcp_ok = tcp_port_open(host, port=port, timeout=3)

            # 如果任一成功就认为可达（优先 tcp）
            reachable = tcp_ok or ping_ok

            # 小延迟以便用户看到状态
            time.sleep(0.2)

            def _on_result():
                try:
                    check_win.destroy()
                except:
                    pass
                if reachable:
                    messagebox.showinfo("连接成功", f"{host}:{port} 可达，正在打开 SFTP 浏览器。")
                    # 在主线程打开 SFTPBrowser（不会阻塞主线程，因为 SFTPBrowser 内部会在构造时尝试连接）
                    # 但为了避免构造时阻塞 UI（若连接慢），我们仍然在后台线程创建窗口实例。
                    # 这里我们直接在主线程创建，SFTPBrowser 内部已有异常处理会弹窗并关闭窗口。
                    try:
                        SFTPBrowser(root, host=host, user=user, password=password, port=port)
                    except Exception as e:
                        messagebox.showerror("错误", f"打开 SFTP 窗口失败: {e}")
                else:
                    # 更详细提示：区分 tcp/ping
                    if not ping_ok and not tcp_ok:
                        messagebox.showerror("连接失败", f"无法连通 {host}（ping 与 TCP 端口检测均失败）。请检查网络或 IP 是否正确。")
                    elif not tcp_ok:
                        messagebox.showwarning("端口不可达", f"{host}:{port} 端口不可达，但 ping 成功。SFTP 可能未启动或被防火墙阻止。")
                    else:
                        messagebox.showwarning("Ping 不通", f"{host} 无法 ping 通，但端口可达。将尝试打开 SFTP 浏览器。")
                        try:
                            SFTPBrowser(root, host=host, user=user, password=password, port=port)
                        except Exception as e:
                            messagebox.showerror("错误", f"打开 SFTP 窗口失败: {e}")

            root.after(0, _on_result)
        except Exception as e:
            # 出现异常时关闭检测窗口并提示
            def _err():
                try:
                    check_win.destroy()
                except:
                    pass
                messagebox.showerror("检测异常", f"检测过程中发生异常: {e}\n{traceback.format_exc()}")
            root.after(0, _err)

    threading.Thread(target=_worker, daemon=True).start()

# ---------------- Help 与 About 函数 ----------------
def show_help():
    """
    显示帮助信息并把简短说明写入 output_text（如果存在）。
    """
    help_text = (
        "使用说明：\n"
        "1. 选择网口与配置后，点击对应配置以应用设置。\n"
        "2. 点击 打开 SFTP 浏览器 以连接远程设备（程序会先检测连通性）。\n"
        "3. 在 SFTP 窗口中可浏览、上传、下载、删除远程文件。\n"
        "4. 一键下载售后数据会下载 /log, /backlog, /alglog, /f120calib, /params 到本地目录。\n\n"
        "常见问题：\n"
        "- 无法连接：请检查 IP、网络与防火墙；程序会先 ping 与检测端口。\n"
        "- 权限问题：Windows 请以管理员身份运行；Linux 请使用 sudo。\n\n"
        "更多帮助请联系hcr2077@outlook.com。"
    )
    # 写入 output_text（如果存在）
    try:
        if 'output_text' in globals() and isinstance(output_text, tk.Text):
            output_text.insert(tk.END, "\n[帮助]\n" + help_text + "\n")
            output_text.see("end")
    except Exception:
        pass
    # 弹窗显示（主线程）
    messagebox.showinfo("帮助", help_text)

def show_about():
    """
    显示关于对话框，包含作者与版本信息。
    """
    about_text = (
        "网口配置工具 & 售后数据下载器\n"
        "版本: 1.8\n"
        "作者: Chenrui Huang\n"
        "邮箱: hcr2077@outlook.com\n\n"
        "说明: 本工具用于网口配置与远程设备售后数据下载。"
    )
    # 写入 output_text（如果存在）
    try:
        if 'output_text' in globals() and isinstance(output_text, tk.Text):
            output_text.insert(tk.END, "\n[关于]\n" + about_text + "\n")
            output_text.see("end")
    except Exception:
        pass
    messagebox.showinfo("关于", about_text)

run_as_admin()
check_admin()

root = tk.Tk()
root.title("网口配置工具 & 售后数据下载器")
# ---------------- 菜单栏与作者行 ----------------
# 在创建 root 之后、控件之前或之后插入以下代码
menubar = tk.Menu(root)
help_menu = tk.Menu(menubar, tearoff=0)
help_menu.add_command(label="帮助", command=show_help)
help_menu.add_separator()
help_menu.add_command(label="关于", command=show_about)
menubar.add_cascade(label="帮助", menu=help_menu)
# 将菜单栏设置到主窗口
try:
    root.config(menu=menubar)
except Exception:
    # 某些平台或嵌入场景可能不支持 menu，忽略错误
    pass
# 在主窗口底部添加一行作者信息（可选）
author_frame = ttk.Frame(root)
author_frame.pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=(0,6))
author_label = ttk.Label(author_frame, text="作者: Chenrui Huang    版本: 1.8", anchor="w")
author_label.pack(side=tk.LEFT)

iface_var = tk.StringVar()
ttk.Label(root, text="选择网口:").pack(pady=5)
iface_combo = ttk.Combobox(root, textvariable=iface_var, values=list_interfaces())
iface_combo.pack(pady=5)
iface_combo.bind("<<ComboboxSelected>>", on_iface_select)
# iface_combo.current(0)

config_var = tk.StringVar()
ttk.Label(root, text="选择配置:").pack(pady=5)
config_combo = ttk.Combobox(root, textvariable=config_var, values=list(configs.keys()))
config_combo.pack(pady=5)
config_combo.bind("<<ComboboxSelected>>", on_config_select)
# config_combo.current(0)

ttk.Button(root, text="打开 SFTP 浏览器", command=open_sftp_window).pack(pady=5)

output_text = tk.Text(root, height=15, width=60)
output_text.pack(pady=10)

root.mainloop()
