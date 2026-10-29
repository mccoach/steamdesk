import math
import os
import queue
import tkinter as tk
from contextlib import contextmanager
from tkinter import filedialog, messagebox, ttk

from app_config import AppConfigError, AppConfigStore
from downloader import InputRejected, RequestSettings
from merger import auto_find_ffmpeg
from task_engine import ExecutionRequest, TaskController


class SteamDeskApp:
    """只在主线程访问 Tk；执行事实由当前控制器拥有。"""

    def __init__(self):
        try:
            self.config_store = AppConfigStore()
        except AppConfigError as error:
            window = tk.Tk()
            window.withdraw()
            try:
                messagebox.showerror("配置错误", str(error), parent=window)
            finally:
                window.destroy()
            raise
        self.root = tk.Tk()
        self.root.title("流存台 · SteamDesk v1.0.0-20261029")
        self.root.geometry("1040x750")
        self.root.minsize(940, 680)
        self._queue = queue.Queue()
        self._controller = None
        self._snapshot = None
        self._snapshot_settings = None
        self._preview_candidate = None
        self._preview_job = False
        self._closing = False
        self._dialog_open = False
        self._after_id = None
        self._variables = {}
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()

    def run(self):
        self.root.mainloop()

    def _cfg(self, key):
        return self.config_store.get(key)

    def _variable(self, key, boolean=False):
        value = self._cfg(key)
        variable = tk.BooleanVar(value=value) if boolean else tk.StringVar(value=str(value))
        self._variables[key] = variable
        variable.trace_add("write", lambda *_, k=key, v=variable: self._save_var(k, v.get()))
        return variable

    def _build_ui(self):
        body = ttk.Frame(self.root, padding=12)
        body.pack(fill="both", expand=True)
        row = ttk.Frame(body)
        row.pack(fill="x")
        self._url_var = self._variable("last_input")
        self._source_entry = ttk.Entry(row, textvariable=self._url_var)
        self._source_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="解析链接", command=self._on_parse).pack(side="left", padx=4)
        ttk.Button(row, text="打开 M3U8", command=self._on_open_m3u8).pack(side="left", padx=4)
        ttk.Button(row, text="本地媒体目录", command=self._on_open_local_dir).pack(side="left")
        self._parse_status = ttk.Label(body, text="请先装载输入")
        self._parse_status.pack(anchor="w", pady=6)
        self._tree = ttk.Treeview(
            body, columns=("resolution", "bandwidth", "codec", "count", "duration"),
            show="headings", height=4, selectmode="browse",
        )
        for column, title in (
            ("resolution", "分辨率"), ("bandwidth", "带宽"),
            ("codec", "编码"), ("count", "片段/文件数"), ("duration", "列表时长"),
        ):
            self._tree.heading(column, text=title)
            self._tree.column(column, width=150, anchor="center")
        self._tree.pack(fill="x")
        options = ttk.Frame(body)
        options.pack(fill="x", pady=6)
        self._selected_audio = tk.BooleanVar(value=True)
        self._selected_subtitle = tk.BooleanVar(value=False)
        self._audio_cb = ttk.Checkbutton(
            options, text="获取匹配独立音轨", variable=self._selected_audio
        )
        self._sub_cb = ttk.Checkbutton(
            options, text="希望输出独立字幕", variable=self._selected_subtitle
        )
        self._audio_cb.pack(side="left")
        self._sub_cb.pack(side="left", padx=12)
        ttk.Label(options, text="字幕缺失/不支持时二次确认").pack(side="left")

        settings = ttk.LabelFrame(body, text="任务与输出设置", padding=8)
        settings.pack(fill="x")
        self._threads_var = self._variable("threads")
        self._retries_var = self._variable("retries")
        self._timeout_var = self._variable("timeout")
        self._ratelimit_var = self._variable("rate_limit_kbps")
        self._resume_var = self._variable("resume", True)
        self._cleanup_var = self._variable("cleanup_temp", True)
        self._outdir_var = self._variable("save_dir")
        self._outname_var = self._variable("output_name")
        self._outfmt_var = self._variable("output_format")
        self._ffmpeg_var = self._variable("ffmpeg_path")
        self._header_var = self._variable("headers_text")
        self._proxy_var = self._variable("proxy")
        self._cookie_var = self._variable("cookie")
        for column, (label, variable) in enumerate((
            ("线程", self._threads_var), ("重试", self._retries_var),
            ("超时秒", self._timeout_var), ("限速 KB/s", self._ratelimit_var),
        )):
            ttk.Label(settings, text=label).grid(row=0, column=column * 2, sticky="w")
            ttk.Entry(settings, textvariable=variable, width=6).grid(
                row=0, column=column * 2 + 1, padx=4
            )
        ttk.Checkbutton(settings, text="校验续传", variable=self._resume_var).grid(row=0, column=8)
        ttk.Checkbutton(settings, text="成功后清理缓存", variable=self._cleanup_var).grid(row=0, column=9)
        for index, (label, variable) in enumerate((
            ("输出目录", self._outdir_var), ("文件名", self._outname_var),
            ("FFmpeg", self._ffmpeg_var), ("请求头", self._header_var),
            ("代理", self._proxy_var), ("Cookie", self._cookie_var),
        ), start=1):
            ttk.Label(settings, text=label).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(
                settings, textvariable=variable,
                show="*" if label == "Cookie" else "",
            ).grid(row=index, column=1, columnspan=7, sticky="ew", padx=4, pady=2)
        settings.grid_columnconfigure(7, weight=1)
        ttk.Button(settings, text="浏览目录", command=self._on_browse_dir).grid(row=1, column=8, columnspan=2)
        ttk.Combobox(
            settings, textvariable=self._outfmt_var, values=(".mp4", ".mkv", ".ts"),
            state="readonly", width=6,
        ).grid(row=2, column=8, columnspan=2)
        ttk.Button(settings, text="查找 FFmpeg", command=self._on_find_ffmpeg).grid(row=3, column=8, columnspan=2)

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=8)
        self._btn_start = ttk.Button(actions, text="开始执行", command=self._on_start_execute)
        self._btn_pause = ttk.Button(actions, text="暂停", command=self._on_pause)
        self._btn_cancel = ttk.Button(actions, text="取消", command=self._on_cancel)
        for button in (self._btn_start, self._btn_pause, self._btn_cancel):
            button.pack(side="left", padx=(0, 6))
        self._status_lbl = ttk.Label(actions, text="就绪")
        self._status_lbl.pack(side="right")
        self._progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(body, variable=self._progress_var, maximum=100).pack(fill="x")
        self._stats_lbl = ttk.Label(body, text="阶段进度；100%资源获取不代表输出完成")
        self._stats_lbl.pack(anchor="w", pady=4)
        log_frame = ttk.Frame(body)
        log_frame.pack(fill="both", expand=True)
        self._log = tk.Text(log_frame, height=8, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, command=self._log.yview)
        self._log.configure(yscrollcommand=scrollbar.set)
        self._log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._refresh_controls()

    def _save_var(self, key, value):
        if key in ("threads", "retries", "timeout", "rate_limit_kbps"):
            text = str(value).strip()
            if not text.isascii() or not text.isdigit():
                return
            value = int(text)
        try:
            self.config_store.set(key, value)
        except AppConfigError as error:
            messagebox.showerror("配置保存失败", str(error), parent=self.root)

    def _log_msg(self, message):
        follow = self._log.yview()[1] >= 0.999
        self._log.configure(state="normal")
        try:
            self._log.insert("end", message + "\n")
            lines = int(self._log.index("end-1c").split(".")[0])
            maximum = max(1, int(self._cfg("log_max_lines")))
            if lines > maximum:
                self._log.delete("1.0", f"{lines - maximum + 1}.0")
            if follow:
                self._log.see("end")
        finally:
            self._log.configure(state="disabled")

    def _is_busy(self):
        # 控制器存在表示主线程仍拥有该任务的入口占用。
        # 线程退出不是释放点；消费对应终止结果后才清空控制器。
        return self._controller is not None

    def _guard_busy(self, action):
        if self._closing:
            return False
        if self._dialog_open:
            return False
        controller = self._controller
        if controller is None:
            return True
        if controller.state == "cancelling" or controller.state not in controller.ACTIVE:
            with self._dialog_scope():
                messagebox.showinfo(
                    "等待任务结束",
                    f"请等待工作线程退出及任务结果处理完成后，再执行「{action}」。",
                    parent=self.root,
                )
            return False
        with self._dialog_scope():
            accepted = messagebox.askyesno(
                "任务进行中", f"是否请求取消当前任务？\n"
                f"取消后不会自动执行「{action}」，请等待停止后重新操作。",
                parent=self.root,
            )
        if (
            accepted and self._controller is controller
            and controller.state in controller.ACTIVE
            and controller.state != "cancelling"
        ):
            self._on_cancel()
        return False

    @contextmanager
    def _dialog_scope(self):
        if self._dialog_open:
            raise RuntimeError("不能重复进入主线程模态交互")
        self._dialog_open = True
        try:
            yield
        finally:
            self._dialog_open = False

    def _refresh_controls(self):
        controller = self._controller
        state = controller.state if controller else "idle"
        busy = self._is_busy()
        self._btn_start.configure(state="disabled" if busy or self._closing else "normal")
        self._btn_pause.configure(
            text="继续" if state == "paused" else "暂停",
            state="normal" if state in ("acquiring", "paused") else "disabled",
        )
        self._btn_cancel.configure(
            state="normal" if state in TaskController.ACTIVE and state != "cancelling" else "disabled"
        )
        for widget in (self._audio_cb, self._sub_cb, self._source_entry):
            widget.configure(state="disabled" if busy else "normal")

    def _read_settings(self):
        def integer(variable, label, minimum):
            text = variable.get().strip()
            if not text.isascii() or not text.isdigit() or int(text) < minimum:
                raise InputRejected(f"{label}必须是大于等于 {minimum} 的整数")
            return int(text)
        headers = []
        for declaration in self._header_var.get().split(";"):
            if not declaration.strip():
                continue
            if ":" not in declaration:
                raise InputRejected("请求头应为名称: 内容，多个以英文分号分隔")
            name, value = declaration.split(":", 1)
            headers.append((name.strip(), value.strip()))
        cookie = self._cookie_var.get().strip()
        if cookie:
            if any(name.lower() == "cookie" for name, _ in headers):
                raise InputRejected("Cookie 不要同时在请求头和 Cookie 框重复填写")
            headers.append(("Cookie", cookie))
        return RequestSettings(
            headers=tuple(sorted(headers)), proxy=self._proxy_var.get().strip(),
            timeout=integer(self._timeout_var, "超时", 1),
            retries=integer(self._retries_var, "重试", 0),
            user_agent=self._cfg("user_agent"),
            threads=integer(self._threads_var, "线程", 1),
            rate_limit_kbps=integer(self._ratelimit_var, "限速", 0),
            resume=self._resume_var.get(),
            pool_connections=int(self._cfg("session_pool_connections")),
            pool_maxsize=int(self._cfg("session_pool_maxsize")),
            backoff_base=float(self._cfg("retry_backoff_base_sec")),
            backoff_max=float(self._cfg("retry_backoff_max_sec")),
            backoff_factor=float(self._cfg("retry_backoff_factor")),
            chunk_size=int(self._cfg("download_chunk_size")),
            success_delay=float(self._cfg("segment_success_sleep_sec")),
        )

    def _new_controller(self, preview):
        controller = TaskController(
            self._queue.put, float(self._cfg("progress_interval_sec")),
            float(self._cfg("speed_window_sec")),
        )
        self._controller = controller
        self._preview_job = preview
        self._preview_candidate = None
        self._progress_var.set(0)
        return controller

    def _on_parse(self):
        if not self._guard_busy("解析"):
            return
        source = self._url_var.get().strip()
        if not source:
            messagebox.showwarning("输入", "请输入 M3U8 地址或目录", parent=self.root)
            return
        try:
            settings = self._read_settings()
            controller = self._new_controller(True)
        except (InputRejected, ValueError) as error:
            messagebox.showwarning("参数", str(error), parent=self.root)
            return
        self._snapshot = None
        self._snapshot_settings = None
        self._tree.delete(*self._tree.get_children())
        self._parse_status.configure(text="解析中")
        controller.start_preview(source, settings)
        self._refresh_controls()

    def _on_open_m3u8(self):
        if not self._guard_busy("装载本地 m3u8"):
            return
        with self._dialog_scope():
            path = filedialog.askopenfilename(
                parent=self.root, filetypes=(("M3U8", "*.m3u8"), ("所有文件", "*.*"))
            )
        if path:
            self._url_var.set(path)
            self._on_parse()

    def _on_open_local_dir(self):
        if not self._guard_busy("装载本地片段目录"):
            return
        with self._dialog_scope():
            path = filedialog.askdirectory(parent=self.root, title="选择有序独立媒体目录")
        if path:
            self._url_var.set(path)
            self._on_parse()

    def _on_browse_dir(self):
        if self._dialog_open or self._closing:
            return
        with self._dialog_scope():
            path = filedialog.askdirectory(parent=self.root, title="选择输出目录")
        if path:
            self._outdir_var.set(path)

    def _on_find_ffmpeg(self):
        if self._dialog_open or self._closing:
            return
        with self._dialog_scope():
            accepted = messagebox.askyesno(
                "查找 FFmpeg", "是否从系统 PATH 查找 FFmpeg？", parent=self.root
            )
        if accepted:
            path = auto_find_ffmpeg()
            if path:
                self._ffmpeg_var.set(path)
            else:
                messagebox.showinfo("未找到", "请填写 FFmpeg 完整路径", parent=self.root)

    def _show_snapshot(self, snapshot):
        self._tree.delete(*self._tree.get_children())
        if snapshot.kind == "master":
            cached = dict(snapshot.cached_playlists)
            for index, variant in enumerate(snapshot.variants):
                playlist = cached.get(variant.uri)
                self._tree.insert(
                    "", "end", iid=f"stream_{index}",
                    values=(
                        variant.resolution or "待探测", variant.bandwidth,
                        variant.codecs or "待探测",
                        len(playlist.segments) if playlist else "准备时确定",
                        f"{playlist.duration:.1f}s" if playlist else "准备时确定",
                    ),
                )
        elif snapshot.kind == "media":
            self._tree.insert(
                "", "end", iid="stream_0",
                values=(
                    "待探测", "",
                    "HLS / AES-128" if any(
                        segment.resource.key is not None
                        or (
                            segment.initialization is not None
                            and segment.initialization.key is not None
                        )
                        for segment in snapshot.media.segments
                    ) else "HLS / 待探测",
                    len(snapshot.media.segments),
                    f"{snapshot.media.duration:.1f}s",
                ),
            )
        else:
            self._tree.insert(
                "", "end", iid="stream_0",
                values=("待探测", "", "有序独立媒体", len(snapshot.directory_files), ""),
            )
        self._tree.selection_set("stream_0")
        self._parse_status.configure(text="快照已装载；请求设置改变后请重新解析")

    def _output_path(self):
        directory = self._outdir_var.get().strip()
        name = self._outname_var.get().strip()
        extension = self._outfmt_var.get().strip()
        if not directory or not name:
            raise InputRejected("请填写输出目录及文件名")
        if (
            name in (".", "..") or any(character in name for character in '\\/:*?"<>|')
            or any(ord(character) < 32 for character in name)
            or name.endswith((" ", "."))
        ):
            raise InputRejected("输出文件名不合法")
        stem = name.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or stem in {
            prefix + str(index) for prefix in ("COM", "LPT") for index in range(1, 10)
        }:
            raise InputRejected("输出文件名属于 Windows 保留名称")
        if extension not in (".mp4", ".mkv", ".ts"):
            raise InputRejected("输出格式不合法")
        return os.path.abspath(os.path.join(directory, name + extension))

    def _on_start_execute(self):
        if not self._guard_busy("开始执行"):
            return
        if self._snapshot is None:
            messagebox.showwarning("输入", "请先完成解析", parent=self.root)
            return
        if self._url_var.get().strip() != self._snapshot.source:
            messagebox.showwarning(
                "输入已改变",
                "当前输入与已装载快照不同，请重新解析后再执行。",
                parent=self.root,
            )
            return
        try:
            settings = self._read_settings()
            if settings != self._snapshot_settings:
                raise InputRejected("请求设置已改变，请重新解析")
            output = self._output_path()
            selection = self._tree.selection()
            index = int(selection[0].split("_", 1)[1]) if selection else -1
            request = ExecutionRequest(
                self._snapshot, self._snapshot_settings, index,
                self._selected_audio.get(), self._selected_subtitle.get(),
                output, os.path.splitext(output)[0] + ".subtitle.vtt",
                self._ffmpeg_var.get().strip(),
                float(self._cfg("ffprobe_timeout")), self._cleanup_var.get(),
            )
            controller = self._new_controller(False)
        except (InputRejected, ValueError) as error:
            messagebox.showwarning("无法开始", str(error), parent=self.root)
            return
        controller.start_execution(request)
        self._refresh_controls()
        self._log.see("end")
        self._log_msg("[任务] 已冻结参数，开始准备")

    def _on_pause(self):
        controller = self._controller
        if controller:
            if controller.state == "paused":
                controller.resume()
            else:
                controller.pause()
            self._refresh_controls()

    def _on_cancel(self):
        controller = self._controller
        if controller:
            controller.cancel()
            self._refresh_controls()
            self._status_lbl.configure(text="正在取消，等待安全停止")

    def _on_close(self):
        if self._closing:
            return
        if self._dialog_open:
            return
        controller = self._controller
        if controller is not None:
            with self._dialog_scope():
                accepted = messagebox.askyesno(
                    "关闭程序",
                    "是否请求关闭程序？\n"
                    "仍在执行的任务将请求取消；工作线程退出并处理任务结果后关闭。\n"
                    "已提交输出保留，未完成任务的缓存保留。",
                    parent=self.root,
                )
            if not accepted:
                return
            self._closing = True
            if self._controller is controller:
                controller.cancel()
            if self._controller is None:
                self._destroy()
            else:
                self._refresh_controls()
        else:
            self._closing = True
            self._destroy()

    def _destroy(self):
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()

    def _handle_event(self, event):
        controller = self._controller
        if controller is None or event.task_id != controller.task_id:
            return
        if event.kind == "state":
            self._status_lbl.configure(
                text=TaskController.LABELS.get(controller.state, controller.state)
            )
            self._refresh_controls()
        elif event.kind == "snapshot":
            self._preview_candidate = event.payload
        elif event.kind == "log":
            self._log_msg(str(event.payload))
        elif event.kind == "progress":
            percent, stage, completed, total, transferred, speed, elapsed = event.payload
            if controller.state != "cancelling":
                self._progress_var.set(percent)
                self._status_lbl.configure(text=f"{stage}：{percent:.1f}%")
            self._stats_lbl.configure(
                text=f"资源 {completed}/{total} | 本次网络读取 {transferred / 1048576:.2f} MiB"
                f" | 速度 {speed / 1024:.1f} KiB/s | 用时 {elapsed:.1f}s"
            )
        elif event.kind == "confirm":
            if controller.state != "awaiting_confirmation" or self._closing:
                return
            # 其他主线程模态交互结束后再处理，不同时创建两个确认。
            if self._dialog_open:
                self.root.after(30, lambda e=event: self._handle_event(e))
                return
            with self._dialog_scope():
                accepted = messagebox.askyesno(
                    "确认本次输出计划", event.payload, parent=self.root
                )
            if self._controller is controller and not self._closing:
                controller.answer(accepted)
        elif event.kind == "output_committed":
            self._log_msg("[已提交] " + event.payload)
        elif event.kind == "worker_finished":
            if controller.alive:
                self.root.after(20, lambda e=event: self._handle_event(e))
                return
            result = event.payload
            preview = self._preview_job
            if preview and result.status == "succeeded" and self._preview_candidate:
                self._snapshot, self._snapshot_settings = self._preview_candidate
                self._show_snapshot(self._snapshot)
            self._controller = None
            self._refresh_controls()
            if self._closing:
                detail = (
                    f"[任务结束] task_id={event.task_id} "
                    f"status={result.status} category={result.category or 'none'}"
                )
                if result.error:
                    detail += "\n" + result.error
                if result.committed_outputs:
                    detail += (
                        "\n以下输出已经提交并保留：\n"
                        + "\n".join(result.committed_outputs)
                    )
                self._log_msg(detail)
                self._destroy()
                return
            # 模态交互期间只记录结果，避免重入第二个结果弹窗。
            show_dialog = not self._dialog_open
            if result.status == "succeeded":
                self._status_lbl.configure(text="装载完成" if preview else "全部必需输出已提交")
                if not preview and show_dialog:
                    with self._dialog_scope():
                        messagebox.showinfo(
                            "完成", "\n".join(result.committed_outputs), parent=self.root
                        )
            else:
                detail = result.error
                if result.committed_outputs:
                    detail += "\n\n以下输出已经提交并保留：\n" + "\n".join(result.committed_outputs)
                self._status_lbl.configure(
                    text="部分提交" if result.committed_outputs
                    else ("已取消" if result.status == "cancelled" else "失败")
                )
                self._log_msg(f"[{result.category}] {detail}")
                if result.status == "failed" and show_dialog:
                    with self._dialog_scope():
                        messagebox.showerror("任务未全部完成", detail, parent=self.root)

    def _poll_queue(self):
        self._after_id = None
        for _ in range(100):
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
            if self._closing and self._controller is None:
                return
        self._after_id = self.root.after(
            max(1, int(self._cfg("ui_poll_interval_ms"))), self._poll_queue
        )


if __name__ == "__main__":
    SteamDeskApp().run()
