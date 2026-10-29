import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from downloader import InputRejected, KeyDeclaration, ResourceSpec, source_uri


@dataclass(frozen=True)
class Segment:
    resource: ResourceSpec
    duration: float
    order: int
    sequence: int
    initialization: object = None


@dataclass(frozen=True)
class MediaPlaylist:
    uri: str
    segments: tuple
    target_duration: float

    @property
    def duration(self):
        return sum(segment.duration for segment in self.segments)


@dataclass(frozen=True)
class StreamVariant:
    uri: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""
    frame_rate: str = ""
    audio_group: str = ""
    subtitle_group: str = ""
    group_id: str = ""
    name: str = ""
    language: str = ""
    default: bool = False


@dataclass(frozen=True)
class InputSnapshot:
    uri: str
    kind: str
    media: object = None
    variants: tuple = ()
    audio: tuple = ()
    subtitles: tuple = ()
    cached_playlists: tuple = ()
    directory_files: tuple = ()
    source: str = ""


def resolve_uri(reference, base):
    try:
        uri = urljoin(base, reference)
        parts = urlsplit(uri)
    except ValueError as error:
        raise InputRejected("资源 URI 不合法") from error
    if parts.scheme.lower() not in ("http", "https", "file"):
        raise InputRejected("资源协议不受支持")
    return uri


def attributes(line):
    if ":" not in line:
        raise InputRejected("列表属性缺少分隔符")
    text = line.split(":", 1)[1]
    result = {}
    position = 0
    pattern = re.compile(r'([A-Z0-9-]+)=(?:"([^"\r\n]*)"|([^,"\r\n]+))')
    while position < len(text):
        match = pattern.match(text, position)
        if match is None:
            raise InputRejected("列表属性格式不合法")
        name = match.group(1)
        if name in result:
            raise InputRejected("列表属性重复")
        result[name] = match.group(2) if match.group(2) is not None else match.group(3)
        position = match.end()
        if position < len(text):
            if text[position] != "," or position + 1 == len(text):
                raise InputRejected("列表属性分隔格式不合法")
            position += 1
    if not result:
        raise InputRejected("列表属性为空")
    return result


def natural_order(path):
    name = os.path.basename(path).lower()
    numbers = tuple(int(value) for value in re.findall(r"\d+", name))
    return (0 if name.startswith("_init") else 1, 0 if numbers else 1, numbers, name)


