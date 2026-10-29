import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import InputRejected, TaskWorkspace, CacheConflict, AcquisitionError
from merger import (
    MediaMetadata, MergeInput, MergeOutputError, TrackInput,
    merge_streams, validate_output_inputs,
)
from task_engine import TaskController


class MergeOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.ts"
        self.source.write_bytes(b"source")
        self.output = self.root / "output.mp4"
        self.output.write_bytes(b"old")
        self.control = TaskController(lambda event: None)
        self.control.phase("preparing")
        self.input_meta = MediaMetadata(
            ({"codec_type": "video", "codec_name": "h264"},), "mpegts", 1
        )
        self.output_meta = MediaMetadata(self.input_meta.streams, "mov,mp4", 1)

    def request(self):
        return MergeInput(
            TrackInput((str(self.source),), (1,)),
            str(self.output), "ffmpeg", "ffprobe",
        )

    def fake_ffmpeg(self, command, control, duration):
        Path(command[-1]).write_bytes(b"new")

    def test_merge_preserves_source_and_cleans_work(self):
        with patch("merger.probe_media", side_effect=[self.input_meta, self.output_meta]):
            with patch("merger._run_ffmpeg", side_effect=self.fake_ffmpeg):
                merge_streams(self.request(), self.control, 1)
        self.assertEqual(self.source.read_bytes(), b"source")
        self.assertEqual(self.output.read_bytes(), b"new")
        self.assertEqual(list(self.root.glob("._merge_work_*")), [])

    def test_validation_failure_preserves_source_and_old_output(self):
        bad = MediaMetadata((), "mov,mp4", 1)
        with patch("merger.probe_media", side_effect=[self.input_meta, bad]):
            with patch("merger._run_ffmpeg", side_effect=self.fake_ffmpeg):
                with self.assertRaises(MergeOutputError):
                    merge_streams(self.request(), self.control, 1)
        self.assertEqual(self.output.read_bytes(), b"old")
        self.assertEqual(self.source.read_bytes(), b"source")
        self.assertEqual(list(self.root.glob("._merge_work_*")), [])

    def test_same_path_rejected(self):
        with self.assertRaises(InputRejected):
            validate_output_inputs(str(self.source), [str(self.source)])

    def test_hardlink_alias_rejected(self):
        alias = self.root / "alias.mp4"
        try:
            os.link(self.source, alias)
        except OSError as error:
            self.skipTest(f"无法创建硬链接：{error}")
        with self.assertRaises(InputRejected):
            validate_output_inputs(str(alias), [str(self.source)])

    def test_cleanup_only_registered_consumed_resource(self):
        foreign = self.root / "foreign.ts"
        foreign.write_bytes(b"foreign")
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None) as workspace:
            resource = Path(workspace.resource_path("b" * 64))
            resource.write_bytes(b"cache")
            workspace.commit("b" * 64, 5, hashlib.sha256(b"cache").hexdigest(), "")
            workspace.cleanup((str(resource), str(foreign), str(self.source)))
            self.assertFalse(resource.exists())
            self.assertTrue(Path(workspace.directory).is_dir())
        self.assertEqual(foreign.read_bytes(), b"foreign")
        self.assertEqual(self.source.read_bytes(), b"source")

    def test_cleanup_failure_warns_and_preserves_resource(self):
        messages = []
        original = os.remove
        with TaskWorkspace(str(self.root), "a" * 64, messages.append) as workspace:
            resource = Path(workspace.resource_path("b" * 64))
            resource.write_bytes(b"cache")
            workspace.commit("b" * 64, 5, hashlib.sha256(b"cache").hexdigest(), "")
            def remove(path):
                if os.fspath(path) == str(resource):
                    raise PermissionError("locked")
                return original(path)
            with patch("downloader.os.remove", side_effect=remove):
                workspace.cleanup((str(resource),))
            self.assertTrue(resource.exists())
        self.assertTrue(messages)

    def test_second_executor_rejected(self):
        with TaskWorkspace(str(self.root), "a" * 64, lambda message: None):
            with self.assertRaises(CacheConflict):
                with TaskWorkspace(str(self.root), "a" * 64, lambda message: None):
                    pass

    def test_corrupt_manifest_releases_lock(self):
        directory = self.root / (".steamdesk_task_" + "a" * 64)
        directory.mkdir()
        (directory / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(InputRejected):
            with TaskWorkspace(str(self.root), "a" * 64, lambda message: None):
                pass
        self.assertFalse((directory / "execution.lock").exists())

    def test_manifest_read_permission_failure_releases_lock(self):
        original = open
        directory = self.root / (".steamdesk_task_" + "a" * 64)
        def fail(path, *args, **kwargs):
            if os.fspath(path) == str(directory / "manifest.json"):
                raise PermissionError("denied")
            return original(path, *args, **kwargs)
        with patch("builtins.open", side_effect=fail):
            with self.assertRaises(AcquisitionError):
                with TaskWorkspace(str(self.root), "a" * 64, lambda message: None):
                    pass
        self.assertFalse((directory / "execution.lock").exists())

    def test_unowned_old_work_directory_preserved(self):
        old = self.root / "._merge_work"
        old.mkdir()
        marker = old / "user-file"
        marker.write_bytes(b"user")
        with patch("merger.probe_media", side_effect=[self.input_meta, self.output_meta]):
            with patch("merger._run_ffmpeg", side_effect=self.fake_ffmpeg):
                merge_streams(self.request(), self.control, 1)
        self.assertEqual(marker.read_bytes(), b"user")

    def test_audio_only_has_no_required_video(self):
        audio = MediaMetadata(
            ({"codec_type": "audio", "codec_name": "aac"},), "mpegts", 1
        )
        result = MediaMetadata(audio.streams, "mov,mp4", 1)
        commands = []
        def execute(command, control, duration):
            commands.append(command)
            Path(command[-1]).write_bytes(b"audio")
        with patch("merger.probe_media", side_effect=[audio, result]):
            with patch("merger._run_ffmpeg", side_effect=execute):
                merge_streams(self.request(), self.control, 1)
        self.assertIn("0:a:0", commands[0])
        self.assertNotIn("0:v:0", commands[0])


if __name__ == "__main__":
    unittest.main()