import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import url2pathname

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


class TaskCancelled(RuntimeError):
    """用户主动取消。"""


class AcquisitionStopped(RuntimeError):
    """同批资源失败后停止其他资源，不改变原始失败。"""


class InputRejected(ValueError):
    """输入或当前支持范围不满足明确契约。"""


class AcquisitionError(RuntimeError):
    """资源访问或缓存持久化失败。"""


class CacheConflict(AcquisitionError):
    """任务缓存正在被其他执行实例使用。"""


class TransferInterrupted(RuntimeError):
    """可以重新获取整个资源的传输不完整。"""


class DecryptionError(RuntimeError):
    """标准 AES-128 密钥、密文或填充不满足解密契约。"""


@dataclass(frozen=True)
class KeyDeclaration:
    uri: str
    scope: str


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    byte_range: object = None
    key: object = None
    iv: bytes = b""

    def __post_init__(self):
        if not isinstance(self.uri, str) or not self.uri:
            raise InputRejected("资源地址必须是非空文本")
        if self.byte_range is not None:
            if (
                not isinstance(self.byte_range, tuple)
                or len(self.byte_range) != 2
                or any(type(value) is not int for value in self.byte_range)
                or self.byte_range[0] < 0 or self.byte_range[1] <= 0
            ):
                raise InputRejected("资源范围必须是非负偏移和正长度")
        if self.key is None:
            if self.iv != b"":
                raise InputRejected("未加密资源不能带解密 IV")
        elif (
            not isinstance(self.key, KeyDeclaration)
            or not isinstance(self.key.uri, str) or not self.key.uri
            or not isinstance(self.key.scope, str) or not self.key.scope
            or not isinstance(self.iv, bytes) or len(self.iv) != 16
        ):
            raise InputRejected("资源密钥声明或 IV 不合法")

    def description(self):
        return (
            self.uri, self.byte_range,
            (self.key.uri, self.key.scope) if self.key else None,
            self.iv.hex(),
        )


