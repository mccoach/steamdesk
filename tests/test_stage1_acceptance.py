import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from downloader import InputRejected, RequestSettings
from merger import MediaMetadata, TrackInput, _prepare_track
from task_engine import TaskController
from ui import SteamDeskApp


class ResultConsumptionGateTests(unittest.TestCase):
    def application(self, controller):
        application = SteamDeskApp.__new__(SteamDeskApp)
        application.root = object()
        application._closing = False
        application._dialog_open = False
        application._controller = controller
        application._on_cancel = Mock()
        return application

    def test_finished_thread_keeps_entry_reserved_until_result_consumed(self):
        events = []
        controller = TaskController(events.append)
        controller._worker(lambda: None, ())
        self.assertEqual(controller.state, "succeeded")
        self.assertFalse(controller.alive)
        application = self.application(controller)

        with patch("ui.messagebox.showinfo") as notice:
            with patch("ui.messagebox.askyesno") as confirmation:
                self.assertTrue(application._is_busy())
                self.assertFalse(application._guard_busy("开始执行"))

        notice.assert_called_once()
        confirmation.assert_not_called()
        application._on_cancel.assert_not_called()
        self.assertIs(application._controller, controller)
        self.assertEqual(events[-1].kind, "worker_finished")

    def test_terminal_event_consumption_releases_entry(self):
        events = []
        controller = TaskController(events.append)
        controller._worker(lambda: None, ())
        application = self.application(controller)
        application._preview_job = True
        application._preview_candidate = None
        application._refresh_controls = Mock()
        application._status_lbl = Mock()

        application._handle_event(events[-1])

        self.assertIsNone(application._controller)
        self.assertFalse(application._is_busy())
        self.assertTrue(application._guard_busy("开始执行"))
        application._refresh_controls.assert_called_once()

    def test_wait_notice_occupies_modal_scope(self):
        controller = TaskController(lambda event: None)
        controller._worker(lambda: None, ())
        application = self.application(controller)

        def notice(*args, **kwargs):
            self.assertTrue(application._dialog_open)
            self.assertFalse(application._guard_busy("再次解析"))

        with patch("ui.messagebox.showinfo", side_effect=notice):
            self.assertFalse(application._guard_busy("解析"))
        self.assertFalse(application._dialog_open)


class RequestHeaderValidationTests(unittest.TestCase):
    def settings(self, headers=(), user_agent="test"):
        return RequestSettings(headers, "", 1, 0, user_agent, 1, 0, True)

    def test_invalid_header_names_rejected_before_request(self):
        for name in ("", "X Test", "X:Test", "X(Test)", "中文", "X\tTest"):
            with self.subTest(name=name):
                with self.assertRaises(InputRejected):
                    self.settings(((name, "fixture"),))

    def test_invalid_header_values_rejected_before_request(self):
        for value in (
            " leading", "\tleading", "a\nb", "a\rb", "a\x00b",
            "a\x7fb", "未编码的中文",
        ):
            with self.subTest(value=value):
                with self.assertRaises(InputRejected):
                    self.settings((("X-Test", value),))

    def test_invalid_user_agent_rejected_before_request(self):
        for value in (" leading", "bad\nagent", "未编码"):
            with self.subTest(value=value):
                with self.assertRaises(InputRejected):
                    self.settings(user_agent=value)

    def test_valid_token_and_encoded_header_values_accepted(self):
        settings = self.settings((
            ("X-Test_1", "fixture"),
            ("Referer", "https://example.invalid/%E4%B8%AD%E6%96%87"),
            ("Cookie", "fixture=1; other=2"),
        ))
        self.assertEqual(len(settings.headers), 3)
        self.assertEqual(len(settings.identity()), 64)

    def test_case_insensitive_duplicate_headers_rejected(self):
        with self.assertRaises(InputRejected):
            self.settings((("X-Test", "one"), ("x-test", "two")))


class IndependentMediaValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = tuple(str(self.root / name) for name in ("one.ts", "two.ts"))
        for path in self.files:
            Path(path).write_bytes(b"fixture")
        self.control = TaskController(lambda event: None)
        self.control.phase("validating")
        self.video = {"codec_type": "video", "codec_name": "h264"}
        self.audio = {"codec_type": "audio", "codec_name": "aac"}

    def prepare(self, metadata, durations=()):
        with patch("merger.probe_media", side_effect=metadata):
            return _prepare_track(
                TrackInput(self.files, durations),
                "auto", "ffprobe", 1, self.control,
            )

    def test_audio_appearing_after_video_only_first_fragment_rejected(self):
        metadata = (
            MediaMetadata((self.video,), "mpegts", 1),
            MediaMetadata((self.video, self.audio), "mpegts", 1),
        )
        with self.assertRaisesRegex(InputRejected, "轨道结构变化"):
            self.prepare(metadata, (1, 1))

    def test_audio_disappearing_after_first_fragment_rejected(self):
        metadata = (
            MediaMetadata((self.video, self.audio), "mpegts", 1),
            MediaMetadata((self.video,), "mpegts", 1),
        )
        with self.assertRaisesRegex(InputRejected, "轨道结构变化"):
            self.prepare(metadata, (1, 1))

    def test_indirect_descriptions_rejected_as_independent_media(self):
        for format_name in ("hls", "concat", "dash", "sdp", "image2"):
            with self.subTest(format_name=format_name):
                metadata = (
                    MediaMetadata((self.video,), format_name, 1),
                    MediaMetadata((self.video,), format_name, 1),
                )
                with self.assertRaisesRegex(InputRejected, "不是当前支持的独立媒体"):
                    self.prepare(metadata)

    def test_stable_video_audio_structure_accepted(self):
        metadata = (
            MediaMetadata((self.video, self.audio), "mpegts", 1),
            MediaMetadata((self.video, self.audio), "mpegts", 1),
        )
        _, kind, duration, _ = self.prepare(metadata, (1, 1))
        self.assertEqual(kind, "video")
        self.assertEqual(duration, 2)

    def test_stable_audio_only_structure_accepted(self):
        metadata = (
            MediaMetadata((self.audio,), "aac", 1),
            MediaMetadata((self.audio,), "aac", 1),
        )
        _, kind, duration, _ = self.prepare(metadata)
        self.assertEqual(kind, "audio")
        self.assertEqual(duration, 2)


if __name__ == "__main__":
    unittest.main()