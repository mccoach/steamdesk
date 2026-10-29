import unittest
from unittest.mock import Mock, patch

from task_engine import TaskController
from ui import SteamDeskApp


class TaskGateContractTests(unittest.TestCase):
    def application(self, state=None):
        application = SteamDeskApp.__new__(SteamDeskApp)
        application._closing = False
        application._dialog_open = False
        application.root = object()
        application._controller = None
        application._on_cancel = Mock()
        if state:
            controller = TaskController(lambda event: None)
            controller.phase(state)
            application._controller = controller
        return application

    def test_idle_allows_without_prompt(self):
        application = self.application()
        with patch("ui.messagebox.askyesno") as prompt:
            self.assertTrue(application._guard_busy("执行"))
        prompt.assert_not_called()

    def test_confirmed_cancel_does_not_allow_current_action(self):
        for state in ("preparing", "acquiring", "packaging", "validating", "committing"):
            with self.subTest(state=state):
                application = self.application(state)
                with patch("ui.messagebox.askyesno", return_value=True):
                    self.assertFalse(application._guard_busy("执行"))
                application._on_cancel.assert_called_once_with()

    def test_declined_cancel_preserves_task(self):
        application = self.application("acquiring")
        with patch("ui.messagebox.askyesno", return_value=False):
            self.assertFalse(application._guard_busy("解析"))
        application._on_cancel.assert_not_called()

    def test_cancelling_does_not_repeat_cancel(self):
        application = self.application("acquiring")
        application._controller.cancel()
        with patch("ui.messagebox.showinfo") as notice:
            with patch("ui.messagebox.askyesno") as confirmation:
                self.assertFalse(application._guard_busy("解析"))
        confirmation.assert_not_called()
        notice.assert_called_once()
        application._on_cancel.assert_not_called()

    def test_finished_during_confirmation_not_cancelled_again(self):
        application = self.application("packaging")
        def answer(*args, **kwargs):
            application._controller = None
            return True
        with patch("ui.messagebox.askyesno", side_effect=answer):
            self.assertFalse(application._guard_busy("执行"))
        application._on_cancel.assert_not_called()
        self.assertTrue(application._guard_busy("执行"))

    def test_nested_dialog_rejects_action(self):
        application = self.application()
        application._dialog_open = True
        self.assertFalse(application._guard_busy("解析"))

    def test_close_pending_rejects_action(self):
        application = self.application()
        application._closing = True
        self.assertFalse(application._guard_busy("执行"))

    def test_busy_entry_points_stop_before_reading_input(self):
        for name in ("_on_parse", "_on_open_m3u8", "_on_open_local_dir", "_on_start_execute"):
            with self.subTest(name=name):
                application = self.application("acquiring")
                controller = application._controller
                with patch("ui.messagebox.askyesno", return_value=True):
                    with patch("ui.filedialog.askopenfilename") as playlist:
                        with patch("ui.filedialog.askdirectory") as directory:
                            getattr(application, name)()
                application._on_cancel.assert_called_once_with()
                playlist.assert_not_called()
                directory.assert_not_called()
                self.assertIs(application._controller, controller)


if __name__ == "__main__":
    unittest.main()