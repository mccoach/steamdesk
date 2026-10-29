import queue
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from downloader import (
    AcquisitionError, AcquisitionStopped, InputRejected, RequestSettings,
    ResourceClient, ResourceSpec, TaskCancelled, TaskWorkspace, local_path,
)
from merger import check_tools
from parser import parse_media
from task_engine import TaskController, TaskEvent, TaskPlan, TrackPlan
from ui import SteamDeskApp


class TaskLifecycleTests(unittest.TestCase):
    def test_settings_immutable_and_finite(self):
        settings = RequestSettings((), "", 1, 0, "test", 1, 0, True)
        with self.assertRaises(FrozenInstanceError):
            settings.timeout = 2
        with self.assertRaises(InputRejected):
            RequestSettings((), "", float("nan"), 0, "test", 1, 0, True)

    def test_pause_cancel_wakes_waiter(self):
        control = TaskController(lambda event: None)
        control.phase("acquiring")
        control.pause()
        errors = []
        ended = threading.Event()
        def wait():
            try:
                control.checkpoint()
            except TaskCancelled:
                errors.append("cancelled")
            finally:
                ended.set()
        thread = threading.Thread(target=wait)
        thread.start()
        control.cancel()
        self.assertTrue(ended.wait(3))
        thread.join()
        self.assertEqual(errors, ["cancelled"])

    def test_phase_cannot_clear_cancel(self):
        control = TaskController(lambda event: None)
        control.phase("preparing")
        control.cancel()
        with self.assertRaises(TaskCancelled):
            control.phase("acquiring")

    def test_failure_while_paused_releases_pool_without_resume(self):
        control = TaskController(lambda event: None)
        settings = RequestSettings((), "", 1, 0, "test", 2, 0, False)
        client = ResourceClient(settings, control)
        fail_ready = threading.Event()
        waiter_ready = threading.Event()
        release_failure = threading.Event()
        waiter_stopped = threading.Event()
        ended = threading.Event()
        errors = []
        plan = TaskPlan(
            TrackPlan((
                ResourceSpec("https://example.invalid/a"),
                ResourceSpec("https://example.invalid/b"),
            ), (1, 1)),
            None, "auto", "out.mp4", "", "", (), "id",
        )
        def acquire(uri, workspace):
            if uri.uri.endswith("/a"):
                fail_ready.set()
                if not release_failure.wait(5):
                    raise AssertionError("失败信号未释放")
                raise AcquisitionError("original failure")
            waiter_ready.set()
            if not release_failure.wait(5):
                raise AssertionError("等待线程未收到信号")
            try:
                control.checkpoint()
            except AcquisitionStopped:
                waiter_stopped.set()
                raise
            raise AssertionError("暂停工作不应正常返回")
        def run():
            try:
                control._acquire(plan, client, object())
            except AcquisitionError as error:
                errors.append(str(error))
            finally:
                ended.set()
        with patch.object(client, "acquire", side_effect=acquire):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(fail_ready.wait(3))
                self.assertTrue(waiter_ready.wait(3))
                control.pause()
                release_failure.set()
                self.assertTrue(ended.wait(5), "暂停期间失败导致线程池未回收")
                self.assertTrue(waiter_stopped.is_set())
                self.assertEqual(errors, ["original failure"])
            finally:
                # 只在测试结束/失败清理时取消；成功断言前从不 resume。
                control.cancel()
                control.stop_acquisition()
                release_failure.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_defect_propagates_and_emits_terminal_event(self):
        events = []
        control = TaskController(events.append)
        def defect():
            raise TypeError("defect")
        with self.assertRaisesRegex(TypeError, "defect"):
            control._worker(defect, ())
        self.assertEqual(events[-1].kind, "worker_finished")
        self.assertEqual(events[-1].payload.category, "program_defect")

    def test_partial_submission_reported_on_later_failure(self):
        events = []
        control = TaskController(events.append)
        def operation():
            control.output_committed("media.mp4")
            raise AcquisitionError("subtitle failure")
        control._worker(operation, ())
        self.assertEqual(events[-1].payload.status, "failed")
        self.assertEqual(events[-1].payload.committed_outputs, ("media.mp4",))

    def test_confirmation_waits_for_explicit_answer(self):
        messages = queue.Queue()
        control = TaskController(messages.put)
        ended = threading.Event()
        def run():
            control.confirm("是否继续")
            ended.set()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            while messages.get(timeout=3).kind != "confirm":
                pass
            self.assertFalse(ended.is_set())
            control.answer(True)
            self.assertTrue(ended.wait(3))
        finally:
            if not ended.is_set():
                control.cancel()
            thread.join(3)

    def test_old_event_ignored(self):
        application = SteamDeskApp.__new__(SteamDeskApp)
        application._controller = TaskController(lambda event: None)
        application._log_msg = Mock()
        application._handle_event(TaskEvent("old-task", "log", "old"))
        application._log_msg.assert_not_called()

    def test_missing_ffprobe_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            ffmpeg = Path(directory) / "ffmpeg.exe"
            ffmpeg.write_bytes(b"placeholder")
            with patch("merger._tool_run") as execute:
                with self.assertRaises(InputRejected):
                    check_tools(str(ffmpeg), 1)
            execute.assert_not_called()

    def test_unsupported_and_live_rejected(self):
        for text in (
            "#EXTM3U\n#EXTINF:1,\na.ts\n",
            "#EXTM3U\n#EXT-X-DISCONTINUITY\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n",
            "#EXTM3U\n#EXT-X-BYTERANGE:10\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n",
            "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"key\"\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(InputRejected):
                    parse_media(text, "https://example.invalid/index.m3u8")

    def test_network_resolution_uses_full_base(self):
        playlist = parse_media(
            "#EXTM3U\n#EXTINF:1,\n/root.ts\n#EXTINF:1,\n//cdn.example.invalid/a.ts\n#EXT-X-ENDLIST\n",
            "https://example.invalid/path/index.m3u8?signature=fixture",
        )
        self.assertEqual(
            tuple(segment.resource.uri for segment in playlist.segments),
            ("https://example.invalid/root.ts", "https://cdn.example.invalid/a.ts"),
        )

    def test_file_uri_is_decoded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 空格%20.ts"
            self.assertEqual(Path(local_path(path.as_uri())), path)


class ResourceHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.requests = []
        log = self.requests
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                log.append((self.path, dict(self.headers)))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/nested/index.m3u8")
                    self.end_headers()
                    return
                if self.path == "/missing":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.path == "/nested/index.m3u8":
                    body = b"#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n"
                else:
                    if self.headers.get("If-None-Match") == '"v1"':
                        self.send_response(304)
                        self.end_headers()
                        return
                    body = b"complete-media"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", '"v1"')
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, format, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.control = TaskController(lambda event: None)
        self.control.phase("acquiring")
        self.settings = RequestSettings((), "", 2, 1, "test", 2, 0, True)
        self.client = ResourceClient(self.settings, self.control)
        self.addCleanup(self.client.close)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_redirect_final_location_is_base(self):
        text, final = self.client.read_text(self.base + "/redirect")
        playlist = parse_media(text, final)
        self.assertEqual(playlist.segments[0].resource.uri, self.base + "/nested/a.ts")

    def test_atomic_resource_and_strong_etag_resume(self):
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None) as workspace:
            first = self.client.acquire(ResourceSpec(self.base + "/media"), workspace)
            second = self.client.acquire(ResourceSpec(self.base + "/media"), workspace)
            self.assertEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b"complete-media")
            self.assertEqual(self.requests[-1][1].get("If-None-Match"), '"v1"')
            self.assertEqual(list(Path(workspace.directory).glob("*.part")), [])

    def test_corrupt_nonempty_cache_reacquired(self):
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None) as workspace:
            path = self.client.acquire(ResourceSpec(self.base + "/media"), workspace)
            Path(path).write_bytes(b"corrupt-media!")
            self.client.acquire(ResourceSpec(self.base + "/media"), workspace)
            self.assertEqual(Path(path).read_bytes(), b"complete-media")
            self.assertIsNone(self.requests[-1][1].get("If-None-Match"))

    def test_404_not_retried(self):
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None) as workspace:
            with self.assertRaises(AcquisitionError):
                self.client.acquire(ResourceSpec(self.base + "/missing"), workspace)
        self.assertEqual(len(self.requests), 1)

    def test_local_resource_is_readonly_reference(self):
        source = self.root / "中文 空格.ts"
        source.write_bytes(b"source")
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None) as workspace:
            path = self.client.acquire(ResourceSpec(source.as_uri()), workspace)
            workspace.cleanup((path,))
        self.assertEqual(Path(path).read_bytes(), b"source")

    def test_cross_origin_drops_all_custom_credentials(self):
        observed = []
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append(dict(self.headers))
                body = b"text"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, format, *args):
                pass
        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
        destination_thread.start()
        target = f"http://127.0.0.1:{destination.server_port}/text"
        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
            def log_message(self, format, *args):
                pass
        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        settings = RequestSettings(
            (("Authorization", "fixture"), ("Cookie", "fixture=1"), ("X-Api-Key", "fixture")),
            "", 2, 0, "test", 1, 0, False,
        )
        client = ResourceClient(settings, self.control)
        try:
            text, final = client.read_text(
                f"http://127.0.0.1:{redirect.server_port}/redirect"
            )
            self.assertEqual(text, "text")
            for name in ("Authorization", "Cookie", "X-Api-Key"):
                self.assertNotIn(name, observed[0])
        finally:
            client.close()
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join()
            destination.shutdown()
            destination.server_close()
            destination_thread.join()


if __name__ == "__main__":
    unittest.main()