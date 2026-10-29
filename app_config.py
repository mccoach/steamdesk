import json
import os
import sys
import tempfile
from copy import deepcopy


APP_DIR = os.path.dirname(os.path.abspath(__file__))

def get_app_dir() -> str:
    """
    获取程序入口所在目录。

    打包后：exe 所在目录。
    未打包：启动入口脚本所在目录，通常是 main.py 所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    return os.path.dirname(os.path.abspath(sys.argv[0]))


DEFAULT_CONFIG_PATH = os.path.join(get_app_dir(), "SteamDesk_config.json")

# 以下设置项的注释说明很重要，必须严格完整保留，且当有新增项时，也必须按照同等标准给出注释说明。
DEFAULT_CONFIG = {
    # 配置文件路径。
    # 含义：当前用户偏好配置文件保存位置。
    # 用途：程序启动时读取该 JSON；用户修改设置后写回该 JSON。
    # 推荐：默认即可，不建议频繁修改；如需便携配置，可在系统设置中改到指定位置。
    "config_path": DEFAULT_CONFIG_PATH,

    # 上一次输入源。
    # 含义：上次输入的 m3u8 链接或本地 m3u8 文件路径。
    # 用途：下次打开程序时自动恢复输入框内容。
    # 推荐：无需手动改，由 UI 自动保存。
    "last_input": "",

    # 默认保存目录。
    # 含义：最终视频文件、字幕文件、临时片段目录的默认输出根目录。
    # 用途：点击开始执行时，输出文件会保存到该目录。
    # 推荐：使用剩余空间较大的磁盘目录，例如 E:\SteamDesk\output。
    "save_dir": r"E:\SteamDesk\output",

    # 默认输出文件名。
    # 含义：最终合并文件名，不包含扩展名。
    # 用途：和 output_format 拼接成最终文件，例如 video.mp4。
    # 推荐：不要为空；避免使用 Windows 非法字符：\ / : * ? " < > |。
    "output_name": "video",

    # 默认输出格式。
    # 含义：最终合并文件扩展名。
    # 用途：决定输出容器格式。
    # 推荐值：".mp4" 最通用；".mkv" 兼容多轨更好；".ts" 适合原始流快速合并。
    # 可选范围：".mp4" / ".mkv" / ".ts"。
    "output_format": ".mp4",

    # 下载线程数。
    # 含义：同时下载多少个媒体切片。
    # 用途：提高下载速度。
    # 推荐范围：2-8；默认 5。
    # 注意：过高可能触发服务器限流、403、连接重置，甚至速度下降。
    "threads": 5,

    # 单切片重试次数。
    # 含义：某个切片下载失败后最多重试多少次。
    # 用途：提升网络不稳定时的成功率。
    # 推荐范围：3-8；网络差可设 8-12。
    "retries": 5,

    # 网络请求超时时间，单位秒。
    # 含义：请求 m3u8、key、切片、字幕等资源时的超时阈值。
    # 用途：避免某个请求长时间卡死。
    # 推荐范围：15-60；默认 30。
    # 注意：太小容易误判失败，太大则异常请求等待时间长。
    "timeout": 15,

    # 下载限速，单位 KB/s。
    # 含义：限制整体读取速度。
    # 用途：避免占满网络带宽，或降低服务器风控概率。
    # 推荐范围：0 或 100-10240。
    # 特殊值：0 表示不限速。
    "rate_limit_kbps": 0,

    # 断点续传。
    # 含义：下载前检查临时目录中已存在且大小大于 0 的切片。
    # 用途：跳过已下载切片，继续未完成任务。
    # 推荐：True。
    # 注意：如果源站切片内容变化，旧切片可能导致合并异常，此时可关闭或删除临时目录。
    "resume": True,

    # 合并后清理临时文件。
    # 含义：下载完成并成功合并后，是否删除中间切片和合并临时文件。
    # 用途：节省磁盘空间，保持输出目录干净。
    # 推荐：普通下载建议 True；排查问题或希望保留切片续传时设 False。
    # 注意：这是“合并成功后”清理，不是下载过程中删除。
    "cleanup_temp": True,

    # 自定义请求头文本。
    # 含义：用户手动填写的 HTTP Header。
    # 用途：部分站点需要 Referer、Origin、Authorization 等才能访问。
    # 推荐格式：Referer: https://example.com; Origin: https://example.com
    # 注意：多个 Header 使用英文分号 ; 分隔。
    "headers_text": "",

    # 代理地址。
    # 含义：HTTP/HTTPS 请求使用的代理。
    # 用途：访问需要代理的资源。
    # 推荐格式：http://127.0.0.1:7890
    # 特殊值：空字符串表示不使用代理。
    "proxy": "",

    # Cookie 字符串。
    # 含义：请求时附加 Cookie。
    # 用途：下载需要登录态、会员权限或鉴权的视频。
    # 推荐：从浏览器开发者工具复制完整 Cookie。
    # 特殊值：空字符串表示不附加 Cookie。
    "cookie": "",

    # User-Agent。
    # 含义：请求时使用的浏览器标识。
    # 用途：模拟浏览器访问，降低被拒绝概率。
    # 推荐：默认即可；遇到特殊站点可换成浏览器真实 UA。
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",

    # 每个下载线程 Session 的连接池连接组数量。
    # 含义：requests HTTPAdapter pool_connections。
    # 用途：控制底层连接池。
    # 推荐范围：1-4；默认 1。
    # 注意：当前实现为每线程独立 Session，因此通常无需调大。
    "session_pool_connections": 1,

    # 每个下载线程 Session 的最大连接数。
    # 含义：requests HTTPAdapter pool_maxsize。
    # 用途：控制每个 Session 内最大连接数。
    # 推荐范围：1-4；默认 1。
    "session_pool_maxsize": 1,

    # 重试退避基础等待秒数。
    # 含义：第一次重试前的等待基数。
    # 用途：失败后不要立即狂重试，降低服务器压力。
    # 推荐范围：0.5-3；默认 1。
    "retry_backoff_base_sec": 1,

    # 重试退避最大等待秒数。
    # 含义：单次重试等待时间上限。
    # 用途：避免指数退避等待过久。
    # 推荐范围：5-20；默认 8。
    "retry_backoff_max_sec": 8,

    # 重试指数退避倍数。
    # 含义：每次重试等待时间乘以的指数因子。
    # 用途：例如 base=1、factor=2，则等待 1、2、4、8 秒。
    # 推荐范围：1.5-3；默认 2。
    "retry_backoff_factor": 2,

    # 下载流式读取块大小，单位字节。
    # 含义：requests iter_content 的 chunk_size。
    # 用途：控制每次从网络读取多少字节。
    # 推荐范围：32768-262144；默认 65536。
    # 注意：太小增加 CPU/UI 更新开销，太大则限速/取消响应略慢。
    "download_chunk_size": 65536,

    # 进度刷新间隔，单位秒。
    # 含义：下载器向 UI 上报进度的频率。
    # 用途：控制进度条、速度、用时等刷新频率。
    # 推荐范围：0.2-1.0；默认 0.2。
    "progress_interval_sec": 0.2,

    # 速度统计窗口，单位秒。
    # 含义：实时速度按最近多少秒的数据计算。
    # 用途：平滑下载速度显示。
    # 推荐范围：2-5；默认 3。
    # 注意：越小越灵敏但波动大，越大越平滑但响应慢。
    "speed_window_sec": 3.0,

    # 单切片成功后的额外等待，单位秒。
    # 含义：每个切片下载完成后主动 sleep。
    # 用途：遇到服务器限流时降低请求频率。
    # 推荐范围：0-0.2；默认 0。
    "segment_success_sleep_sec": 0,

    # UI 消息队列轮询间隔，单位毫秒。
    # 含义：主线程多久处理一次后台下载/解析消息。
    # 用途：控制 UI 响应和刷新频率。
    # 推荐范围：80-300；默认 120。
    "ui_poll_interval_ms": 120,

    # 日志窗口最大保留行数。
    # 含义：超过该行数后删除较早日志。
    # 用途：避免长时间下载时 Text 控件过大导致卡顿。
    # 推荐范围：300-2000；默认 500。
    "log_max_lines": 2000,

    # ffmpeg 路径。
    # 含义：ffmpeg 可执行文件完整路径。
    # 用途：合并 ts/m4s/mp4/aac/m4a、封装 mp4/mkv/ts。
    # 推荐：填写真实 ffmpeg.exe 路径，例如 D:\ffmpeg\bin\ffmpeg.exe。
    # 注意：程序不会无提示乱找，只有用户确认自动搜索时才搜索。
    "ffmpeg_path": r"D:\ffmpeg-8.1-essentials_build\bin\ffmpeg.exe",

    # ffprobe 超时时间，单位秒。
    # 含义：探测本地媒体片段信息时 ffprobe 的超时。
    # 用途：本地目录识别分辨率、编码、帧率等。
    # 推荐范围：3-15；默认 5。
    # 注意：ffprobe 默认从 ffmpeg 同目录推导。
    "ffprobe_timeout": 5,

    # 是否自动保存用户偏好。
    # 含义：用户修改界面设置后是否立即写入 user_prefs.json。
    # 用途：下次启动恢复设置。
    # 推荐：True。
    "prefs_auto_save": True
}


class AppConfigError(RuntimeError):
    """配置读取、格式校验或持久化失败，不代表程序缺陷。"""


class AppConfigStore:
    def __init__(self):
        self._config_path = DEFAULT_CONFIG_PATH
        self.data = deepcopy(DEFAULT_CONFIG)
        self.load()

    @property
    def config_path(self) -> str:
        return self._config_path

    def load(self):
        path = self._config_path

        try:
            with open(path, "r", encoding="utf-8") as config_file:
                raw = json.load(config_file)
        except FileNotFoundError:
            # 首次启动没有配置文件是正常情况；权限错误等不能伪装成首次启动。
            self.data = deepcopy(DEFAULT_CONFIG)
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AppConfigError(
                f"用户配置文件读取失败：\n{path}\n\n{error}"
            ) from error

        if not isinstance(raw, dict):
            raise AppConfigError(
                f"用户配置文件格式错误，根节点必须是 JSON 对象：\n{path}"
            )

        merged = deepcopy(DEFAULT_CONFIG)
        for key in DEFAULT_CONFIG:
            if key in raw:
                merged[key] = raw[key]

        # 完整读取及根结构验证结束后才发布新的内存配置。
        self.data = merged

    def save(self):
        """
        将当前内存设置提交到配置文件。

        内存设置不会因保存失败回滚。目标替换成功前，旧文件保持不变。
        同目录临时文件用于避免跨文件系统替换；不承诺断电时的目录持久性。
        """
        path = os.path.abspath(self._config_path)
        parent = os.path.dirname(path)
        temporary_path = None

        try:
            os.makedirs(parent, exist_ok=True)

            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix="._prefs_",
                suffix=".tmp",
                dir=parent,
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                json.dump(
                    self.data,
                    temporary_file,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            # Windows 下必须先关闭临时文件，再执行目标替换。
            os.replace(temporary_path, path)
            temporary_path = None

        except OSError as error:
            raise AppConfigError(
                f"用户配置保存失败，原配置未被本次写入替换：\n"
                f"{path}\n\n当前界面设置仍保留在内存中。\n\n{error}"
            ) from error

        finally:
            if temporary_path is not None:
                try:
                    os.remove(temporary_path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise AppConfigError(
                        f"配置临时文件清理失败：\n{temporary_path}\n\n"
                        f"原配置未被本次写入替换，请检查文件权限和磁盘状态。\n\n"
                        f"{error}"
                    ) from error

    def get(self, key):
        return self.data[key]

    def set(self, key, value, auto_save=True):
        self.data[key] = value
        if auto_save and self.data["prefs_auto_save"]:
            self.save()

    def update(self, values: dict, auto_save=True):
        self.data.update(values)
        if auto_save and self.data["prefs_auto_save"]:
            self.save()
