import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from downloader import InputRejected


class MergeExecutionError(RuntimeError):
    """媒体工具启动、读取或执行失败。"""


class MergeOutputError(RuntimeError):
    """输出验证或提交失败。"""


class SubtitleUnsupported(InputRejected):
    """字幕结构不属于当前正式支持范围。"""


@dataclass(frozen=True)
class TrackInput:
    files: tuple
    durations: tuple = ()
    initializations: tuple = ()


@dataclass(frozen=True)
class MediaMetadata:
    streams: tuple
    format_name: str
    duration: float


@dataclass(frozen=True)
class MergeInput:
    primary: TrackInput
    output_path: str
    ffmpeg_path: str
    ffprobe_path: str
    audio: object = None
    primary_kind: str = "auto"


def find_ffmpeg(custom_path):
    return custom_path if custom_path and os.path.isfile(custom_path) else None


def auto_find_ffmpeg():
    return shutil.which("ffmpeg")


def same_file(first, second):
    if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
        return True
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False


def validate_output_inputs(output, files):
    for path in files:
        if same_file(output, path):
            raise InputRejected("输出不能覆盖输入素材或其同文件别名")


def _flags():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _tool_run(command, timeout):
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", creationflags=_flags(), timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MergeExecutionError("媒体工具无法启动或超过等待时限") from error
    if result.returncode:
        raise MergeExecutionError(
            f"媒体工具退出码 {result.returncode}：\n{result.stderr[-3000:]}"
        )
    return result.stdout


