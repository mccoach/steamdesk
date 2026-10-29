import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from downloader import InputRejected, RequestSettings, ResourceSpec, TaskCancelled
from merger import (
    MediaMetadata, MergeExecutionError, MergeInput, MergeOutputError,
    TrackInput, _ffmpeg_file_reference, _prepare_track,
    _write_concat_file, _write_hls, merge_streams, probe_media,
)
from parser import load_snapshot
from task_engine import TaskController, TaskPlan, TrackPlan


class BoundedAcquisitionTests(unittest.TestCase):
    def plan(self, count):
        return TaskPlan(
            TrackPlan(tuple(ResourceSpec(f"https://example.invalid/{index}") for index in range(count))),
            None, "auto", "unused.mp4", "", "", (), "identity",
        )

    def settings(self):
        return RequestSettings((), "", 1, 0, "test", 2, 0, False)

    def test_cancel_does_not_dispatch_entire_playlist(self):
        control = TaskController(lambda event: None)
        first_two_started = threading.Event()
        finished = threading.Event()
        started = []
        errors = []
        lock = threading.Lock()

        def acquire(uri, workspace):
            with lock:
                started.append(uri)
                if len(started) == 2:
                    first_two_started.set()
            control.wait_delay(30)
            return uri

        client = SimpleNamespace(settings=self.settings(), acquire=acquire)

        def execute():
            try:
                control._acquire(self.plan(200), client, object())
            except TaskCancelled:
                errors.append("cancelled")
            finally:
                finished.set()

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        try:
            self.assertTrue(first_two_started.wait(3))
            control.cancel()
            self.assertTrue(finished.wait(5))
            self.assertEqual(errors, ["cancelled"])
            self.assertEqual(len(started), 2)
        finally:
            control.cancel()
            control.stop_acquisition()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_pause_prevents_replenishment_after_active_resources_finish(self):
        control = TaskController(lambda event: None)
        first_two_started = threading.Event()
        release = threading.Event()
        first_two_consumed = threading.Event()
        finished = threading.Event()
        started = []
        results = []
        lock = threading.Lock()

        original_progress = control.progress

        def progress(percent, stage, completed=0, total=0):
            if completed == 2:
                first_two_consumed.set()
            original_progress(percent, stage, completed, total)

        def acquire(uri, workspace):
            with lock:
                started.append(uri)
                if len(started) == 2:
                    first_two_started.set()
            if not release.wait(5):
                raise AssertionError("资源释放信号超时")
            return uri

        client = SimpleNamespace(settings=self.settings(), acquire=acquire)

        def execute():
            try:
                results.append(control._acquire(self.plan(5), client, object()))
            finally:
                finished.set()

        with patch.object(control, "progress", side_effect=progress):
            thread = threading.Thread(target=execute, daemon=True)
            thread.start()
            try:
                self.assertTrue(first_two_started.wait(3))
                control.pause()
                release.set()
                self.assertTrue(first_two_consumed.wait(3))
                self.assertEqual(control.state, "paused")
                with lock:
                    self.assertEqual(len(started), 2)
                self.assertFalse(finished.is_set())
                control.resume()
                self.assertTrue(finished.wait(5))
                self.assertEqual(len(results[0]), 5)
            finally:
                control.cancel()
                control.stop_acquisition()
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_success_reconstructs_all_resources(self):
        control = TaskController(lambda event: None)
        client = SimpleNamespace(
            settings=self.settings(),
            acquire=lambda resource, workspace: "local:" + resource.uri,
        )
        plan = self.plan(20)
        result = control._acquire(plan, client, object())
        self.assertEqual(len(result), 20)
        self.assertEqual(
            tuple(result[resource] for resource in plan.primary.resources),
            tuple("local:" + resource.uri for resource in plan.primary.resources),
        )


class MasterSelectionTests(unittest.TestCase):
    def test_master_preview_does_not_fetch_first_variant(self):
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=2000,RESOLUTION=1920x1080\n"
            "unsupported.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000,RESOLUTION=1280x720\n"
            "supported.m3u8\n"
        )
        client = SimpleNamespace(
            read_text=Mock(return_value=(text, "https://example.invalid/master.m3u8"))
        )
        snapshot = load_snapshot("https://example.invalid/master.m3u8", client)
        client.read_text.assert_called_once_with("https://example.invalid/master.m3u8")
        self.assertEqual(snapshot.kind, "master")
        self.assertEqual(len(snapshot.variants), 2)
        self.assertEqual(snapshot.cached_playlists, ())

    def test_selected_child_is_loaded_once_in_preparation_cache(self):
        text = (
            "#EXTM3U\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
        )
        client = SimpleNamespace(
            read_text=Mock(return_value=(text, "https://example.invalid/selected.m3u8"))
        )
        control = TaskController(lambda event: None)
        loaded = {}
        snapshot = object()
        first = control._playlist(
            "https://example.invalid/selected.m3u8", snapshot, client, loaded
        )
        second = control._playlist(
            "https://example.invalid/selected.m3u8", snapshot, client, loaded
        )
        self.assertIs(first, second)
        self.assertEqual(client.read_text.call_count, 1)


class LocalToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_probe_explicitly_restricts_protocols(self):
        document = {
            "streams": [{"codec_type": "video", "codec_name": "h264"}],
            "format": {"format_name": "mpegts", "duration": "1"},
        }
        with patch("merger._tool_run", return_value=json.dumps(document)) as run:
            result = probe_media("local.ts", "ffprobe", 1)
        command = run.call_args.args[0]
        index = command.index("-protocol_whitelist")
        self.assertEqual(command[index + 1], "file")
        self.assertEqual(result.duration, 1)

    def test_native_reference_preserves_literal_percent_and_spaces(self):
        path = self.root / "中文 空格%20.ts"
        reference = _ffmpeg_file_reference(str(path))
        expected = os.path.abspath(path)
        if os.name == "nt":
            expected = expected.replace("\\", "/")
        self.assertEqual(reference, "file:" + expected)
        self.assertIn("中文 空格%20.ts", reference)
        self.assertNotIn("%2520", reference)

    def test_hls_rejects_line_injection_before_creating_description(self):
        target = self.root / "unsafe.m3u8"
        with self.assertRaises(InputRejected):
            _write_hls(
                TrackInput((str(self.root / "bad\nname.ts"),), (1,)),
                str(target),
            )
        self.assertFalse(target.exists())

    def test_concat_rejects_line_injection(self):
        with self.assertRaises(InputRejected):
            _write_concat_file(
                (str(self.root / "bad\nname.ts"),),
                str(self.root / "unsafe.txt"),
            )

    def test_input_probe_failure_is_input_validation_failure(self):
        source = self.root / "source.ts"
        source.write_bytes(b"source")
        control = TaskController(lambda event: None)
        control.phase("validating")
        with patch("merger.probe_media", side_effect=MergeExecutionError("probe failed")):
            with self.assertRaisesRegex(InputRejected, "输入媒体验证失败"):
                _prepare_track(
                    TrackInput((str(source),), (1,)),
                    "auto", "ffprobe", 1, control,
                )

    def test_output_probe_failure_preserves_old_target(self):
        source = self.root / "source.ts"
        source.write_bytes(b"source")
        output = self.root / "output.mp4"
        output.write_bytes(b"old")
        metadata = MediaMetadata(
            ({"codec_type": "video", "codec_name": "h264"},), "mpegts", 1
        )
        control = TaskController(lambda event: None)
        control.phase("preparing")

        def execute(command, controller, duration):
            Path(command[-1]).write_bytes(b"new")

        request = MergeInput(
            TrackInput((str(source),), (1,)),
            str(output), "ffmpeg", "ffprobe",
        )
        with patch(
            "merger.probe_media",
            side_effect=[metadata, MergeExecutionError("output probe failed")],
        ):
            with patch("merger._run_ffmpeg", side_effect=execute):
                with self.assertRaisesRegex(MergeOutputError, "输出媒体验证失败"):
                    merge_streams(request, control, 1)
        self.assertEqual(output.read_bytes(), b"old")
        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(list(self.root.glob("._merge_output_*")), [])
        self.assertEqual(list(self.root.glob("._merge_work_*")), [])

    def test_tool_hardlink_output_is_rejected_before_probe(self):
        tool = self.root / "tool.exe"
        tool.write_bytes(b"tool")
        output = self.root / "output.mp4"
        try:
            os.link(tool, output)
        except OSError as error:
            self.skipTest(f"无法创建硬链接：{error}")
        source = self.root / "source.ts"
        source.write_bytes(b"source")
        request = MergeInput(
            TrackInput((str(source),), (1,)),
            str(output), str(tool), str(self.root / "ffprobe.exe"),
        )
        control = TaskController(lambda event: None)
        control.phase("preparing")
        with patch("merger.probe_media") as probe:
            with self.assertRaises(InputRejected):
                merge_streams(request, control, 1)
        probe.assert_not_called()
        self.assertEqual(tool.read_bytes(), b"tool")
        self.assertEqual(output.read_bytes(), b"tool")


if __name__ == "__main__":
    unittest.main()