import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from downloader import TaskCancelled
from merger import MergeOutputError, _atomic_output
from task_engine import TaskController
from ui import SteamDeskApp


class CommitCancellationOrderingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output.mp4"
        self.output.write_bytes(b"old")
        self.events = []
        self.control = TaskController(self.events.append)
        self.control.phase("committing")

    def test_cancel_accepted_before_commit_rejects_submit(self):
        submit = Mock()
        self.control.cancel()

        with self.assertRaises(TaskCancelled):
            self.control.commit_output(str(self.output), submit)

        submit.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(any(
            event.kind == "output_committed" for event in self.events
        ))

    def test_commit_failure_does_not_record_output(self):
        submit = Mock(side_effect=PermissionError("locked"))

        with self.assertRaises(PermissionError):
            self.control.commit_output(str(self.output), submit)

        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(any(
            event.kind == "output_committed" for event in self.events
        ))
        self.control.cancel()
        self.assertEqual(self.control.state, "cancelling")

    def test_cancel_waits_until_started_commit_is_recorded(self):
        cancel_attempted = threading.Event()
        cancel_returned = threading.Event()
        original_replace = __import__("os").replace

        def cancel():
            cancel_attempted.set()
            self.control.cancel()
            cancel_returned.set()

        cancel_thread = threading.Thread(target=cancel, daemon=True)

        def replace(source, target):
            # 已进入控制事务，另一个线程尝试取消，但不能在提交中间
            # 把状态改成 cancelling。
            cancel_thread.start()
            if not cancel_attempted.wait(3):
                raise AssertionError("取消线程未开始")
            self.assertFalse(cancel_returned.is_set())
            self.assertEqual(self.control.state, "committing")
            self.assertEqual(self.output.read_bytes(), b"old")
            return original_replace(source, target)

        try:
            with patch("merger.os.replace", side_effect=replace):
                with _atomic_output(str(self.output), self.control) as staged:
                    Path(staged).write_bytes(b"new")
            self.assertTrue(cancel_returned.wait(3))
        finally:
            if cancel_thread.ident is not None:
                cancel_thread.join(3)

        self.assertFalse(cancel_thread.is_alive())
        self.assertEqual(self.output.read_bytes(), b"new")
        kinds = [event.kind for event in self.events]
        committed_index = kinds.index("output_committed")
        cancelling_index = next(
            index for index, event in enumerate(self.events)
            if event.kind == "state" and event.payload == "cancelling"
        )
        self.assertLess(committed_index, cancelling_index)
        self.assertEqual(self.control.state, "cancelling")
        self.assertEqual(list(self.root.glob("._merge_output_*")), [])

    def test_atomic_replace_failure_preserves_target_and_no_commit_event(self):
        with patch("merger.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(MergeOutputError):
                with _atomic_output(str(self.output), self.control) as staged:
                    Path(staged).write_bytes(b"new")

        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertFalse(any(
            event.kind == "output_committed" for event in self.events
        ))
        self.assertEqual(list(self.root.glob("._merge_output_*")), [])


class CloseResultConsumptionTests(unittest.TestCase):
    def application(self, controller):
        application = SteamDeskApp.__new__(SteamDeskApp)
        application.root = object()
        application._controller = controller
        application._closing = False
        application._dialog_open = False
        application._preview_job = False
        application._preview_candidate = None
        application._refresh_controls = Mock()
        application._destroy = Mock()
        application._log_msg = Mock()
        application._status_lbl = Mock()
        return application

    def finished_controller(self):
        events = []
        controller = TaskController(events.append)

        def operation():
            controller.output_committed("media.mp4")
            from downloader import AcquisitionError
            raise AcquisitionError("later output failed")

        controller._worker(operation, ())
        self.assertFalse(controller.alive)
        self.assertFalse(controller.busy)
        return controller, events[-1]

    def test_close_waits_for_pending_terminal_result(self):
        controller, terminal = self.finished_controller()
        application = self.application(controller)

        with patch("ui.messagebox.askyesno", return_value=True) as confirmation:
            application._on_close()

        confirmation.assert_called_once()
        self.assertTrue(application._closing)
        self.assertIs(application._controller, controller)
        application._destroy.assert_not_called()

        with patch("ui.messagebox.showerror") as error_dialog:
            application._handle_event(terminal)

        self.assertIsNone(application._controller)
        application._destroy.assert_called_once()
        error_dialog.assert_not_called()

    def test_declining_close_preserves_pending_result(self):
        controller, _ = self.finished_controller()
        application = self.application(controller)

        with patch("ui.messagebox.askyesno", return_value=False):
            application._on_close()

        self.assertFalse(application._closing)
        self.assertIs(application._controller, controller)
        application._destroy.assert_not_called()

    def test_close_after_terminal_consumption_needs_no_task_confirmation(self):
        application = self.application(None)

        with patch("ui.messagebox.askyesno") as confirmation:
            application._on_close()

        confirmation.assert_not_called()
        self.assertTrue(application._closing)
        application._destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()