def check_tools(ffmpeg_path, timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise InputRejected("ffprobe 超时必须为有限正数")
    ffmpeg = os.path.abspath(ffmpeg_path) if ffmpeg_path else ""
    if not find_ffmpeg(ffmpeg):
        raise InputRejected("请配置可用 FFmpeg")
    ffprobe = os.path.join(
        os.path.dirname(ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe"
    )
    if not os.path.isfile(ffprobe):
        raise InputRejected("正式执行需要 FFmpeg 同目录的 ffprobe")
    _tool_run([ffmpeg, "-version"], timeout)
    _tool_run([ffprobe, "-version"], timeout)
    return ffmpeg, ffprobe


def probe_media(path, ffprobe, timeout):
    # 获取器已经交付本地资源。探测器不得另行访问 HTTP 等网络协议。
    text = _tool_run([
        ffprobe, "-v", "error", "-protocol_whitelist", "file",
        "-show_streams", "-show_format", "-of", "json", path,
    ], timeout)
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise MergeOutputError("ffprobe 返回非法 JSON") from error
    if not isinstance(document, dict):
        raise MergeOutputError("ffprobe 返回结构不合法")
    streams = document.get("streams")
    container = document.get("format")
    if (
        not isinstance(streams, list) or not isinstance(container, dict)
        or any(not isinstance(stream, dict) for stream in streams)
        or not isinstance(container.get("format_name"), str)
    ):
        raise MergeOutputError("媒体轨道或容器信息不合法")
    try:
        duration = float(container.get("duration", "0"))
    except (ValueError, TypeError) as error:
        raise MergeOutputError("媒体时长不可解析") from error
    if not math.isfinite(duration) or duration < 0:
        raise MergeOutputError("媒体时长不合法")
    return MediaMetadata(tuple(streams), container["format_name"], duration)


def _stream(metadata, kind):
    return next(
        (stream for stream in metadata.streams if stream.get("codec_type") == kind),
        None,
    )


def _signature(metadata, kinds):
    return tuple(
        (
            kind, stream.get("codec_name"), stream.get("profile"),
            stream.get("width"), stream.get("height"), stream.get("pix_fmt"),
            stream.get("sample_rate"), stream.get("channels"),
        )
        for kind in kinds for stream in [_stream(metadata, kind)]
        if stream is not None
    )


def _copy_media(paths, target, control):
    with open(target, "wb") as output:
        for path in paths:
            with open(path, "rb") as source:
                while True:
                    control.checkpoint()
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)


def _probe_fragment(path, initialization, ffprobe, timeout, control):
    if not initialization:
        return probe_media(path, ffprobe, timeout)
    # 初始化与媒体片段一起构成可探测对象，裸 moof 不是独立 MP4。
    directory = tempfile.mkdtemp(prefix="._steamdesk_probe_")
    try:
        combined = os.path.join(directory, "fragment.mp4")
        _copy_media((initialization, path), combined, control)
        return probe_media(combined, ffprobe, timeout)
    finally:
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except OSError:
            control.notice("本次 fMP4 探测临时目录清理失败")


def _prepare_track(track, kind, ffprobe, timeout, control):
    if not track.files:
        raise InputRejected("没有媒体输入")
    if track.durations and (
        len(track.durations) != len(track.files)
        or any(not math.isfinite(value) or value <= 0 for value in track.durations)
    ):
        raise InputRejected("片段时长与播放顺序不一致")
    if track.initializations and (
        not track.durations or len(track.initializations) != len(track.files)
    ):
        raise InputRejected("初始化关联必须对应 HLS 片段顺序")
    maps = track.initializations or ("",) * len(track.files)
    if any(maps) and not all(maps):
        raise InputRejected("同轨道不能混合无初始化和有初始化片段")
    for path in dict.fromkeys(track.files + tuple(path for path in maps if path)):
        control.checkpoint()
        properties = os.stat(path)
        if not stat.S_ISREG(properties.st_mode) or properties.st_size <= 0:
            raise InputRejected("媒体及初始化输入必须是非空普通文件")
    metadata = []
    for path, initialization in zip(track.files, maps):
        control.checkpoint()
        try:
            item = _probe_fragment(path, initialization, ffprobe, timeout, control)
        except (MergeExecutionError, MergeOutputError) as error:
            raise InputRejected(f"输入媒体验证失败：{error}") from error
        metadata.append(item)
        control.checkpoint()
    first = metadata[0]
    if kind == "auto":
        kind = "video" if _stream(first, "video") else "audio"
    if kind not in ("video", "audio") or _stream(first, kind) is None:
        raise InputRejected("输入没有本次必需的媒体轨道")
    reference = _signature(first, ("video", "audio"))
    indirect_formats = {"hls", "concat", "dash", "sdp", "image2", "image2pipe"}
    packed_audio = ("aac", "mp3", "ac3", "eac3")
    first_family = None
    for item in metadata:
        formats = set(item.format_name.split(","))
        if formats & indirect_formats:
            raise InputRejected(
                "媒体资源实际是列表、会话或序列描述，不是当前支持的独立媒体；"
                "请通过正式列表入口提供输入"
            )
        if _signature(item, ("video", "audio")) != reference:
            raise InputRejected("输入编码或轨道结构变化，当前不自动转码")
        if track.durations:
            if any(maps):
                if not formats & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
                    raise InputRejected("MAP 关联媒体不是可验证的 fMP4")
                family = "fmp4"
            elif "mpegts" in formats:
                family = "mpegts"
            elif formats.intersection(packed_audio) and _stream(item, "video") is None:
                family = next(name for name in packed_audio if name in formats)
            else:
                raise InputRejected("HLS 片段不是普通 TS、受支持的原始音频或带 MAP 的 fMP4")
            if first_family is None:
                first_family = family
            elif family != first_family:
                raise InputRejected("同轨道片段封装类型变化，当前不自动转换时间线")
    if track.durations:
        duration = sum(track.durations)
        tolerance = max(2.0, duration * 0.02, max(track.durations))
    else:
        if any(item.duration <= 0 for item in metadata):
            raise InputRejected("无法确定独立文件的合理时长")
        duration = sum(item.duration for item in metadata)
        tolerance = max(2.0, duration * 0.02)
    return first, kind, duration, tolerance


def _write_concat_file(paths, target_path):
    with open(target_path, "w", encoding="utf-8", newline="\n") as target:
        for path in paths:
            escaped = os.path.abspath(path).replace("\\", "/").replace("'", r"'\''")
            if "\r" in escaped or "\n" in escaped:
                raise InputRejected("媒体路径包含换行，不能安全生成 concat 描述")
            target.write(f"file '{escaped}'\n")


def _ffmpeg_file_reference(path):
    absolute = os.path.abspath(path)
    if os.name == "nt":
        absolute = absolute.replace("\\", "/")
    if "\r" in absolute or "\n" in absolute:
        raise InputRejected("媒体路径包含换行，不能安全生成 HLS 描述")
    # FFmpeg file 协议使用原生路径，不使用浏览器式百分号编码。
    # 例如 file:C:/素材/中文 空格%20.ts 或 file:/tmp/中文 空格%20.ts。
    return "file:" + absolute


def _write_hls(track, target_path):
    references = tuple(_ffmpeg_file_reference(path) for path in track.files)
    maps = track.initializations or ("",) * len(track.files)
    if len(maps) != len(track.files):
        raise InputRejected("初始化关联与片段数量不一致")
    map_references = tuple(
        _ffmpeg_file_reference(path) if path else "" for path in maps
    )
    if any('"' in reference for reference in map_references):
        raise InputRejected("初始化路径包含双引号，不能安全生成 MAP")
    with open(target_path, "w", encoding="utf-8", newline="\n") as target:
        target.write(f"#EXTM3U\n#EXT-X-VERSION:{7 if any(maps) else 3}\n")
        target.write(
            f"#EXT-X-TARGETDURATION:{math.ceil(max(track.durations))}\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
        )
        previous_map = ""
        for reference, duration, initialization in zip(
            references, track.durations, map_references
        ):
            if initialization and initialization != previous_map:
                target.write(f'#EXT-X-MAP:URI="{initialization}"\n')
                previous_map = initialization
            target.write(f"#EXTINF:{duration:.9f},\n{reference}\n")
        target.write("#EXT-X-ENDLIST\n")


def _input_arguments(track, directory, label, format_name, control):
    if track.durations:
        formats = set(format_name.split(","))
        if any(track.initializations):
            extension = ".m4s"
        elif "mpegts" in formats:
            extension = ".ts"
        else:
            names = tuple(
                name for name in ("aac", "mp3", "ac3", "eac3") if name in formats
            )
            if not names:
                raise InputRejected("已验证的 HLS 片段格式没有包装输入契约")
            extension = "." + names[0]
        # 格式已验证后建立正式包装副本，避免 .media 扩展名影响 HLS
        # 解复用器。控制器显式传递，不使用函数属性或全局任务状态。
        aliases = {}

        def alias(source, suffix):
            identity = (source, suffix)
            if identity not in aliases:
                target = os.path.join(
                    directory, f"{label}_{len(aliases):08d}{suffix}"
                )
                _copy_media((source,), target, control)
                aliases[identity] = target
            return aliases[identity]

        files = tuple(alias(path, extension) for path in track.files)
        maps = tuple(
            alias(path, ".mp4") if path else ""
            for path in track.initializations
        )
        path = os.path.join(directory, label + ".m3u8")
        _write_hls(TrackInput(files, track.durations, maps), path)
        return [
            "-protocol_whitelist", "file", "-allowed_extensions", "ALL",
            "-f", "hls", "-i", path,
        ]
    path = os.path.join(directory, label + "_concat_list.txt")
    _write_concat_file(track.files, path)
    return [
        "-protocol_whitelist", "file",
        "-f", "concat", "-safe", "0", "-i", path,
    ]


def _stop_process(process):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def _run_ffmpeg(command, control, duration):
    control.checkpoint()
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1, creationflags=_flags(),
        )
    except OSError as error:
        raise MergeExecutionError("FFmpeg 无法启动") from error
    messages = queue.Queue()
    tail = []

    def reader():
        ended = False
        try:
            for line in process.stdout:
                messages.put(("line", line))
            ended = True
        except OSError as error:
            messages.put(("error", error))
        finally:
            messages.put(("end", ended))

    thread = threading.Thread(target=reader, daemon=True)
    ended = False
    percent = 0.0
    try:
        thread.start()
        while True:
            control.checkpoint()
            try:
                kind, payload = messages.get(timeout=0.1)
            except queue.Empty:
                kind, payload = "heartbeat", None
            if kind == "error":
                raise MergeExecutionError("FFmpeg 输出读取失败") from payload
            if kind == "end":
                if not payload:
                    raise MergeExecutionError("FFmpeg 输出线程异常终止")
                ended = True
            elif kind == "line":
                tail.append(payload)
                if len(tail) > 300:
                    del tail[:100]
                if payload.startswith("out_time_us="):
                    try:
                        elapsed = int(payload.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    percent = max(percent, min(99.0, max(0, elapsed) / duration * 99))
                    control.progress(percent, "封装")
            if process.poll() is not None and ended:
                control.checkpoint()
                if process.returncode:
                    raise MergeExecutionError(
                        f"FFmpeg 退出码 {process.returncode}：\n"
                        + "".join(tail)[-4000:]
                    )
                return
    finally:
        try:
            _stop_process(process)
        finally:
            if thread.ident is not None:
                thread.join()
            process.stdout.close()


def _validate_staged_output(path):
    metadata = os.stat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise MergeOutputError("待提交输出不是非空普通文件")


@contextmanager
def _atomic_output(output_path, control):
    control.checkpoint()
    target = os.path.abspath(output_path)
    temporary = None
    try:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=os.path.dirname(target), prefix="._merge_output_",
                suffix=os.path.splitext(target)[1], delete=False,
            ) as handle:
                temporary = handle.name
        except OSError as error:
            raise MergeOutputError("无法创建输出临时文件") from error
        yield temporary
        control.checkpoint()
        _validate_staged_output(temporary)
        try:
            control.commit_output(
                target, lambda: os.replace(temporary, target)
            )
        except OSError as error:
            raise MergeOutputError("输出提交失败，原目标未被本次替换") from error
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                control.notice("输出临时文件清理失败")


