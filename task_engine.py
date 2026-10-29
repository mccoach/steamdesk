import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from downloader import (
    AcquisitionError, AcquisitionStopped, DecryptionError, InputRejected,
    ResourceClient, ResourceSpec, TaskCancelled, TaskWorkspace, local_path,
)
from parser import load_snapshot, parse_media
from merger import (
    MergeExecutionError, MergeOutputError, MergeInput, TrackInput,
    SubtitleUnsupported, check_tools, commit_subtitle, merge_streams,
    same_file, validate_output_inputs, validate_webvtt,
)


@dataclass(frozen=True)
class TaskEvent:
    task_id: str
    kind: str
    payload: object = None


@dataclass(frozen=True)
class ExecutionRequest:
    snapshot: object
    settings: object
    variant_index: int
    download_audio: bool
    want_subtitle: bool
    output_path: str
    subtitle_output_path: str
    ffmpeg_path: str
    probe_timeout: float
    cleanup: bool

    def __post_init__(self):
        if not math.isfinite(self.probe_timeout) or self.probe_timeout <= 0:
            raise InputRejected("ffprobe 超时必须为有限正数")


@dataclass(frozen=True)
class TrackPlan:
    resources: tuple
    durations: tuple = ()
    initializations: tuple = ()

    def all_resources(self):
        return self.resources + tuple(
            resource for resource in self.initializations if resource is not None
        )

    def description(self):
        return (
            tuple(resource.description() for resource in self.resources),
            self.durations,
            tuple(
                resource.description() if resource is not None else None
                for resource in self.initializations
            ),
        )


def playlist_track(playlist):
    return TrackPlan(
        tuple(segment.resource for segment in playlist.segments),
        tuple(segment.duration for segment in playlist.segments),
        tuple(segment.initialization for segment in playlist.segments),
    )


@dataclass(frozen=True)
class TaskPlan:
    primary: TrackPlan
    audio: object
    primary_kind: str
    output_path: str
    subtitle_path: str
    subtitle_text: str
    protected_sources: tuple
    identity: str


@dataclass(frozen=True)
class TaskResult:
    status: str
    committed_outputs: tuple
    error: str = ""
    category: str = ""


