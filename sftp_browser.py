import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import paramiko, os, stat

class SFTPBrowser(tk.Toplevel):
    def __init__(self, master, host, user, password, port=22):
        super().__init__(master)
        self.title("SFTP 浏览器")
        self.geometry("600x400")

        try:
            self.transport = paramiko.Transport((host, port))
            self.transport.connect(username=user, password=password)
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        except Exception as e:
            messagebox.showerror("错误", f"无法连接 SFTP: {e}")
            self.destroy()
            return

        # 起始目录固定为根 /
        self.start_dir = "/"
        self.current_path = self.start_dir
        self.file_types = {}

        # 文件列表
        self.file_list = tk.Listbox(self, selectmode=tk.SINGLE)
        self.file_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.file_list.bind("<Double-Button-1>", self.on_double_click)

        # 按钮区
        btn_frame = tk.Frame(self)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="下载文件", command=self.download_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="下载文件夹", command=self.download_folder).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="上传文件", command=self.upload_file).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="上传文件夹", command=self.upload_folder).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="返回上级", command=self.go_up).pack(side=tk.LEFT, padx=5)

        # 状态栏
        self.status = tk.Label(self, text=f"当前路径: {self.current_path}", anchor="w")
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        self.refresh_files()

    def _join_path(self, base, name):
        """统一路径拼接，避免出现 //xxx"""
        if base == "/":
            return f"/{name}"
        else:
            return f"{base}/{name}"

    def _is_dir(self, entry, remote_path=None):
        """健壮判断目录"""
        try:
            return stat.S_ISDIR(entry.st_mode)
        except Exception:
            try:
                if remote_path:
                    attr = self.sftp.lstat(remote_path)
                    return stat.S_ISDIR(attr.st_mode)
            except:
                return False
        return False

    def refresh_files(self):
        self.file_list.delete(0, tk.END)
        self.file_types.clear()
        try:
            entries = self.sftp.listdir_attr(self.current_path)
            for entry in entries:
                self.file_list.insert(tk.END, entry.filename)
                remote_path = self._join_path(self.current_path, entry.filename)
                self.file_types[entry.filename] = self._is_dir(entry, remote_path)
            self.status.config(text=f"当前路径: {self.current_path}")
        except Exception as e:
            messagebox.showerror("错误", f"无法列出 {self.current_path}: {e}")

    def on_double_click(self, event):
        sel = self.file_list.curselection()
        if not sel: return
        name = self.file_list.get(sel[0])
        if self.file_types.get(name, False):
            self.current_path = self._join_path(self.current_path, name)
            self.refresh_files()
        else:
            messagebox.showinfo("提示", f"{name} 是文件，不能进入")

    def go_up(self):
        if self.current_path == "/":
            messagebox.showinfo("提示", "已经在根目录，无法返回上级")
            return
        parent = os.path.dirname(self.current_path.rstrip("/"))
        if parent == "":
            parent = "/"
        self.current_path = parent
        self.refresh_files()

    def download_file(self):
        sel = self.file_list.curselection()
        if not sel: return
        filename = self.file_list.get(sel[0])
        save_path = filedialog.asksaveasfilename(initialfile=filename)
        if save_path:
            remote_path = self._join_path(self.current_path, filename)
            self.sftp.get(remote_path, save_path)
            messagebox.showinfo("完成", f"已下载 {filename}")

    def download_folder(self):
        sel = self.file_list.curselection()
        if not sel: return
        foldername = self.file_list.get(sel[0])
        if not self.file_types.get(foldername, False):
            messagebox.showinfo("提示", f"{foldername} 不是目录")
            return
        local_parent = filedialog.askdirectory()
        if local_parent:
            local_dir = os.path.join(local_parent, foldername)
            remote_path = self._join_path(self.current_path, foldername)

            # 计算总文件数
            total_files = self._count_files(remote_path)

            # 创建进度窗口
            self.progress_win = tk.Toplevel(self)
            self.progress_win.title("下载进度")
            self.progress = ttk.Progressbar(self.progress_win, length=300, mode="determinate", maximum=total_files)
            self.progress.pack(padx=20, pady=10)
            self.progress_label = tk.Label(self.progress_win, text="准备下载...")
            self.progress_label.pack(padx=20, pady=5)

            # 开始下载
            self._download_dir(remote_path, local_dir)

            # 下载完成
            self.progress_win.destroy()
            messagebox.showinfo("完成", f"已下载文件夹 {foldername}")

    def _count_files(self, remote_dir):
        count = 0
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_path = self._join_path(remote_dir, entry.filename)
            if self._is_dir(entry, remote_path):
                count += self._count_files(remote_path)
            else:
                count += 1
        return count

    def _download_dir(self, remote_dir, local_dir):
        os.makedirs(local_dir, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_path = self._join_path(remote_dir, entry.filename)
            local_path = os.path.join(local_dir, entry.filename)
            if self._is_dir(entry, remote_path):
                self._download_dir(remote_path, local_path)
            else:
                self.sftp.get(remote_path, local_path)
                # 更新进度条和标签
                self.progress.step(1)
                self.progress_label.config(text=f"正在下载: {entry.filename} ({int(self.progress['value'])}/{int(self.progress['maximum'])})")
                self.progress.update()

    def upload_file(self):
        filepath = filedialog.askopenfilename()
        if filepath:
            remote_path = self._join_path(self.current_path, os.path.basename(filepath))
            self.sftp.put(filepath, remote_path)
            self.refresh_files()
            messagebox.showinfo("完成", f"已上传 {os.path.basename(filepath)}")

    def upload_folder(self):
        folderpath = filedialog.askdirectory()
        if folderpath:
            self._upload_dir(folderpath, self.current_path)
            self.refresh_files()
            messagebox.showinfo("完成", f"已上传文件夹 {os.path.basename(folderpath)}")

    def _upload_dir(self, local_dir, remote_dir):
        try:
            self.sftp.listdir(remote_dir)
        except IOError:
            self.sftp.mkdir(remote_dir)
        for item in os.listdir(local_dir):
            local_path = os.path.join(local_dir, item)
            remote_path = self._join_path(remote_dir, item)
            if os.path.isdir(local_path):
                self._upload_dir(local_path, remote_path)
            else:
                self.sftp.put(local_path, remote_path)