@dataclass(frozen=True)
class RequestSettings:
    headers: tuple
    proxy: str
    timeout: float
    retries: int
    user_agent: str
    threads: int
    rate_limit_kbps: int
    resume: bool
    pool_connections: int = 1
    pool_maxsize: int = 1
    backoff_base: float = 1.0
    backoff_max: float = 8.0
    backoff_factor: float = 2.0
    chunk_size: int = 65536
    success_delay: float = 0.0

    def __post_init__(self):
        integers = (
            (self.retries, 0), (self.threads, 1), (self.rate_limit_kbps, 0),
            (self.pool_connections, 1), (self.pool_maxsize, 1),
            (self.chunk_size, 1),
        )
        if any(type(value) is not int or value < minimum for value, minimum in integers):
            raise InputRejected("请求整数参数不合法")
        floats = (
            (self.timeout, 0, False), (self.backoff_base, 0, True),
            (self.backoff_max, 0, True), (self.backoff_factor, 1, True),
            (self.success_delay, 0, True),
        )
        for value, minimum, inclusive in floats:
            if (
                not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)
                or (value < minimum if inclusive else value <= minimum)
            ):
                raise InputRejected("请求时间或退避参数不合法")
        if type(self.resume) is not bool:
            raise InputRejected("续传设置必须是布尔值")
        if not isinstance(self.headers, tuple):
            raise InputRejected("请求头必须是冻结的元组")
        if not isinstance(self.proxy, str) or not isinstance(self.user_agent, str):
            raise InputRejected("代理和 User-Agent 必须是文本")
        names = set()
        for declaration in self.headers:
            if not isinstance(declaration, tuple) or len(declaration) != 2:
                raise InputRejected("请求头结构不合法")
            name, value = declaration
            if not isinstance(name, str) or not isinstance(value, str):
                raise InputRejected("请求头必须是文本")
            if (
                re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name) is None
                or name.lower() in names
            ):
                raise InputRejected("请求头名称或重复声明不合法")
            if name.lower() in (
                "host", "content-length", "range", "if-range", "if-none-match"
            ):
                raise InputRejected("Host、长度、Range 和缓存条件头由程序管理")
            _validate_header_value(value, "请求头")
            names.add(name.lower())
        _validate_header_value(self.user_agent, "User-Agent")

    def identity(self):
        return hashlib.sha256(json.dumps(
            [self.headers, self.proxy, self.user_agent],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()


def _validate_header_value(value, label):
    # requests/http.client 的文本请求头采用 Latin-1 编码，不自动编码 Unicode。
    # 用户需要自行提供站点规定的百分号编码或其他实际传输格式。
    if (
        any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value.startswith(" ")
    ):
        raise InputRejected(f"{label}包含控制字符或起始空白")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as error:
        raise InputRejected(
            f"{label}包含不能直接作为 HTTP 请求头传输的字符；"
            "请使用源站要求的编码，不要直接填写未编码的 Unicode 文本"
        ) from error

def local_path(uri):
    parts = urlsplit(uri)
    if parts.scheme.lower() != "file" or parts.query or parts.fragment:
        raise InputRejected("本地文件 URI 不合法")
    if parts.netloc and parts.netloc.lower() != "localhost":
        if os.name != "nt":
            raise InputRejected("当前平台不支持 UNC 文件 URI")
        return url2pathname("//" + parts.netloc + parts.path)
    # url2pathname 自身负责百分号解码，不能先 unquote 再重复解码。
    return url2pathname(parts.path)


def source_uri(source):
    if os.path.isfile(source):
        return Path(source).resolve().as_uri()
    try:
        parts = urlsplit(source)
    except ValueError as error:
        raise InputRejected("输入地址不合法") from error
    if parts.scheme.lower() in ("http", "https", "file"):
        return source
    raise InputRejected("输入必须是存在的文件或 HTTP/HTTPS/file 地址")


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, document, notice):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n",
            dir=os.path.dirname(path), prefix="._manifest_", suffix=".tmp",
            delete=False,
        ) as target:
            temporary = target.name
            json.dump(document, target, ensure_ascii=False, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                notice("缓存清单临时文件清理失败，请检查磁盘权限")


def _hex_digest(value):
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class TaskWorkspace:
    """清单是缓存授权来源；不扫描目录推断资源完成或删除权限。"""

    def __init__(self, parent, identity, notice):
        if not _hex_digest(identity):
            raise InputRejected("任务缓存身份不合法")
        self.directory = os.path.join(parent, ".steamdesk_task_" + identity)
        self.manifest_path = os.path.join(self.directory, "manifest.json")
        self.lock_path = os.path.join(self.directory, "execution.lock")
        self._notice = notice
        self._lock = threading.Lock()
        self._owns_lock = False
        self._manifest = {"version": 1, "identity": identity, "resources": {}}

    def __enter__(self):
        opened = False
        try:
            if os.path.islink(self.directory):
                raise InputRejected("任务缓存目录不能是符号链接")
            os.makedirs(self.directory, exist_ok=True)
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError as error:
                raise CacheConflict(
                    "同任务缓存存在执行锁。若此前进程异常退出，请确认无其他"
                    "活跃实例后，手动删除该缓存的 execution.lock。"
                ) from error
            self._owns_lock = True
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            if os.path.islink(self.manifest_path):
                raise InputRejected("缓存清单不能是符号链接")
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as source:
                    manifest = json.load(source)
            except FileNotFoundError:
                manifest = self._manifest
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"version", "identity", "resources"}
                or type(manifest["version"]) is not int
                or manifest["version"] != 1
                or manifest["identity"] != self._manifest["identity"]
                or not isinstance(manifest["resources"], dict)
            ):
                raise InputRejected("缓存清单版本、格式或任务身份不匹配")
            for resource_id, entry in manifest["resources"].items():
                if (
                    not _hex_digest(resource_id) or not isinstance(entry, dict)
                    or set(entry) != {"size", "sha256", "etag"}
                    or type(entry["size"]) is not int or entry["size"] <= 0
                    or not _hex_digest(entry["sha256"])
                    or not isinstance(entry["etag"], str)
                    or "\r" in entry["etag"] or "\n" in entry["etag"]
                ):
                    raise InputRejected("缓存资源记录不合法")
            self._manifest = manifest
            opened = True
            return self
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AcquisitionError("任务缓存无法打开或读取") from error
        finally:
            if not opened:
                self._release()

    def _release(self):
        if self._owns_lock:
            self._owns_lock = False
            try:
                os.remove(self.lock_path)
            except FileNotFoundError:
                pass
            except OSError:
                self._notice("任务执行锁清理失败，请检查缓存目录")

    def __exit__(self, exception_type, exception, traceback):
        self._release()

    def resource_path(self, resource_id):
        if not _hex_digest(resource_id):
            raise InputRejected("资源身份不合法")
        return os.path.join(self.directory, resource_id + ".media")

    def entry(self, resource_id):
        with self._lock:
            entry = self._manifest["resources"].get(resource_id)
            return dict(entry) if entry is not None else None

    def commit(self, resource_id, size, sha256, etag):
        with self._lock:
            resources = dict(self._manifest["resources"])
            resources[resource_id] = {"size": size, "sha256": sha256, "etag": etag}
            document = dict(self._manifest, resources=resources)
            atomic_json(self.manifest_path, document, self._notice)
            self._manifest = document

    def cleanup(self, paths):
        selected = {os.path.normcase(os.path.abspath(path)) for path in paths}
        with self._lock:
            remaining = dict(self._manifest["resources"])
            for resource_id in self._manifest["resources"]:
                path = self.resource_path(resource_id)
                if os.path.normcase(os.path.abspath(path)) not in selected:
                    continue
                try:
                    metadata = os.lstat(path)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
                        self._notice("缓存已变为链接或非普通文件，未清理")
                        continue
                    os.remove(path)
                    remaining.pop(resource_id)
                except FileNotFoundError:
                    remaining.pop(resource_id)
                except OSError:
                    self._notice("授权缓存清理失败，文件已保留")
            try:
                document = dict(self._manifest, resources=remaining)
                atomic_json(self.manifest_path, document, self._notice)
                self._manifest = document
            except OSError:
                self._notice("清理后的缓存清单提交失败，下次执行将重新复核")


