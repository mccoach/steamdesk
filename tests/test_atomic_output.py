import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import TaskCancelled
from merger import (
    MergeExecutionError, MergeOutputError, SubtitleUnsupported,
    _atomic_output, _run_ffmpeg, commit_subtitle, validate_webvtt,
)
from task_engine import TaskController


class AtomicOutputContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "output.mp4"
        self.output.write_bytes(b"old")
        self.events = []
        self.control = TaskController(self.events.append)
        self.control.phase("packaging")

    def assert_clean(self):
        self.assertEqual(list(self.root.glob("._merge_output_*")), [])

    def test_commit_preserves_old_until_replace(self):
        with _atomic_output(str(self.output), self.control) as staged:
            self.assertEqual(Path(staged).parent, self.output.parent)
            self.assertEqual(Path(staged).suffix, ".mp4")
            self.assertEqual(self.output.read_bytes(), b"old")
            Path(staged).write_bytes(b"new")
        self.assertEqual(self.output.read_bytes(), b"new")
        self.assertEqual(self.events[-1].kind, "output_committed")
        self.assert_clean()

    def test_writer_defect_preserves_old_and_propagates(self):
        with self.assertRaisesRegex(TypeError, "defect"):
            with _atomic_output(str(self.output), self.control) as staged:
                Path(staged).write_bytes(b"partial")
                raise TypeError("defect")
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assert_clean()

    def test_empty_result_rejected(self):
        with self.assertRaises(MergeOutputError):
            with _atomic_output(str(self.output), self.control):
                pass
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assert_clean()

    def test_replace_failure_preserves_old(self):
        with patch("merger.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(MergeOutputError):
                with _atomic_output(str(self.output), self.control) as staged:
                    Path(staged).write_bytes(b"new")
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assert_clean()

    def test_cancel_before_start(self):
        self.control.cancel()
        with self.assertRaises(TaskCancelled):
            with _atomic_output(str(self.output), self.control):
                self.fail("不能在取消后写入")
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_cancel_after_write(self):
        with self.assertRaises(TaskCancelled):
            with _atomic_output(str(self.output), self.control) as staged:
                Path(staged).write_bytes(b"new")
                self.control.cancel()
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assert_clean()

    def test_cancel_during_check_rechecked(self):
        with patch(
            "merger._validate_staged_output",
            side_effect=lambda path: self.control.cancel(),
        ):
            with self.assertRaises(TaskCancelled):
                with _atomic_output(str(self.output), self.control) as staged:
                    Path(staged).write_bytes(b"new")
        self.assertEqual(self.output.read_bytes(), b"old")

    def test_failed_new_target_never_appears(self):
        self.output.unlink()
        with self.assertRaises(RuntimeError):
            with _atomic_output(str(self.output), self.control) as staged:
                Path(staged).write_bytes(b"partial")
                raise RuntimeError("failed")
        self.assertFalse(self.output.exists())
        self.assert_clean()

    def test_subtitle_snapshot_commit(self):
        text = validate_webvtt(
            "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n正文\n"
        )
        target = self.root / "subtitle.vtt"
        commit_subtitle(text, str(target), self.control)
        self.assertIn("正文", target.read_text(encoding="utf-8"))

    def test_invalid_subtitles_rejected_before_commit(self):
        for text in (
            "", "WEBVTT\n\n", "WEBVTTinvalid\n\n",
            "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n",
            "WEBVTT\n\n00:00:02.000 --> 00:00:01.000\nwrong\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(SubtitleUnsupported):
                    validate_webvtt(text)

    def test_subtitle_replace_failure_preserves_target(self):
        text = validate_webvtt("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nbody\n")
        target = self.root / "subtitle.vtt"
        target.write_bytes(b"old")
        with patch("merger.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(MergeOutputError):
                commit_subtitle(text, str(target), self.control)
        self.assertEqual(target.read_bytes(), b"old")


class FFmpegProcessContractTests(unittest.TestCase):
    def control(self):
        control = TaskController(lambda event: None)
        control.phase("packaging")
        return control

    def command(self, program):
        return [sys.executable, "-u", "-c", program]

    def test_nonzero_exit_keeps_details(self):
        with self.assertRaises(MergeExecutionError) as caught:
            _run_ffmpeg(
                self.command("import sys; print('failure-details'); sys.exit(7)"),
                self.control(), 1,
            )
        self.assertIn("7", str(caught.exception))
        self.assertIn("failure-details", str(caught.exception))

    def run_observed(self, program, control):
        original = subprocess.Popen
        processes = []
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch("merger.subprocess.Popen", side_effect=launch):
            _run_ffmpeg(self.command(program), control, 1)
        return processes

    def test_success_reaps_and_closes(self):
        processes = self.run_observed("print('out_time_us=1000000')", self.control())
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdout.closed)

    def test_cancel_reaps_running_process(self):
        original = subprocess.Popen
        processes = []
        control = self.control()
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            control.cancel()
            return process
        with patch("merger.subprocess.Popen", side_effect=launch):
            with self.assertRaises(TaskCancelled):
                _run_ffmpeg(
                    self.command("import time; time.sleep(30)"), control, 1
                )
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(processes[0].stdout.closed)

    def test_callback_defect_still_reaps(self):
        original = subprocess.Popen
        processes = []
        control = self.control()
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process
        with patch("merger.subprocess.Popen", side_effect=launch):
            with patch.object(control, "progress", side_effect=TypeError("defect")):
                with self.assertRaisesRegex(TypeError, "defect"):
                    _run_ffmpeg(
                        self.command(
                            "import time; print('out_time_us=1000000'); time.sleep(30)"
                        ), control, 1,
                    )
        self.assertIsNotNone(processes[0].returncode)
        self.assertTrue(processes[0].stdout.closed)


if __name__ == "__main__":
    unittest.main()