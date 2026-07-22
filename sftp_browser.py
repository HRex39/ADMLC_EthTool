import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import paramiko, os, stat, threading, traceback, sys

class SFTPBrowser(tk.Toplevel):
    def __init__(self, master, host, user, password, port=22):
        super().__init__(master)
        self.title("SFTP 浏览器")
        self.geometry("900x600")

        # 连接 SFTP
        try:
            self.transport = paramiko.Transport((host, port))
            self.transport.connect(username=user, password=password)
            self.sftp = paramiko.SFTPClient.from_transport(self.transport)
        except Exception as e:
            messagebox.showerror("错误", f"无法连接 SFTP: {e}")
            self.destroy()
            return

        # 当前路径（保留原逻辑）
        self.start_dir = "/"
        self.current_path = self.start_dir

        # Treeview 替代 Listbox
        self.tree = ttk.Treeview(self, columns=("fullpath",), displaycolumns=())
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self.tree.heading("#0", text="远程文件系统", anchor="w")
        self.tree.bind("<<TreeviewOpen>>", self.on_open)
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)

        # 按钮区（保留并扩展）
        btn_frame = tk.Frame(self)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="一键下载售后问题数据", command=self.download_aftersales).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="一键下载标定数据", command=self.download_calib).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="下载文件", command=self.download_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="下载文件夹", command=self.download_folder).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="上传文件", command=self.upload_file).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="上传文件夹", command=self.upload_folder).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="删除", command=self.delete_item).pack(side=tk.LEFT, padx=5)
        # ttk.Button(btn_frame, text="返回上级", command=self.go_up).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="刷新当前", command=self.refresh_current).pack(side=tk.LEFT, padx=5)

        # 状态栏
        self.status = tk.Label(self, text=f"当前路径: {self.current_path}", anchor="w")
        self.status.pack(fill=tk.X, side=tk.BOTTOM)

        # 右键菜单
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="下载", command=self.download_file)
        self.menu.add_command(label="下载文件夹", command=self.download_folder)
        # self.menu.add_separator()
        # self.menu.add_command(label="上传文件到此目录", command=self.upload_file_to_selected_dir)
        # self.menu.add_command(label="上传文件夹到此目录", command=self.upload_folder_to_selected_dir)
        # self.menu.add_separator()
        # self.menu.add_command(label="删除", command=self.delete_item)

        # 进度窗口占位
        self.progress_win = None
        self.progress = None
        self.progress_label = None
        self.progress_total = 0
        self.progress_count = 0

        # 初始化根节点
        root_node = self.tree.insert("", "end", text="/", values=("/",), open=True)
        # 插入占位符以便展开时动态加载
        self.tree.insert(root_node, "end", text="dummy")
        # 不直接调用 refresh_files()，使用树的动态加载

    # ---------------- 工具函数 ----------------
    def _join_path(self, base, name):
        if base == "/":
            return f"/{name}"
        else:
            return f"{base}/{name}"

    def _safe_ui(self, fn, *a, **kw):
        """在主线程执行 UI 更新"""
        self.after(0, lambda: fn(*a, **kw))

    def _is_dir(sftp, entry=None, remote_path=None):
        """
        健壮判断远程路径是否为目录。
        - sftp: paramiko.SFTPClient 实例
        - entry: paramiko.SFTPAttributes，可选
        - remote_path: 完整远程路径
        返回 True 表示目录（包括软链接指向目录），否则 False。
        """
        try:
            mode = getattr(entry, "st_mode", None) if entry else None
            if mode and mode != 0:
                if stat.S_ISDIR(mode):
                    return True
                if stat.S_ISLNK(mode) and remote_path:
                    try:
                        target_attr = sftp.stat(remote_path)
                        return stat.S_ISDIR(target_attr.st_mode)
                    except Exception:
                        return False
                # 明确排除常见非目录类型
                if stat.S_ISREG(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) \
                or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode):
                    return False
        except Exception:
            pass

        # 兜底：只有在 st_mode 不可靠时才尝试 chdir
        if remote_path:
            try:
                sftp.chdir(remote_path)
                sftp.chdir("..")
                return True
            except Exception:
                return False
        return False    


    # ---------------- 树状加载 ----------------
    def on_open(self, event):
        node = self.tree.focus()
        if not node:
            return
        path = self.tree.item(node, "values")[0]
        # 如果只有一个占位子节点 "dummy"，则删除并加载真实子节点
        children = self.tree.get_children(node)
        if len(children) == 1 and self.tree.item(children[0], "text") == "dummy":
            self.tree.delete(children[0])
            # 异步加载，避免 UI 卡顿
            threading.Thread(target=self._populate_tree, args=(node, path), daemon=True).start()

    def _populate_tree(self, parent, remote_path):
        """
        用于在树中填充 remote_path 的子项（目录优先）。
        异常会被捕获并通过 UI 报错，但不会崩溃。
        """
        try:
            entries = self.sftp.listdir_attr(remote_path)
            dirs, others = [], []
            for entry in entries:
                child_path = self._join_path(remote_path, entry.filename)
                # 先用统一函数判断是否目录
                try:
                    if self._is_dir(entry, child_path):
                        dirs.append((entry, child_path))
                    else:
                        others.append((entry, child_path))
                except Exception:
                    # 任何异常都当作文件处理，避免中断
                    others.append((entry, child_path))

            # 先插入目录（按名字排序）
            for entry, child_path in sorted(dirs, key=lambda x: x[0].filename):
                node = self.tree.insert(parent, "end", text=f"📁 {entry.filename}", values=(child_path,))
                # 占位符，展开时再加载
                self.tree.insert(node, "end", text="dummy")

            # 再插入文件或链接（按名字排序）
            for entry, child_path in sorted(others, key=lambda x: x[0].filename):
                mode = getattr(entry, "st_mode", 0) or 0
                try:
                    if stat.S_ISLNK(mode):
                        display = f"🔗 {entry.filename}"
                    else:
                        display = f"📄 {entry.filename}"
                except Exception:
                    display = f"📄 {entry.filename}"
                self.tree.insert(parent, "end", text=display, values=(child_path,))
        except Exception as e:
            # 使用主线程弹窗
            self._safe_ui(messagebox.showerror, "错误", f"无法列出 {remote_path}: {e}")


    # ---------------- 双击与右键 ----------------
    def on_double_click(self, event):
        """
        双击行为：如果是目录则展开/折叠；如果是文件则触发下载。
        使用 lstat/stat 做最终判断，避免依赖显示文本。
        """
        node = self.tree.focus()
        if not node:
            return
        remote_path = self.tree.item(node, "values")[0]
        try:
            attr = self.sftp.lstat(remote_path)
            # 如果 lstat 表示目录（或链接指向目录），当作目录处理
            if stat.S_ISDIR(attr.st_mode):
                is_open = self.tree.item(node, "open")
                self.tree.item(node, open=not is_open)
                if not is_open:
                    # 展开时加载子节点
                    self.on_open(None)
                return
            if stat.S_ISLNK(attr.st_mode):
                # 跟随链接判断目标
                try:
                    target = self.sftp.stat(remote_path)
                    if stat.S_ISDIR(target.st_mode):
                        is_open = self.tree.item(node, "open")
                        self.tree.item(node, open=not is_open)
                        if not is_open:
                            self.on_open(None)
                        return
                except Exception:
                    # 无法跟随链接或目标不可访问，继续当文件处理
                    pass
            # 走到这里，按文件处理（触发下载）
            self.download_file()
        except Exception:
            # lstat 失败时保守当文件处理
            self.refresh_current()
            # self.download_file()


    def on_right_click(self, event):
        node = self.tree.identify_row(event.y)
        if node:
            self.tree.selection_set(node)
            self.menu.post(event.x_root, event.y_root)

    # ---------------- 刷新 / 返回上级 ----------------
    def refresh_current(self):
        node = self.tree.focus()
        if not node:
            node = self.tree.get_children("")[0]  # 根
        path = self.tree.item(node, "values")[0]
        # 删除子节点并插入占位符，然后触发展开加载
        for c in self.tree.get_children(node):
            self.tree.delete(c)
        self.tree.insert(node, "end", text="dummy")
        self.tree.item(node, open=True)
        self.on_open(None)

    def go_up(self):
        node = self.tree.focus()
        if not node:
            return
        path = self.tree.item(node, "values")[0]
        if path == "/":
            messagebox.showinfo("提示", "已经在根目录，无法返回上级")
            return
        parent_path = os.path.dirname(path.rstrip("/"))
        if parent_path == "":
            parent_path = "/"
        # 找到父节点并选中
        parent_node = self._find_node_by_path(parent_path)
        if parent_node:
            self.tree.selection_set(parent_node)
            self.tree.see(parent_node)
            self.tree.item(parent_node, open=True)
            self.refresh_current()
        else:
            # 如果找不到，刷新根
            root = self.tree.get_children("")[0]
            self.tree.selection_set(root)
            self.tree.item(root, open=True)
            self.refresh_current()

    def _find_node_by_path(self, path):
        # 遍历树查找值为 path 的节点（简单 DFS）
        def dfs(node):
            if self.tree.item(node, "values")[0] == path:
                return node
            for c in self.tree.get_children(node):
                found = dfs(c)
                if found:
                    return found
            return None
        for root in self.tree.get_children(""):
            found = dfs(root)
            if found:
                return found
        return None

    # ---------------- 下载 ----------------
    def download_file(self):
        node = self.tree.focus()
        if not node:
            return
        remote_path = self.tree.item(node, "values")[0]
        filename = os.path.basename(remote_path)
        save_path = filedialog.asksaveasfilename(initialfile=filename)
        if save_path:
            try:
                self.sftp.get(remote_path, save_path)
                messagebox.showinfo("完成", f"已下载 {filename}")
            except Exception as e:
                messagebox.showerror("错误", f"下载失败: {e}")

    def download_folder(self):
        node = self.tree.focus()
        if not node:
            return
        remote_path = self.tree.item(node, "values")[0]
        # 确认是目录
        try:
            attr = self.sftp.lstat(remote_path)
            if not stat.S_ISDIR(attr.st_mode) and not stat.S_ISLNK(attr.st_mode):
                messagebox.showinfo("提示", f"{remote_path} 不是目录或软链接，无法下载文件夹")
                return
        except Exception:
            messagebox.showinfo("提示", f"无法判断是否为目录")
            return

        local_parent = filedialog.askdirectory()
        if not local_parent:
            return
        foldername = os.path.basename(remote_path.rstrip("/"))
        local_dir = os.path.join(local_parent, foldername)

        # 计算总文件数（用于进度）
        total_files = self._count_files(remote_path)
        # 启动线程下载
        threading.Thread(target=self._download_worker, args=(remote_path, local_dir, total_files), daemon=True).start()

    def _download_worker(self, remote_path, local_dir, total_files):
        errors = []
        try:
            self._safe_ui(self._start_progress, total_files, f"下载 {remote_path}")
            self._download_dir(remote_path, local_dir, errors)
        finally:
            self._safe_ui(self._end_progress)
            if errors:
                msg = "\n".join([f"{p}: {err}" for p, err in errors])
                self._safe_ui(messagebox.showerror, "部分文件下载失败", msg)
            else:
                self._safe_ui(messagebox.showinfo, "完成", f"已下载 {remote_path}")

    def _count_files(self, remote_dir):
        count = 0
        try:
            for entry in self.sftp.listdir_attr(remote_dir):
                remote_item = self._join_path(remote_dir, entry.filename)
                if self._is_dir(entry, remote_item):
                    count += self._count_files(remote_item)
                else:
                    count += 1
        except Exception:
            pass
        return count

    def _download_dir(self, remote_dir, local_dir, errors=None):
        os.makedirs(local_dir, exist_ok=True)
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_path = self._join_path(remote_dir, entry.filename)
            local_path = os.path.join(local_dir, entry.filename)
            try:
                if stat.S_ISDIR(entry.st_mode):
                    self._download_dir(remote_path, local_path, errors)
                elif stat.S_ISLNK(entry.st_mode):
                    # 软链接，跟随目标
                    try:
                        target_attr = self.sftp.stat(remote_path)
                        if stat.S_ISDIR(target_attr.st_mode):
                            self._download_dir(remote_path, local_path, errors)
                        else:
                            self.sftp.get(remote_path, local_path)
                    except Exception as e:
                        if errors is not None:
                            errors.append((remote_path, f"软链接下载失败: {e}"))
                else:
                    try:
                        self.sftp.get(remote_path, local_path)
                    except Exception as e:
                        if errors is not None:
                            errors.append((remote_path, str(e)))
                # 每个文件完成后更新进度
                self._safe_ui(self._step_progress, remote_path)
            except Exception as e:
                if errors is not None:
                    errors.append((remote_path, str(e)))


    # ---------------- 上传 ----------------
    def upload_file(self):
        filepath = filedialog.askopenfilename()
        if not filepath:
            return
        # 上传到当前选中目录（若选中为文件则取其父目录）
        node = self.tree.focus()
        if node:
            remote_dir = self.tree.item(node, "values")[0]
            try:
                attr = self.sftp.lstat(remote_dir)
                if not stat.S_ISDIR(attr.st_mode):
                    remote_dir = os.path.dirname(remote_dir) or "/"
            except Exception:
                remote_dir = "/"
        else:
            remote_dir = "/"
        remote_path = self._join_path(remote_dir, os.path.basename(filepath))
        threading.Thread(target=self._upload_worker_single, args=(filepath, remote_path), daemon=True).start()

    def _upload_worker_single(self, local_path, remote_path):
        try:
            self._safe_ui(self._start_progress, 1, f"上传: {os.path.basename(local_path)}")
            self.sftp.put(local_path, remote_path)
            self._safe_ui(self._step_progress, os.path.basename(local_path))
            self._safe_ui(self._end_progress)
            self._safe_ui(messagebox.showinfo, "完成", f"已上传 {os.path.basename(local_path)}")
            # 刷新远程父节点
            parent_remote = os.path.dirname(remote_path) or "/"
            self._safe_ui(self._refresh_remote_node_by_path, parent_remote)
        except Exception as e:
            self._safe_ui(messagebox.showerror, "错误", f"上传失败: {e}\n{traceback.format_exc()}")

    def upload_folder(self):
        folderpath = filedialog.askdirectory()
        if not folderpath:
            return
        # 上传到当前选中目录（若选中为文件则取其父目录）
        node = self.tree.focus()
        if node:
            remote_dir = self.tree.item(node, "values")[0]
            try:
                attr = self.sftp.lstat(remote_dir)
                if not stat.S_ISDIR(attr.st_mode):
                    remote_dir = os.path.dirname(remote_dir) or "/"
            except Exception:
                remote_dir = "/"
        else:
            remote_dir = "/"
        foldername = os.path.basename(folderpath.rstrip("/"))
        target_remote = self._join_path(remote_dir, foldername)
        # 计算本地文件数
        total = self._count_local_files(folderpath)
        threading.Thread(target=self._upload_worker_dir, args=(folderpath, target_remote, total), daemon=True).start()

    def upload_folder_to_selected_dir(self):
        # 右键菜单调用：把本地选择的文件夹上传到当前右键所在远程目录
        # 这里复用 upload_folder（用户先在本地选择）
        self.upload_folder()

    def upload_file_to_selected_dir(self):
        # 右键菜单调用：上传单文件到当前远程目录
        self.upload_file()

    def _count_local_files(self, local_dir):
        count = 0
        for root, dirs, files in os.walk(local_dir):
            count += len(files)
        return count

    def _upload_worker_dir(self, local_dir, remote_target, total):
        try:
            self._safe_ui(self._start_progress, total, f"上传: {os.path.basename(local_dir)}")
            self._upload_dir(local_dir, remote_target)
            self._safe_ui(self._end_progress)
            self._safe_ui(messagebox.showinfo, "完成", f"已上传文件夹 {os.path.basename(local_dir)}")
            # 刷新远程父节点
            parent_remote = os.path.dirname(remote_target) or "/"
            self._safe_ui(self._refresh_remote_node_by_path, parent_remote)
        except Exception as e:
            self._safe_ui(messagebox.showerror, "错误", f"上传失败: {e}\n{traceback.format_exc()}")

    def _upload_dir(self, local_dir, remote_dir):
        # 确保远程目录存在
        try:
            self.sftp.listdir(remote_dir)
        except IOError:
            try:
                self.sftp.mkdir(remote_dir)
            except Exception:
                pass
        for item in sorted(os.listdir(local_dir)):
            local_path = os.path.join(local_dir, item)
            remote_path = self._join_path(remote_dir, item)
            if os.path.isdir(local_path):
                self._upload_dir(local_path, remote_path)
            else:
                self.sftp.put(local_path, remote_path)
                self._safe_ui(self._step_progress, item)

    # ---------------- 删除 ----------------
    def delete_item(self):
        node = self.tree.focus()
        if not node:
            return
        remote_path = self.tree.item(node, "values")[0]
        if not messagebox.askyesno("确认删除", f"确定要删除远程：{remote_path} 吗？"):
            return
        threading.Thread(target=self._delete_worker, args=(node, remote_path), daemon=True).start()

    def _delete_worker(self, node, remote_path):
        try:
            self._safe_ui(self._set_status, f"删除 {remote_path} ...")
            attr = self.sftp.lstat(remote_path)
            if stat.S_ISDIR(attr.st_mode):
                self._delete_dir(remote_path)
            else:
                self.sftp.remove(remote_path)
            self._safe_ui(lambda: self.tree.delete(node))
            self._safe_ui(self._set_status, "删除完成")
        except Exception as e:
            self._safe_ui(messagebox.showerror, "错误", f"删除失败: {e}\n{traceback.format_exc()}")
            self._safe_ui(self._set_status, "删除失败")

    def _delete_dir(self, remote_dir):
        for entry in self.sftp.listdir_attr(remote_dir):
            remote_item = self._join_path(remote_dir, entry.filename)
            if self._is_dir(entry, remote_item):
                self._delete_dir(remote_item)
            else:
                self.sftp.remove(remote_item)
        self.sftp.rmdir(remote_dir)

    # ---------------- 进度 UI ----------------
    def _start_progress(self, total, title="进行中"):
        if self.progress_win:
            try:
                self.progress_win.destroy()
            except:
                pass
        self.progress_total = total
        self.progress_count = 0
        self.progress_win = tk.Toplevel(self)
        self.progress_win.title(title)
        self.progress = ttk.Progressbar(self.progress_win, length=400, mode="determinate", maximum=total)
        self.progress.pack(padx=20, pady=10)
        self.progress_label = tk.Label(self.progress_win, text="准备...")
        self.progress_label.pack(padx=20, pady=6)
        self.progress_win.protocol("WM_DELETE_WINDOW", lambda: None)

    def _step_progress(self, current_name):
        self.progress_count += 1
        if self.progress:
            self.progress['value'] = self.progress_count
        if self.progress_label:
            self.progress_label.config(text=f"{current_name}  ({self.progress_count}/{self.progress_total})")

    def _end_progress(self):
        if self.progress_win:
            try:
                self.progress_win.destroy()
            except:
                pass
            self.progress_win = None
            self.progress = None
            self.progress_label = None
            self.progress_total = 0
            self.progress_count = 0

    # ---------------- 刷新辅助 ----------------
    def _refresh_remote_node_by_path(self, path):
        node = self._find_node_by_path(path)
        if node:
            # 删除子节点并插入占位符，然后展开加载
            for c in self.tree.get_children(node):
                self.tree.delete(c)
            self.tree.insert(node, "end", text="dummy")
            self.tree.item(node, open=True)
            self.on_open(None)

    def _find_node_by_path(self, path):
        # DFS 查找
        def dfs(node):
            try:
                if self.tree.item(node, "values")[0] == path:
                    return node
            except Exception:
                pass
            for c in self.tree.get_children(node):
                found = dfs(c)
                if found:
                    return found
            return None
        for root in self.tree.get_children(""):
            found = dfs(root)
            if found:
                return found
        return None

    def _refresh_local_node_by_path(self, path):
        # 简单刷新：重新初始化根节点（可按需优化）
        for node in self.tree.get_children(""):
            try:
                self.tree.delete(node)
            except:
                pass
        root_node = self.tree.insert("", "end", text="/", values=("/",), open=True)
        self.tree.insert(root_node, "end", text="dummy")

    def _set_status(self, text):
        self.status.config(text=text)

    def download_aftersales(self):
        """
        一键下载 /log, /backlog, /alglog 三个远程文件夹到本地选择的父目录。
        使用统一进度窗口显示总进度与当前文件名（后台线程执行）。
        """
        # 远程目标列表（按需可改）
        targets = ["/log", "/backlog", "/alglog"]

        # 让用户选择本地父目录
        local_parent = filedialog.askdirectory(title="选择保存售后数据的本地目录")
        if not local_parent:
            return

        # 计算总文件数（可能耗时，放到线程里）
        threading.Thread(target=self._download_aftersales_worker, args=(targets, local_parent), daemon=True).start()
    
    def download_calib(self):
        """
        一键下载 /f120calib, /params 两个远程文件夹到本地选择的父目录。
        使用统一进度窗口显示总进度与当前文件名（后台线程执行）。
        """
        # 远程目标列表（按需可改）
        targets = ["/f120calib", "/params"]

        # 让用户选择本地父目录
        local_parent = filedialog.askdirectory(title="选择保存标定数据的本地目录")
        if not local_parent:
            return

        # 计算总文件数（可能耗时，放到线程里）
        threading.Thread(target=self._download_aftersales_worker, args=(targets, local_parent), daemon=True).start()

    def _download_aftersales_worker(self, targets, local_parent):
        try:
            # 统计总文件数（对每个目标调用已有的 _count_files）
            total = 0
            for t in targets:
                try:
                    total += self._count_files(t)
                except Exception:
                    # 若统计失败，继续但不计入（保守处理）
                    pass
            if total <= 0:
                # 如果统计不到文件数，至少把进度设为 1，避免除零或无进度条
                total = 1

            # 启动统一进度窗口（在主线程）
            self._safe_ui(self._start_progress, total, "下载售后问题数据")

            # 逐个下载
            for remote_dir in targets:
                # 本地目标目录：在 local_parent 下创建同名文件夹（去掉前导 /）
                foldername = os.path.basename(remote_dir.rstrip("/")) or remote_dir.strip("/").replace("/", "_")
                local_dir = os.path.join(local_parent, foldername)
                try:
                    # 如果远程不是目录则跳过并记录
                    try:
                        attr = self.sftp.lstat(remote_dir)
                        if not stat.S_ISDIR(attr.st_mode):
                            # 不是目录，跳过
                            continue
                    except Exception:
                        # 无法判断，尝试列目录以确认
                        try:
                            _ = self.sftp.listdir(remote_dir)
                        except Exception:
                            continue

                    # 递归下载（内部会在每个文件完成时调用 _step_progress）
                    self._download_dir(remote_dir, local_dir)
                except Exception as e:
                    # 单个目录下载失败，记录并继续下一个
                    self._safe_ui(messagebox.showerror, "下载失败", f"下载 {remote_dir} 失败: {e}\n{traceback.format_exc()}")

            # 结束进度并提示完成
            self._safe_ui(self._end_progress)
            self._safe_ui(messagebox.showinfo, "完成", "售后问题数据已下载完成")
        except Exception as e:
            self._safe_ui(self._end_progress)
            self._safe_ui(messagebox.showerror, "错误", f"一键下载失败: {e}\n{traceback.format_exc()}")


# ----------------- 如果你想单独运行测试 -----------------
if __name__ == "__main__":
    # 简单交互获取连接信息
    root = tk.Tk()
    root.withdraw()
    host = tk.simpledialog.askstring("SFTP", "Host (IP or domain):")
    if not host:
        sys.exit(0)
    user = tk.simpledialog.askstring("SFTP", "Username:")
    if user is None:
        sys.exit(0)
    password = tk.simpledialog.askstring("SFTP", "Password:", show="*")
    if password is None:
        sys.exit(0)
    port = tk.simpledialog.askinteger("SFTP", "Port:", initialvalue=22)
    if port is None:
        port = 22
    app = tk.Tk()
    app.withdraw()
    win = SFTPBrowser(app, host, user, password, port)
    app.mainloop()
