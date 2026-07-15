import tkinter as tk
from tkinter import ttk
import psutil, json, platform, os, sys
import threading
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

def open_sftp_window():
    SFTPBrowser(root, host="172.16.105.26", user="root", password="bY6dCcBBZ0HsM3m")

run_as_admin()
check_admin()

root = tk.Tk()
root.title("网口配置工具")

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