class TaskController:
    ACTIVE = frozenset((
        "preparing", "awaiting_confirmation", "acquiring", "paused",
        "packaging", "validating", "committing", "cancelling",
    ))
    LABELS = {
        "preparing": "准备", "awaiting_confirmation": "等待确认",
        "acquiring": "获取资源", "paused": "已暂停",
        "packaging": "封装", "validating": "验证",
        "committing": "提交", "cancelling": "正在取消",
        "succeeded": "完成", "failed": "失败", "cancelled": "已取消",
    }

    def __init__(self, emit, progress_interval=0.2, speed_window=3.0):
        if (
            not math.isfinite(progress_interval) or progress_interval <= 0
            or not math.isfinite(speed_window) or speed_window <= 0
        ):
            raise InputRejected("进度刷新和速度窗口必须为有限正数")
        self.task_id = uuid.uuid4().hex
        self._emit = emit
        self._condition = threading.Condition(threading.RLock())
        self._state = "idle"
        self._cancelled = False
        self._abort_acquisition = False
        self._answer = None
        self._outputs = []
        self._thread = None
        self._started = False
        self._interval = progress_interval
        self._window = speed_window
        self._records = deque()
        self._bytes = 0
        self._started_at = time.monotonic()
        self._last_progress_at = 0.0

    @property
    def state(self):
        with self._condition:
            return self._state

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def busy(self):
        return self.state in self.ACTIVE or self.alive

    def event(self, kind, payload=None):
        self._emit(TaskEvent(self.task_id, kind, payload))

    def phase(self, state):
        with self._condition:
            if state not in self.ACTIVE:
                raise ValueError("非法执行阶段")
            self.checkpoint()
            self._state = state
            self._condition.notify_all()
            self.event("state", state)

    def notice(self, message):
        self.event("log", message)

    def record_bytes(self, length):
        now = time.monotonic()
        with self._condition:
            self._bytes += length
            self._records.append((now, length))
            cutoff = now - self._window
            while self._records and self._records[0][0] < cutoff:
                self._records.popleft()

    def progress(self, percent, stage, completed=0, total=0):
        now = time.monotonic()
        with self._condition:
            if percent < 100 and now - self._last_progress_at < self._interval:
                return
            self._last_progress_at = now
            cutoff = now - self._window
            while self._records and self._records[0][0] < cutoff:
                self._records.popleft()
            elapsed = now - self._started_at
            span = now - self._records[0][0] if self._records else 0
            speed = sum(length for _, length in self._records) / span if span > 0.05 else 0
            payload = (
                max(0.0, min(100.0, percent)), stage, completed, total,
                self._bytes, speed, elapsed,
            )
        self.event("progress", payload)

    def _check_stopped_locked(self):
        if self._cancelled:
            raise TaskCancelled("用户取消")
        if self._abort_acquisition:
            raise AcquisitionStopped("同批资源已经停止")

    def check_stopped(self):
        """协调线程观察停止事实，不因暂停而失去失败观察能力。"""
        with self._condition:
            self._check_stopped_locked()

    def checkpoint(self):
        with self._condition:
            while self._state == "paused" and not self._cancelled and not self._abort_acquisition:
                self._condition.wait()
            self._check_stopped_locked()

    def wait_delay(self, delay):
        deadline = time.monotonic() + max(0, delay)
        with self._condition:
            while True:
                self.checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(timeout=min(remaining, 0.2))

    def pause(self):
        with self._condition:
            if self._state == "acquiring" and not self._cancelled:
                self._state = "paused"
                self._condition.notify_all()
                self.event("state", "paused")

    def resume(self):
        with self._condition:
            if self._state == "paused" and not self._cancelled:
                self._state = "acquiring"
                self._condition.notify_all()
                self.event("state", "acquiring")

    def cancel(self):
        with self._condition:
            if self._state not in self.ACTIVE or self._cancelled:
                return
            self._cancelled = True
            self._state = "cancelling"
            self._condition.notify_all()
            self.event("state", "cancelling")

    def stop_acquisition(self):
        with self._condition:
            self._abort_acquisition = True
            self._condition.notify_all()

    def answer(self, accepted):
        with self._condition:
            if self._state == "awaiting_confirmation" and not self._cancelled:
                self._answer = bool(accepted)
                self._condition.notify_all()

    def confirm(self, message):
        with self._condition:
            self._answer = None
            self.phase("awaiting_confirmation")
            self.event("confirm", message)
            while self._answer is None:
                self.checkpoint()
                self._condition.wait(timeout=0.2)
            accepted = self._answer
        if not accepted:
            raise TaskCancelled("用户放弃本次执行")
        self.phase("preparing")

    def output_committed(self, path):
        with self._condition:
            self._outputs.append(path)
        self.event("output_committed", path)

    def commit_output(self, path, submit):
        """将取消裁决、文件提交和提交事实记录排序为一个控制事务。

        submit 由输出事务提供，负责实际文件替换。
        本方法不构建路径、不验证媒体，也不承担临时文件清理。
        取消先获得锁则拒绝提交；提交先获得锁则完成并记录该输出，
        后续取消不能把它描述成未提交。
        """
        with self._condition:
            self.checkpoint()
            submit()
            self.output_committed(path)

    def start_preview(self, source, settings):
        self._start(self._preview, (source, settings))

    def start_execution(self, request):
        self._start(self._execute, (request,))

    def _start(self, operation, arguments):
        with self._condition:
            if self._started:
                raise RuntimeError("控制器不能重复启动")
            self._started = True
            self.phase("preparing")
            self._thread = threading.Thread(
                target=self._worker, args=(operation, arguments),
                name="steamdesk-task-" + self.task_id, daemon=True,
            )
            self._thread.start()

    def _worker(self, operation, arguments):
        result = None
        try:
            operation(*arguments)
            result = TaskResult("succeeded", tuple(self._outputs))
        except TaskCancelled as error:
            result = TaskResult("cancelled", tuple(self._outputs), str(error), "cancelled")
        except InputRejected as error:
            result = TaskResult("failed", tuple(self._outputs), str(error), "input")
        except DecryptionError as error:
            result = TaskResult("failed", tuple(self._outputs), str(error), "decryption")
        except AcquisitionError as error:
            result = TaskResult("failed", tuple(self._outputs), str(error), "acquisition")
        except MergeExecutionError as error:
            result = TaskResult("failed", tuple(self._outputs), str(error), "packaging")
        except MergeOutputError as error:
            result = TaskResult("failed", tuple(self._outputs), str(error), "validation_or_commit")
        except OSError:
            result = TaskResult(
                "failed", tuple(self._outputs),
                "文件系统操作失败，请检查路径、权限和磁盘空间", "filesystem",
            )
        finally:
            if result is None:
                result = TaskResult(
                    "failed", tuple(self._outputs),
                    "工作线程出现未预期程序缺陷，请查看原始线程异常",
                    "program_defect",
                )
            with self._condition:
                self._state = result.status
                self._condition.notify_all()
            self.event("worker_finished", result)

    def _preview(self, source, settings):
        client = ResourceClient(settings, self)
        try:
            snapshot = load_snapshot(source, client)
            self.checkpoint()
            self.event("snapshot", (snapshot, settings))
        finally:
            client.close()

    def _playlist(self, uri, snapshot, client, loaded):
        if uri in loaded:
            return loaded[uri]
        text, final_uri = client.read_text(uri)
        playlist = parse_media(text, final_uri)
        loaded[uri] = playlist
        return playlist

    def _plan(self, request, client):
        snapshot = request.snapshot
        loaded = dict(snapshot.cached_playlists)
        output = os.path.abspath(request.output_path)
        subtitle_path = os.path.abspath(request.subtitle_output_path)
        protected = []
        audio_plan = None
        primary_kind = "auto"
        subtitle_candidates = ()
        if snapshot.kind == "directory":
            files = tuple(
                path for path in snapshot.directory_files if not same_file(path, output)
            )
            video = tuple(
                path for path in files if os.path.splitext(path)[1].lower() in (".ts", ".mp4", ".mkv")
            )
            audio = tuple(
                path for path in files if os.path.splitext(path)[1].lower() in (".aac", ".m4a")
            )
            if video and audio:
                raise InputRejected("目录混合主媒体和独立音频，缺少明确关联，不能猜测")
            selected = video or audio
            if not selected:
                raise InputRejected("排除输出后没有媒体")
            primary = TrackPlan(tuple(ResourceSpec(Path(path).as_uri()) for path in selected))
            if not video:
                primary_kind = "audio"
            protected.extend(selected)
        else:
            if snapshot.kind == "master":
                if not 0 <= request.variant_index < len(snapshot.variants):
                    raise InputRejected("未选择有效主媒体")
                variant = snapshot.variants[request.variant_index]
                media = self._playlist(variant.uri, snapshot, client, loaded)
                if media.uri.startswith("file:"):
                    protected.append(local_path(media.uri))
                audio_candidates = tuple(
                    track for track in snapshot.audio
                    if track.group_id == variant.audio_group and track.uri
                )
                if request.download_audio:
                    if len(audio_candidates) > 1:
                        raise InputRejected("匹配组有多个独立音轨，当前不能隐式任选")
                    if audio_candidates:
                        audio_media = self._playlist(
                            audio_candidates[0].uri, snapshot, client, loaded
                        )
                        audio_plan = playlist_track(audio_media)
                        if audio_media.uri.startswith("file:"):
                            protected.append(local_path(audio_media.uri))
                subtitle_candidates = tuple(
                    track for track in snapshot.subtitles
                    if track.group_id == variant.subtitle_group and track.uri
                )
            else:
                media = snapshot.media
            primary = playlist_track(media)
            if snapshot.uri.startswith("file:"):
                protected.append(local_path(snapshot.uri))

        subtitle_text = ""
        if request.want_subtitle:
            if not subtitle_candidates:
                self.confirm(
                    "你希望输出字幕，但所选源未声明可获取的独立字幕。\n"
                    "这不代表画面没有烧录字幕。\n\n是否继续，仅生成主媒体？"
                )
            elif len(subtitle_candidates) > 1:
                self.confirm(
                    "匹配组包含多个字幕，当前尚未提供语言选择。\n\n"
                    "是否继续，本次不输出独立字幕？"
                )
            else:
                text, final_uri = client.read_text(subtitle_candidates[0].uri)
                try:
                    subtitle_text = validate_webvtt(text)
                except SubtitleUnsupported as error:
                    self.confirm(
                        f"源提供字幕，但当前不能正确处理该结构：\n{error}\n\n"
                        "是否继续，仅生成主媒体？"
                    )
                if final_uri.startswith("file:"):
                    protected.append(local_path(final_uri))

        resources = primary.all_resources() + (
            audio_plan.all_resources() if audio_plan else ()
        )
        for resource in resources:
            for uri in (
                resource.uri,
                resource.key.uri if resource.key is not None else "",
            ):
                if uri.startswith("file:"):
                    protected.append(local_path(uri))
        protected = list(dict.fromkeys(protected))
        validate_output_inputs(output, protected)
        if subtitle_text:
            validate_output_inputs(subtitle_path, protected + [output])
        if os.path.exists(output):
            self.confirm(f"主媒体输出已存在：\n{output}\n\n是否覆盖？")
        if subtitle_text and os.path.exists(subtitle_path):
            self.confirm(f"本次字幕属于必需输出，目标已存在：\n{subtitle_path}\n\n是否覆盖？")
        identity = hashlib.sha256(json.dumps(
            {
                "version": 2,
                "primary": primary.description(),
                "audio": audio_plan.description() if audio_plan else None,
                "kind": primary_kind, "settings": request.settings.identity(),
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")).hexdigest()
        return TaskPlan(
            primary, audio_plan, primary_kind, output,
            subtitle_path if subtitle_text else "", subtitle_text,
            tuple(protected), identity,
        )

    def _acquire(self, plan, client, workspace):
        resources = tuple(dict.fromkeys(
            plan.primary.all_resources()
            + (plan.audio.all_resources() if plan.audio else ())
        ))
        if not resources:
            raise InputRejected("计划没有资源")
        results = {}
        succeeded = False
        self.phase("acquiring")
        pool = ThreadPoolExecutor(max_workers=client.settings.threads)
        active = {}
        next_index = 0
        try:
            while next_index < len(resources) or active:
                self.check_stopped()
                if active:
                    completed, _ = wait(
                        tuple(active), timeout=0.1,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        resource = active.pop(future)
                        results[resource] = future.result()
                    if completed:
                        self.progress(
                            len(results) / len(resources) * 100,
                            "获取资源", len(results), len(resources),
                        )
                with self._condition:
                    self._check_stopped_locked()
                    if self._state != "paused":
                        while (
                            next_index < len(resources)
                            and len(active) < client.settings.threads
                        ):
                            resource = resources[next_index]
                            future = pool.submit(client.acquire, resource, workspace)
                            active[future] = resource
                            next_index += 1
                    elif not active:
                        self._condition.wait(timeout=0.1)
            self.checkpoint()
            succeeded = True
            return results
        finally:
            if not succeeded:
                self.stop_acquisition()
                for future in active:
                    future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)

    def _execute(self, request):
        client = ResourceClient(request.settings, self)
        try:
            ffmpeg, ffprobe = check_tools(request.ffmpeg_path, request.probe_timeout)
            self.checkpoint()
            tool_paths = (ffmpeg, ffprobe)
            validate_output_inputs(request.output_path, tool_paths)
            if request.want_subtitle:
                validate_output_inputs(request.subtitle_output_path, tool_paths)

            plan = self._plan(request, client)
            self.event("plan", plan)
            client.prepare_keys(
                plan.primary.all_resources()
                + (plan.audio.all_resources() if plan.audio else ())
            )
            self.checkpoint()
            with TaskWorkspace(
                os.path.dirname(plan.output_path), plan.identity, self.notice
            ) as workspace:
                acquired = self._acquire(plan, client, workspace)

                def track_input(track):
                    return TrackInput(
                        tuple(acquired[resource] for resource in track.resources),
                        track.durations,
                        tuple(
                            acquired[resource] if resource is not None else ""
                            for resource in track.initializations
                        ),
                    )

                primary = track_input(plan.primary)
                audio = track_input(plan.audio) if plan.audio else None
                protected = (
                    tuple(acquired.values()) + plan.protected_sources + tool_paths
                    + (workspace.manifest_path, workspace.lock_path)
                )
                validate_output_inputs(plan.output_path, protected)
                if plan.subtitle_text:
                    validate_output_inputs(plan.subtitle_path, protected + (plan.output_path,))
                merge_streams(
                    MergeInput(
                        primary, plan.output_path, ffmpeg, ffprobe,
                        audio, plan.primary_kind,
                    ), self, request.probe_timeout,
                )
                if plan.subtitle_text:
                    self.checkpoint()
                    commit_subtitle(plan.subtitle_text, plan.subtitle_path, self)
                if request.cleanup:
                    workspace.cleanup(tuple(acquired.values()))
                self.progress(100, "全部必需输出已提交")
        finally:
            client.close()