def _validate_result(metadata, extension, video, audio, duration, tolerance):
    accepted = {
        ".mp4": {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"},
        ".mkv": {"matroska", "webm"}, ".ts": {"mpegts"},
    }
    if not set(metadata.format_name.split(",")) & accepted[extension]:
        raise MergeOutputError("输出实际容器与要求不符")
    if video and _stream(metadata, "video") is None:
        raise MergeOutputError("输出缺少必需视频轨道")
    if audio and _stream(metadata, "audio") is None:
        raise MergeOutputError("输出缺少必需音频轨道")
    if metadata.duration <= 0 or abs(metadata.duration - duration) > tolerance:
        raise MergeOutputError(
            f"输出时长不符合计划：预期 {duration:.3f} 秒，"
            f"实际 {metadata.duration:.3f} 秒，容差 {tolerance:.3f} 秒"
        )


def merge_streams(request, control, probe_timeout):
    extension = os.path.splitext(request.output_path)[1].lower()
    if extension not in (".mp4", ".mkv", ".ts"):
        raise InputRejected("输出容器不受支持")
    tracks = (request.primary,) + ((request.audio,) if request.audio else ())
    inputs = tuple(
        path for track in tracks
        for path in track.files + tuple(path for path in track.initializations if path)
    )
    validate_output_inputs(
        request.output_path,
        inputs + (request.ffmpeg_path, request.ffprobe_path),
    )
    control.phase("validating")
    primary, kind, duration, tolerance = _prepare_track(
        request.primary, request.primary_kind, request.ffprobe_path,
        probe_timeout, control,
    )
    required_video = kind == "video"
    required_audio = kind == "audio" or _stream(primary, "audio") is not None
    audio_metadata = None
    if request.audio:
        if not required_video:
            raise InputRejected("纯音频主媒体不能再次附加独立音轨")
        audio_metadata, _, audio_duration, audio_tolerance = _prepare_track(
            request.audio, "audio", request.ffprobe_path, probe_timeout, control,
        )
        if abs(audio_duration - duration) > max(tolerance, audio_tolerance):
            raise InputRejected("独立音视频时长超出当前共同时间线契约")
        required_audio = True
    parent = os.path.dirname(os.path.abspath(request.output_path))
    os.makedirs(parent, exist_ok=True)
    work = tempfile.mkdtemp(prefix="._merge_work_", dir=parent)
    try:
        control.phase("packaging")
        with _atomic_output(request.output_path, control) as staged:
            command = [
                request.ffmpeg_path, "-y", "-hide_banner", "-nostats",
                "-loglevel", "warning", "-progress", "pipe:1",
            ]
            command += _input_arguments(
                request.primary, work, "primary", primary.format_name, control
            )
            if request.audio:
                command += _input_arguments(
                    request.audio, work, "audio", audio_metadata.format_name, control
                )
            if required_video:
                command += ["-map", "0:v:0"]
            if required_audio:
                command += ["-map", "1:a:0" if request.audio else "0:a:0"]
            command += ["-c", "copy"]
            if extension == ".mp4":
                command += ["-movflags", "+faststart"]
            command.append(staged)
            _run_ffmpeg(command, control, duration)
            control.phase("validating")
            try:
                metadata = probe_media(staged, request.ffprobe_path, probe_timeout)
            except MergeExecutionError as error:
                raise MergeOutputError(f"输出媒体验证失败：{error}") from error
            control.checkpoint()
            _validate_result(
                metadata, extension, required_video, required_audio,
                duration, tolerance,
            )
            control.phase("committing")
        return request.output_path
    finally:
        try:
            shutil.rmtree(work)
        except FileNotFoundError:
            pass
        except OSError:
            control.notice("本次合并工作目录清理失败")


def validate_webvtt(text):
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    blocks = re.split(r"\n[ \t]*\n", normalized)
    if not blocks or not blocks[0]:
        raise SubtitleUnsupported("字幕为空")
    header = blocks[0].splitlines()[0]
    if header != "WEBVTT" and not header.startswith(("WEBVTT ", "WEBVTT\t")):
        raise SubtitleUnsupported("当前仅支持普通单文件 WebVTT")
    if "X-TIMESTAMP-MAP" in normalized:
        raise SubtitleUnsupported("字幕包含尚未实现的时间映射")
    timestamp = r"(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})"
    pattern = re.compile("^" + timestamp + r"\s+-->\s+" + timestamp + r"(?:[ \t]+.*)?$")
    cues = 0
    for block in blocks[1:]:
        lines = block.splitlines()
        if not lines:
            continue
        if lines[0] == "NOTE" or lines[0].startswith(("NOTE ", "NOTE\t")):
            continue
        if lines[0] in ("STYLE", "REGION"):
            raise SubtitleUnsupported("当前暂不处理 STYLE/REGION 字幕结构")
        timing = 0 if "-->" in lines[0] else 1
        if timing >= len(lines):
            raise SubtitleUnsupported("字幕缺少时间行")
        match = pattern.fullmatch(lines[timing])
        if match is None or timing + 1 >= len(lines):
            raise SubtitleUnsupported("字幕时间或正文不合法")
        values = match.groups()
        def seconds(offset):
            hour, minute, second, millisecond = values[offset:offset + 4]
            if int(minute) >= 60 or int(second) >= 60:
                raise SubtitleUnsupported("字幕时间字段超出范围")
            return int(hour or 0) * 3600 + int(minute) * 60 + int(second) + int(millisecond) / 1000
        if seconds(4) <= seconds(0):
            raise SubtitleUnsupported("字幕结束时间必须晚于开始时间")
        cues += 1
    if not cues:
        raise SubtitleUnsupported("字幕没有可验证的正文提示")
    return normalized + "\n"


def commit_subtitle(validated_text, output_path, control):
    control.phase("committing")
    with _atomic_output(output_path, control) as staged:
        with open(staged, "w", encoding="utf-8", newline="\n") as target:
            target.write(validated_text)
            target.flush()
            os.fsync(target.fileno())
    return output_path