class ResourceClient:
    MAX_TEXT_BYTES = 8 * 1024 * 1024
    TRANSIENT_STATUS = frozenset((408, 429, 500, 502, 503, 504))

    def __init__(self, settings, control):
        self.settings = settings
        self.control = control
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_read_at = 0.0
        self._keys = {}

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.settings.pool_connections,
                pool_maxsize=self.settings.pool_maxsize, max_retries=0,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self):
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()
        self._keys.clear()

    def _parts(self, uri):
        try:
            parts = urlsplit(uri)
            port = parts.port
        except ValueError as error:
            raise InputRejected("HTTP 资源地址不合法") from error
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            raise InputRejected("资源必须使用有效 HTTP/HTTPS 地址")
        if parts.username or parts.password:
            raise InputRejected("URL 内嵌凭据不受支持，请使用显式请求头")
        return parts, (
            parts.scheme.lower(), parts.hostname.lower(),
            port or (443 if parts.scheme.lower() == "https" else 80),
        )

    def _open(self, uri, conditional, byte_range):
        current = uri
        headers = {
            name: value for name, value in self.settings.headers
            if name.lower() not in ("user-agent", "accept-encoding")
        }
        headers["User-Agent"] = self.settings.user_agent
        headers["Accept-Encoding"] = "identity"
        if conditional:
            headers["If-None-Match"] = conditional
        if byte_range is not None:
            offset, length = byte_range
            headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        proxy = self.settings.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        for _ in range(11):
            self.control.checkpoint()
            parts, origin = self._parts(current)
            session = self._session()
            session.cookies.clear()
            response = session.get(
                current, headers=headers, proxies=proxies,
                timeout=self.settings.timeout, stream=True,
                allow_redirects=False,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise AcquisitionError("重定向响应缺少 Location")
            following = urljoin(current, location)
            next_parts, next_origin = self._parts(following)
            if parts.scheme.lower() == "https" and next_parts.scheme.lower() == "http":
                raise AcquisitionError("拒绝 HTTPS 到 HTTP 降级重定向")
            if origin != next_origin:
                headers = {
                    "User-Agent": self.settings.user_agent,
                    "Accept-Encoding": "identity",
                }
                if byte_range is not None:
                    offset, length = byte_range
                    headers["Range"] = f"bytes={offset}-{offset + length - 1}"
            current = following
        raise AcquisitionError("资源重定向超过限制")

    def _transfer(self, uri, consume, conditional=None, byte_range=None):
        last_error = None
        for attempt in range(self.settings.retries + 1):
            self.control.checkpoint()
            try:
                with self._open(uri, conditional, byte_range) as response:
                    if response.status_code == 304:
                        sent = response.request.headers.get("If-None-Match")
                        if not conditional or sent != conditional:
                            raise AcquisitionError("服务器返回无有效条件请求的 304")
                        return consume(response)
                    if response.status_code in self.TRANSIENT_STATUS:
                        last_error = TransferInterrupted(
                            f"暂时性 HTTP 故障：{response.status_code}"
                        )
                    elif byte_range is not None and response.status_code == 200:
                        raise AcquisitionError("服务器忽略 Range，未把整文件当作片段")
                    elif response.status_code != (206 if byte_range is not None else 200):
                        raise AcquisitionError(
                            f"资源请求被拒绝，HTTP {response.status_code}"
                        )
                    else:
                        if byte_range is not None:
                            self._validate_range(response, byte_range)
                        return consume(response)
            except requests.exceptions.SSLError as error:
                raise AcquisitionError("TLS 校验失败；未记录鉴权地址") from error
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                TransferInterrupted,
            ) as error:
                last_error = error
            except (
                requests.exceptions.InvalidURL,
                requests.exceptions.InvalidSchema,
                requests.exceptions.MissingSchema,
            ) as error:
                raise InputRejected("HTTP 地址或协议不合法") from error
            except requests.exceptions.ContentDecodingError as error:
                raise AcquisitionError("响应内容编码无法处理") from error
            if attempt < self.settings.retries:
                self.control.notice(f"传输暂时失败，正在进行第 {attempt + 1} 次重试")
                self.control.wait_delay(min(
                    self.settings.backoff_base
                    * self.settings.backoff_factor ** attempt,
                    self.settings.backoff_max,
                ))
        raise AcquisitionError("资源传输在重试后仍失败") from last_error

    def _validate_range(self, response, byte_range):
        match = re.fullmatch(
            r"bytes ([0-9]+)-([0-9]+)/([0-9]+)",
            response.headers.get("Content-Range", ""),
        )
        if match is None:
            raise AcquisitionError("范围响应缺少合法 Content-Range")
        start, end, total = map(int, match.groups())
        offset, length = byte_range
        if start != offset or end != offset + length - 1 or total <= end:
            raise AcquisitionError("服务器返回范围与计划不一致")

    def _chunks(self, response):
        encoding = response.headers.get("Content-Encoding", "identity").lower()
        if encoding not in ("", "identity"):
            raise AcquisitionError("服务器未遵守 identity 编码请求")
        for chunk in response.iter_content(chunk_size=self.settings.chunk_size):
            self.control.checkpoint()
            if not chunk:
                continue
            if self.settings.rate_limit_kbps:
                with self._rate_lock:
                    now = time.monotonic()
                    self._next_read_at = max(now, self._next_read_at) + (
                        len(chunk) / (self.settings.rate_limit_kbps * 1024)
                    )
                    delay = self._next_read_at - now
                self.control.wait_delay(delay)
            self.control.record_bytes(len(chunk))
            yield chunk

    def _validate_length(self, response, size):
        declared = response.headers.get("Content-Length")
        if declared is None:
            return
        try:
            expected = int(declared)
        except ValueError as error:
            raise AcquisitionError("Content-Length 不合法") from error
        if expected < 0:
            raise AcquisitionError("Content-Length 不能为负数")
        if size != expected:
            raise TransferInterrupted("响应长度不完整")

    def read_bytes(self, uri, maximum):
        self.control.checkpoint()
        if urlsplit(uri).scheme.lower() == "file":
            try:
                with open(local_path(uri), "rb") as source:
                    content = source.read(maximum + 1)
            except OSError as error:
                raise AcquisitionError("本地资源无法读取") from error
            final_uri = uri
        else:
            def consume(response):
                blocks = []
                size = 0
                for chunk in self._chunks(response):
                    size += len(chunk)
                    if size > maximum:
                        raise InputRejected("文本或密钥资源超过允许大小")
                    blocks.append(chunk)
                self._validate_length(response, size)
                return b"".join(blocks), response.url
            content, final_uri = self._transfer(uri, consume)
        self.control.checkpoint()
        if len(content) > maximum:
            raise InputRejected("文本或密钥资源超过允许大小")
        return content, final_uri

    def read_text(self, uri):
        content, final_uri = self.read_bytes(uri, self.MAX_TEXT_BYTES)
        try:
            return content.decode("utf-8-sig"), final_uri
        except UnicodeDecodeError as error:
            raise InputRejected("列表和字幕必须是有效 UTF-8") from error

    def prepare_keys(self, resources):
        # 准备线程在并发派发前冻结密钥；声明作用域不同则分别获取。
        declarations = tuple(dict.fromkeys(
            resource.key for resource in resources if resource.key is not None
        ))
        for declaration in declarations:
            self.control.checkpoint()
            if declaration in self._keys:
                continue
            try:
                key, _ = self.read_bytes(declaration.uri, 16)
            except InputRejected as error:
                raise DecryptionError("AES-128 密钥必须直接返回 16 字节") from error
            if len(key) != 16:
                raise DecryptionError("AES-128 密钥必须直接返回 16 字节")
            self._keys[declaration] = key

    def _decrypt(self, encrypted_path, target, key, iv):
        cipher = AES.new(key, AES.MODE_CBC, iv)
        pending = b""
        digest = hashlib.sha256()
        size = 0

        def write(block):
            nonlocal size
            target.write(block)
            digest.update(block)
            size += len(block)

        with open(encrypted_path, "rb") as source:
            while True:
                self.control.checkpoint()
                block = source.read(1024 * 1024)
                if not block:
                    break
                pending += block
                available = ((len(pending) - 16) // 16) * 16
                if available > 0:
                    write(cipher.decrypt(pending[:available]))
                    pending = pending[available:]
        if not pending or len(pending) % 16:
            raise DecryptionError("AES-128 密文为空或不是完整的 16 字节块")
        try:
            final = unpad(cipher.decrypt(pending), 16)
        except ValueError as error:
            raise DecryptionError("AES-128 解密填充无效，请检查密钥、IV 和资源") from error
        write(final)
        if size <= 0:
            raise DecryptionError("AES-128 解密后没有媒体内容")
        return size, digest.hexdigest()

    def acquire(self, resource, workspace):
        if not isinstance(resource, ResourceSpec):
            raise TypeError("获取器需要 ResourceSpec")
        self.control.checkpoint()
        key = None
        if resource.key is not None:
            if resource.key not in self._keys:
                raise InputRejected("资源密钥尚未在准备阶段冻结")
            key = self._keys[resource.key]
        is_local = urlsplit(resource.uri).scheme.lower() == "file"
        if is_local and resource.byte_range is None and key is None:
            path = local_path(resource.uri)
            try:
                metadata = os.stat(path)
            except OSError as error:
                raise AcquisitionError("本地媒体资源无法访问") from error
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                raise InputRejected("本地资源必须是非空普通文件")
            return path

        resource_id = hashlib.sha256(json.dumps(
            (
                "resource-v2", self.settings.identity(), resource.description(),
                hashlib.sha256(key).hexdigest() if key is not None else None,
            ),
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        path = workspace.resource_path(resource_id)
        entry = workspace.entry(resource_id) if self.settings.resume and not is_local else None
        conditional = None
        if entry:
            try:
                metadata = os.lstat(path)
                valid = (
                    stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
                    and metadata.st_size == entry["size"]
                    and digest_file(path) == entry["sha256"]
                )
            except FileNotFoundError:
                valid = False
            except OSError as error:
                raise AcquisitionError("缓存内容复核失败") from error
            etag = entry["etag"]
            if valid and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', etag):
                conditional = etag

        def submit(chunks, response=None, local_length=None):
            pending_paths = []
            try:
                digest = hashlib.sha256()
                transferred = 0
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=workspace.directory,
                    prefix="._resource_", suffix=".part", delete=False,
                ) as target:
                    raw_path = target.name
                    pending_paths.append(raw_path)
                    for chunk in chunks:
                        self.control.checkpoint()
                        target.write(chunk)
                        digest.update(chunk)
                        transferred += len(chunk)
                    # 在解密之前验证传输，截断响应仍是传输错误。
                    if response is not None:
                        self._validate_length(response, transferred)
                    expected = (
                        resource.byte_range[1] if resource.byte_range is not None
                        else local_length
                    )
                    if expected is not None and transferred != expected:
                        if response is not None:
                            raise TransferInterrupted("范围资源传输长度不完整")
                        raise AcquisitionError("本地资源长度改变或范围读取不完整")
                    if transferred <= 0:
                        raise AcquisitionError("资源没有非空媒体内容")
                    target.flush()
                    os.fsync(target.fileno())

                staged = raw_path
                size, sha256 = transferred, digest.hexdigest()
                if key is not None:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=workspace.directory,
                        prefix="._decrypted_", suffix=".part", delete=False,
                    ) as target:
                        staged = target.name
                        pending_paths.append(staged)
                        size, sha256 = self._decrypt(raw_path, target, key, resource.iv)
                        target.flush()
                        os.fsync(target.fileno())
                self.control.checkpoint()
                os.replace(staged, path)
                pending_paths.remove(staged)
                workspace.commit(
                    resource_id, size, sha256,
                    response.headers.get("ETag", "") if response is not None else "",
                )
                return path
            except OSError as error:
                raise AcquisitionError("资源文件或清单提交失败") from error
            finally:
                for temporary in pending_paths:
                    try:
                        os.remove(temporary)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        self.control.notice("未提交资源临时文件清理失败")

        if is_local:
            try:
                with open(local_path(resource.uri), "rb") as source:
                    metadata = os.fstat(source.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                        raise InputRejected("本地资源必须是非空普通文件")
                    offset, length = (
                        resource.byte_range if resource.byte_range is not None
                        else (0, metadata.st_size)
                    )
                    if offset + length > metadata.st_size:
                        raise InputRejected("本地 BYTERANGE 超出文件边界")
                    source.seek(offset)

                    def chunks():
                        remaining = length
                        while remaining:
                            self.control.checkpoint()
                            block = source.read(min(remaining, self.settings.chunk_size))
                            if not block:
                                break
                            remaining -= len(block)
                            yield block
                    result = submit(chunks(), local_length=length)
            except OSError as error:
                raise AcquisitionError("本地资源无法读取") from error
        else:
            def consume(response):
                if response.status_code == 304:
                    self.control.notice("源已确认缓存版本，复用完整性已校验资源")
                    return path
                return submit(self._chunks(response), response=response)
            result = self._transfer(
                resource.uri, consume, conditional, resource.byte_range
            )
        if self.settings.success_delay:
            self.control.wait_delay(self.settings.success_delay)
        return result