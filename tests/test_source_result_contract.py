import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from downloader import AcquisitionError
from parser import load_snapshot
from task_engine import TaskController
from ui import SteamDeskApp


class SnapshotSourceTests(unittest.TestCase):
    def test_media_keeps_original_source_separate_from_redirect_location(self):
        source = "https://example.invalid/original.m3u8"
        final = "https://example.invalid/final/index.m3u8"
        text = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST\n"
        client = SimpleNamespace(read_text=Mock(return_value=(text, final)))

        snapshot = load_snapshot(source, client)

        self.assertEqual(snapshot.source, source)
        self.assertEqual(snapshot.uri, final)
        self.assertEqual(
            snapshot.media.segments[0].resource.uri,
            "https://example.invalid/final/a.ts",
        )
        client.read_text.assert_called_once_with(source)

    def test_master_keeps_original_source_without_fetching_child(self):
        source = "https://example.invalid/original.m3u8"
        final = "https://example.invalid/final/master.m3u8"
        text = (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=1000\n"
            "child.m3u8\n"
        )
        client = SimpleNamespace(read_text=Mock(return_value=(text, final)))

        snapshot = load_snapshot(source, client)

        self.assertEqual(snapshot.source, source)
        self.assertEqual(snapshot.uri, final)
        self.assertEqual(snapshot.kind, "master")
        client.read_text.assert_called_once_with(source)

    def test_directory_keeps_supplied_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = str(Path(directory) / "媒体目录")
            Path(source).mkdir()
            (Path(source) / "1.ts").write_bytes(b"fixture")

            snapshot = load_snapshot(source, object())

            self.assertEqual(snapshot.source, source)
            self.assertEqual(snapshot.kind, "directory")


class ExecutionSourceTests(unittest.TestCase):
    def application(self, current_source, snapshot_source):
        application = SteamDeskApp.__new__(SteamDeskApp)
        application.root = object()
        application._closing = False
        application._dialog_open = False
        application._controller = None
        application._snapshot = SimpleNamespace(source=snapshot_source)
        application._url_var = Mock()
        application._url_var.get.return_value = current_source
        application._read_settings = Mock()
        application._new_controller = Mock()
        return application

    def test_changed_source_rejected_before_execution_parameters_are_read(self):
        application = self.application(
            "https://example.invalid/b.m3u8",
            "https://example.invalid/a.m3u8",
        )

        with patch("ui.messagebox.showwarning") as warning:
            application._on_start_execute()

        warning.assert_called_once()
        application._read_settings.assert_not_called()
        application._new_controller.assert_not_called()
        self.assertIsNone(application._controller)

    def test_cleared_source_cannot_execute_previous_snapshot(self):
        application = self.application(
            "   ", "https://example.invalid/a.m3u8"
        )

        with patch("ui.messagebox.showwarning") as warning:
            application._on_start_execute()

        warning.assert_called_once()
        application._read_settings.assert_not_called()
        application._new_controller.assert_not_called()

    def test_unchanged_source_reaches_existing_request_validation(self):
        source = "https://example.invalid/a.m3u8"
        application = self.application("  " + source + "  ", source)
        settings = object()
        application._read_settings.return_value = settings
        application._snapshot_settings = settings
        # 在已确认的来源检查后，由现有输出校验明确停止。
        from downloader import InputRejected
        application._output_path = Mock(
            side_effect=InputRejected("output validation fixture")
        )

        with patch("ui.messagebox.showwarning") as warning:
            application._on_start_execute()

        application._read_settings.assert_called_once()
        application._output_path.assert_called_once()
        application._new_controller.assert_not_called()
        self.assertIn("output validation fixture", warning.call_args.args[1])


class ClosingResultRecordTests(unittest.TestCase):
    def test_closing_records_failure_and_committed_outputs_before_destroy(self):
        events = []
        controller = TaskController(events.append)

        def operation():
            controller.output_committed("media.mp4")
            raise AcquisitionError("subtitle submission failed")

        controller._worker(operation, ())
        terminal = events[-1]
        application = SteamDeskApp.__new__(SteamDeskApp)
        application.root = object()
        application._controller = controller
        application._closing = True
        application._dialog_open = False
        application._preview_job = False
        application._preview_candidate = None
        application._refresh_controls = Mock()
        actions = []

        def record(message):
            actions.append(("log", message))

        def destroy():
            actions.append(("destroy", None))

        application._log_msg = Mock(side_effect=record)
        application._destroy = Mock(side_effect=destroy)

        with patch("ui.messagebox.showerror") as error_dialog:
            with patch("ui.messagebox.showinfo") as info_dialog:
                application._handle_event(terminal)

        self.assertEqual([kind for kind, _ in actions], ["log", "destroy"])
        message = actions[0][1]
        self.assertIn(controller.task_id, message)
        self.assertIn("status=failed", message)
        self.assertIn("category=acquisition", message)
        self.assertIn("subtitle submission failed", message)
        self.assertIn("media.mp4", message)
        self.assertIsNone(application._controller)
        error_dialog.assert_not_called()
        info_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()