def _lines(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise InputRejected("M3U8 必须以 #EXTM3U 开头")
    return lines


def _positive_number(text, label):
    try:
        value = float(text)
    except ValueError as error:
        raise InputRejected(f"{label}不合法") from error
    if not math.isfinite(value) or value <= 0:
        raise InputRejected(f"{label}必须为有限正数")
    return value


def _byte_range(text, resource_uri, previous):
    match = re.fullmatch(r"([0-9]+)(?:@([0-9]+))?", text)
    if match is None or int(match.group(1)) <= 0:
        raise InputRejected("BYTERANGE 必须是正长度及可选非负偏移")
    length = int(match.group(1))
    if match.group(2) is not None:
        offset = int(match.group(2))
    else:
        if previous is None or previous[0] != resource_uri:
            raise InputRejected("隐式 BYTERANGE 缺少紧邻的同资源范围")
        offset = previous[1] + previous[2]
    return offset, length


def parse_media(text, uri):
    lines = _lines(text)
    accepted = {
        "#EXTM3U", "#EXTINF", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION",
        "#EXT-X-MEDIA-SEQUENCE", "#EXT-X-ENDLIST", "#EXT-X-PLAYLIST-TYPE",
        "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-KEY",
        "#EXT-X-MAP", "#EXT-X-BYTERANGE",
    }
    segments = []
    pending = None
    pending_range = None
    target = 0.0
    ended = False
    sequence = 0
    sequence_seen = False
    key = None
    explicit_iv = None
    declaration_index = 0
    initialization = None
    previous_segment_range = None

    for line in lines[1:]:
        if line.startswith("#EXT"):
            tag = line.split(":", 1)[0]
            if tag not in accepted:
                raise InputRejected(f"当前不支持 HLS 标签：{tag}")
            if ended:
                raise InputRejected("结束标签之后不能继续声明 HLS 内容")
        if line.startswith("#EXT-X-KEY:"):
            attrs = attributes(line)
            method = attrs.get("METHOD")
            if method == "NONE":
                if set(attrs) != {"METHOD"}:
                    raise InputRejected("METHOD=NONE 不能包含密钥参数")
                key, explicit_iv = None, None
            elif method == "AES-128":
                if (
                    not attrs.get("URI")
                    or attrs.get("KEYFORMAT", "identity") != "identity"
                    or attrs.get("KEYFORMATVERSIONS", "1") != "1"
                    or set(attrs) - {"METHOD", "URI", "IV", "KEYFORMAT", "KEYFORMATVERSIONS"}
                ):
                    raise InputRejected("当前仅支持标准 identity AES-128 密钥声明")
                explicit_iv = None
                if "IV" in attrs:
                    if re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", attrs["IV"]) is None:
                        raise InputRejected("AES IV 必须是不超过 128 位的十六进制整数")
                    explicit_iv = int(attrs["IV"], 16).to_bytes(16, "big")
                declaration_index += 1
                scope = hashlib.sha256(
                    (uri + "\n" + str(declaration_index)).encode("utf-8")
                ).hexdigest()
                key = KeyDeclaration(resolve_uri(attrs["URI"], uri), scope)
            else:
                raise InputRejected("当前不支持该加密方法或 DRM")
        elif line.startswith("#EXT-X-MAP:"):
            attrs = attributes(line)
            if not attrs.get("URI") or set(attrs) - {"URI", "BYTERANGE"}:
                raise InputRejected("MAP 声明不合法")
            map_uri = resolve_uri(attrs["URI"], uri)
            map_range = None
            if "BYTERANGE" in attrs:
                if "@" not in attrs["BYTERANGE"]:
                    raise InputRejected("MAP BYTERANGE 必须显式声明偏移")
                map_range = _byte_range(attrs["BYTERANGE"], map_uri, None)
            if key is not None and explicit_iv is None:
                raise InputRejected("AES-128 加密的 MAP 必须提供显式 IV")
            initialization = ResourceSpec(
                map_uri, map_range, key, explicit_iv if key else b""
            )
        elif line.startswith("#EXT-X-BYTERANGE:"):
            if pending_range is not None:
                raise InputRejected("同一片段重复声明 BYTERANGE")
            pending_range = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            value = line.split(":", 1)[1]
            if (
                sequence_seen or segments or pending is not None
                or re.fullmatch(r"[0-9]+", value) is None
            ):
                raise InputRejected("MEDIA-SEQUENCE 必须在片段之前唯一声明")
            sequence = int(value)
            if sequence >= 2 ** 128:
                raise InputRejected("媒体序列号超出 AES IV 范围")
            sequence_seen = True
        elif line == "#EXT-X-ENDLIST":
            if pending is not None or pending_range is not None:
                raise InputRejected("结束标签之前仍有未完成片段")
            ended = True
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = _positive_number(line.split(":", 1)[1], "TARGETDURATION")
        elif line.startswith("#EXTINF:"):
            if pending is not None:
                raise InputRejected("EXTINF 缺少资源")
            pending = _positive_number(
                line.split(":", 1)[1].split(",", 1)[0], "片段时长"
            )
        elif not line.startswith("#"):
            if pending is None or ended:
                raise InputRejected("资源行缺少时长或出现在结束标签之后")
            resource_uri = resolve_uri(line, uri)
            byte_range = (
                _byte_range(pending_range, resource_uri, previous_segment_range)
                if pending_range is not None else None
            )
            media_sequence = sequence + len(segments)
            if media_sequence >= 2 ** 128:
                raise InputRejected("媒体序列号超出 AES IV 范围")
            iv = (
                explicit_iv if explicit_iv is not None
                else media_sequence.to_bytes(16, "big")
            ) if key else b""
            segments.append(Segment(
                ResourceSpec(resource_uri, byte_range, key, iv),
                pending, len(segments), media_sequence, initialization,
            ))
            previous_segment_range = (
                (resource_uri, byte_range[0], byte_range[1])
                if byte_range is not None else None
            )
            pending, pending_range = None, None

    if pending is not None or pending_range is not None or not segments:
        raise InputRejected("媒体列表为空或最后一个片段缺少资源")
    if not ended:
        raise InputRejected("列表尚未结束，当前版本未启用持续录制")
    return MediaPlaylist(uri, tuple(segments), target)


def parse_master(text, uri):
    lines = _lines(text)
    accepted = {
        "#EXTM3U", "#EXT-X-VERSION", "#EXT-X-INDEPENDENT-SEGMENTS",
        "#EXT-X-MEDIA", "#EXT-X-STREAM-INF",
    }
    variants, audio, subtitles = [], [], []
    pending = None
    for line in lines[1:]:
        if line.startswith("#EXT") and line.split(":", 1)[0] not in accepted:
            raise InputRejected("主列表包含当前尚未支持的 HLS 标签")
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = attributes(line)
            rendition = StreamVariant(
                uri=resolve_uri(attrs["URI"], uri) if attrs.get("URI") else "",
                group_id=attrs.get("GROUP-ID", ""),
                name=attrs.get("NAME", ""), language=attrs.get("LANGUAGE", ""),
                default=attrs.get("DEFAULT", "").upper() == "YES",
            )
            if attrs.get("TYPE") == "AUDIO":
                audio.append(rendition)
            elif attrs.get("TYPE") == "SUBTITLES":
                subtitles.append(rendition)
        elif line.startswith("#EXT-X-STREAM-INF:"):
            if pending is not None:
                raise InputRejected("主列表声明缺少资源")
            pending = attributes(line)
        elif not line.startswith("#"):
            if pending is None:
                raise InputRejected("主列表出现无关联 URI")
            try:
                bandwidth = int(pending.get("BANDWIDTH", "0"))
            except ValueError as error:
                raise InputRejected("主列表带宽不合法") from error
            if bandwidth < 0:
                raise InputRejected("主列表带宽不能为负数")
            variants.append(StreamVariant(
                uri=resolve_uri(line, uri), bandwidth=bandwidth,
                resolution=pending.get("RESOLUTION", ""),
                codecs=pending.get("CODECS", ""),
                frame_rate=pending.get("FRAME-RATE", ""),
                audio_group=pending.get("AUDIO", ""),
                subtitle_group=pending.get("SUBTITLES", ""),
            ))
            pending = None
    if pending is not None or not variants:
        raise InputRejected("主列表没有有效主媒体")
    return (
        tuple(sorted(variants, key=lambda variant: variant.bandwidth, reverse=True)),
        tuple(audio), tuple(subtitles),
    )


def load_snapshot(source, client):
    if os.path.isdir(source):
        files = []
        with os.scandir(source) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                extension = os.path.splitext(entry.name)[1].lower()
                if extension == ".m4s" or entry.name.lower().startswith("_init"):
                    raise InputRejected("目录存在初始化依赖，当前不能猜测其关联")
                if extension in (".ts", ".mp4", ".mkv", ".aac", ".m4a"):
                    files.append(str(Path(entry.path).resolve()))
        if not files:
            raise InputRejected("目录没有当前支持的独立媒体文件")
        return InputSnapshot(
            str(Path(source).resolve()), "directory",
            directory_files=tuple(sorted(files, key=natural_order)),
            source=source,
        )
    uri = source_uri(source)
    text, final_uri = client.read_text(uri)
    lines = _lines(text)
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        variants, audio, subtitles = parse_master(text, final_uri)
        # 预览只确定主列表能力，不以第一条子列表作为全部选择的门槛。
        # 实际选择的子列表由正式任务准备阶段获取一次并冻结。
        return InputSnapshot(
            final_uri, "master", variants=variants,
            audio=audio, subtitles=subtitles, source=source,
        )
    return InputSnapshot(
        final_uri, "media", media=parse_media(text, final_uri), source=source,
    )