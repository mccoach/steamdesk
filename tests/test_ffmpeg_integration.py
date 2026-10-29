import functools
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from downloader import RequestSettings, ResourceClient, local_path
from merger import check_tools, probe_media
from parser import load_snapshot, parse_media
from task_engine import ExecutionRequest, TaskController


class RangeFixtureHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        byte_range = self.headers.get("Range")
        if byte_range is None:
            return super().do_GET()
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(404)
            return
        if not byte_range.startswith("bytes="):
            self.send_error(400)
            return
        try:
            start, end = map(int, byte_range[6:].split("-"))
        except ValueError:
            self.send_error(400)
            return
        total = path.stat().st_size
        if start < 0 or end < start or end >= total:
            self.send_error(416)
            return
        with path.open("rb") as source:
            source.seek(start)
            content = source.read(end - start + 1)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


class RealFFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        configured = os.environ.get("STEAMDESK_TEST_FFMPEG", "").strip()
        path = configured or shutil.which("ffmpeg")
        if not path:
            raise unittest.SkipTest(
                "真实 FFmpeg 集成未执行：设置 STEAMDESK_TEST_FFMPEG 或配置系统 PATH"
            )
        cls.ffmpeg, cls.ffprobe = check_tools(path, 10)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="HLS_中文 空格%20_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = RequestSettings((), "", 5, 0, "integration-test", 3, 0, True)
        handler = functools.partial(RangeFixtureHandler, directory=str(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tool(self, arguments, working_directory=None):
        result = subprocess.run(
            [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error"] + arguments,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=60,
            cwd=working_directory,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])

    def make_hls(self, name, fmp4=False, audio_only=False, video_only=False):
        directory = self.root / name
        directory.mkdir()
        arguments = []
        if not audio_only:
            arguments += ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25"]
        if not video_only:
            arguments += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        arguments += ["-t", "4"]
        if not audio_only:
            arguments += [
                "-c:v", "libx264", "-preset", "ultrafast",
                "-g", "25", "-sc_threshold", "0",
            ]
        if not video_only:
            arguments += ["-c:a", "aac", "-b:a", "64k"]
        arguments += ["-f", "hls", "-hls_time", "1", "-hls_list_size", "0"]
        if fmp4:
            arguments += [
                "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", "init.mp4",
                "-hls_segment_filename", "segment_%05d.m4s",
            ]
        else:
            arguments += ["-hls_segment_filename", "segment_%05d.ts"]
        playlist = directory / "index.m3u8"
        # 输出名称相对于明确的素材工作目录。
        # 目录中的字面百分号不是切片名称模板的一部分。
        arguments.append("index.m3u8")
        self.tool(arguments, working_directory=str(directory))
        self.assertTrue(
            playlist.is_file(),
            f"FFmpeg 未在指定素材目录生成列表：{playlist}",
        )
        media = parse_media(playlist.read_text(encoding="utf-8"), playlist.as_uri())
        resources = tuple(dict.fromkeys(
            tuple(segment.resource for segment in media.segments)
            + tuple(
                segment.initialization for segment in media.segments
                if segment.initialization is not None
            )
        ))
        generated_names = sorted(path.name for path in directory.iterdir())
        for resource in resources:
            path = Path(local_path(resource.uri))
            self.assertEqual(
                path.parent, directory,
                f"测试列表引用逃出素材目录：{path}",
            )
            self.assertTrue(
                path.is_file(),
                f"测试列表引用文件不存在：{path}\n"
                f"素材目录实际内容：{generated_names}",
            )
            self.assertGreater(
                path.stat().st_size, 0,
                f"测试列表引用了空文件：{path}",
            )
        return playlist

    def execute(self, source, name, video=True, audio=True, extension=".mp4"):
        events = []
        control = TaskController(events.append)
        client = ResourceClient(self.settings, control)
        try:
            snapshot = load_snapshot(source, client)
        finally:
            client.close()
        output = self.root / (name + extension)
        request = ExecutionRequest(
            snapshot, self.settings, 0, True, False,
            str(output), str(self.root / (name + ".vtt")),
            self.ffmpeg, 10, True,
        )
        control.phase("preparing")
        control._worker(control._execute, (request,))
        result = events[-1].payload
        self.assertEqual(result.status, "succeeded", result.error)
        self.assertEqual(result.committed_outputs, (str(output),))
        metadata = probe_media(str(output), self.ffprobe, 10)
        kinds = {stream.get("codec_type") for stream in metadata.streams}
        if video:
            self.assertIn("video", kinds)
        if audio:
            self.assertIn("audio", kinds)
        self.assertGreater(metadata.duration, 3)
        self.assertLess(metadata.duration, 6)
        self.assertEqual(list(self.root.glob("._merge_work_*")), [])
        return output

    def test_plain_ts_local_http_and_output_containers(self):
        playlist = self.make_hls("ts")
        for extension in (".mp4", ".mkv", ".ts"):
            self.execute(str(playlist), "local-ts", extension=extension)
        self.execute(self.base + "/ts/index.m3u8", "http-ts")

    def test_raw_aac_hls(self):
        source = self.root / "voice.aac"
        self.tool([
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "4", "-c:a", "aac", "-f", "adts", str(source),
        ])
        playlist = self.root / "voice.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\nvoice.aac\n#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        self.execute(str(playlist), "raw-aac", video=False)

    def test_aes_ts_default_iv_and_key_rotation(self):
        original = self.make_hls("encrypted-ts")
        media = parse_media(original.read_text(encoding="utf-8"), original.as_uri())
        keys = (bytes(range(16)), bytes(range(16, 32)))
        for index, key in enumerate(keys):
            (original.parent / f"key-{index}").write_bytes(key)
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:50"]
        for index, segment in enumerate(media.segments):
            key_index = index % 2
            lines.append(f'#EXT-X-KEY:METHOD=AES-128,URI="key-{key_index}"')
            plaintext = Path(local_path(segment.resource.uri)).read_bytes()
            ciphertext = AES.new(
                keys[key_index], AES.MODE_CBC, (50 + index).to_bytes(16, "big")
            ).encrypt(pad(plaintext, 16))
            name = f"cipher-{index}.bin"
            (original.parent / name).write_bytes(ciphertext)
            lines += [f"#EXTINF:{segment.duration},", name]
        lines.append("#EXT-X-ENDLIST")
        original.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.execute(self.base + "/encrypted-ts/index.m3u8", "aes-ts")
        self.assertTrue((original.parent / "key-0").exists())

    def test_encrypted_fmp4_map_and_ranges_local_http(self):
        original = self.make_hls("fmp4", fmp4=True)
        media = parse_media(original.read_text(encoding="utf-8"), original.as_uri())
        key = bytes(range(16))
        (original.parent / "key").write_bytes(key)
        initialization = Path(local_path(media.segments[0].initialization.uri)).read_bytes()
        encrypted_init = AES.new(
            key, AES.MODE_CBC, (7).to_bytes(16, "big")
        ).encrypt(pad(initialization, 16))
        blob = bytearray(encrypted_init)
        lines = [
            "#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:2",
            "#EXT-X-MEDIA-SEQUENCE:100",
            '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x7',
            f'#EXT-X-MAP:URI="blob",BYTERANGE="{len(encrypted_init)}@0"',
            '#EXT-X-KEY:METHOD=AES-128,URI="key"',
        ]
        for index, segment in enumerate(media.segments):
            plaintext = Path(local_path(segment.resource.uri)).read_bytes()
            ciphertext = AES.new(
                key, AES.MODE_CBC, (100 + index).to_bytes(16, "big")
            ).encrypt(pad(plaintext, 16))
            offset = len(blob)
            blob.extend(ciphertext)
            lines += [
                f"#EXTINF:{segment.duration},",
                f"#EXT-X-BYTERANGE:{len(ciphertext)}@{offset}", "blob",
            ]
        lines.append("#EXT-X-ENDLIST")
        (original.parent / "blob").write_bytes(blob)
        original.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.execute(str(original), "local-fmp4")
        self.execute(self.base + "/fmp4/index.m3u8", "http-fmp4")
        self.assertEqual((original.parent / "blob").read_bytes(), bytes(blob))

    def test_plain_fmp4_and_compatible_map_change(self):
        original = self.make_hls("plain-fmp4", fmp4=True)
        media = parse_media(original.read_text(encoding="utf-8"), original.as_uri())
        first = Path(local_path(media.segments[0].initialization.uri))
        second = first.with_name("second-init.mp4")
        second.write_bytes(first.read_bytes())
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:2"]
        for index, segment in enumerate(media.segments):
            initialization = first.name if index == 0 else second.name
            lines += [
                f'#EXT-X-MAP:URI="{initialization}"',
                f"#EXTINF:{segment.duration},",
                Path(local_path(segment.resource.uri)).name,
            ]
        lines.append("#EXT-X-ENDLIST")
        original.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.execute(self.base + "/plain-fmp4/index.m3u8", "plain-fmp4")

    def test_independent_fmp4_audio(self):
        self.make_hls("video", fmp4=True, video_only=True)
        self.make_hls("audio", fmp4=True, audio_only=True)
        master = self.root / "master.m3u8"
        master.write_text(
            '#EXTM3U\n'
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="audio",DEFAULT=YES,URI="audio/index.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="a"\n'
            'video/index.m3u8\n',
            encoding="utf-8",
        )
        self.execute(self.base + "/master.m3u8", "independent-av")


if __name__ == "__main__":
    unittest.main()