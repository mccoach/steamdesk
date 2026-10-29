import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import RequestSettings, ResourceClient, ResourceSpec
from merger import _write_concat_file, _write_hls, TrackInput
from parser import natural_order, parse_media, load_snapshot
from task_engine import TaskController, TaskPlan, TrackPlan


class MediaOrderContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_parallel_completion_preserves_plan_order(self):
        control = TaskController(lambda event: None)
        settings = RequestSettings((), "", 1, 0, "test", 3, 0, False)
        client = ResourceClient(settings, control)
        uris = tuple(ResourceSpec(f"https://example.invalid/{index}") for index in range(3))
        plan = TaskPlan(TrackPlan(uris, (1, 1, 1)), None, "auto", "out.mp4", "", "", (), "id")
        second, third = threading.Event(), threading.Event()
        completed = []
        lock = threading.Lock()
        def acquire(uri, workspace):
            index = uris.index(uri)
            if index == 0 and not second.wait(5):
                raise AssertionError("第二片未完成")
            if index == 1 and not third.wait(5):
                raise AssertionError("第三片未完成")
            with lock:
                completed.append(index)
            if index == 2:
                third.set()
            if index == 1:
                second.set()
            return f"file-{index}"
        with patch.object(client, "acquire", side_effect=acquire):
            results = control._acquire(plan, client, object())
        self.assertEqual(completed, [2, 1, 0])
        self.assertEqual(tuple(results[uri] for uri in plan.primary.resources), ("file-0", "file-1", "file-2"))

    def test_concat_preserves_supplied_order(self):
        paths = [self.root / name for name in ("z.ts", "a.ts", "m.ts")]
        target = self.root / "concat.txt"
        _write_concat_file(tuple(map(str, paths)), str(target))
        self.assertEqual(
            target.read_text(encoding="utf-8").splitlines(),
            ["file '" + path.resolve().as_posix() + "'" for path in paths],
        )

    def test_playlist_order_overrides_filename(self):
        playlist = parse_media(
            "#EXTM3U\n#EXTINF:1,\nseg_2.ts\n#EXTINF:1,\nseg_0.ts\n"
            "#EXTINF:1,\nseg_1.ts\n#EXT-X-ENDLIST\n",
            (self.root / "index.m3u8").as_uri(),
        )
        self.assertEqual(
            [segment.resource.uri.rsplit("/", 1)[1] for segment in playlist.segments],
            ["seg_2.ts", "seg_0.ts", "seg_1.ts"],
        )

    def test_natural_order_multiple_number_groups(self):
        self.assertEqual(
            sorted(["clip_10.ts", "clip_2.ts", "clip_1_2.ts", "clip_1.ts", "clip_1_10.ts", "plain.ts"], key=natural_order),
            ["clip_1.ts", "clip_1_2.ts", "clip_1_10.ts", "clip_2.ts", "clip_10.ts", "plain.ts"],
        )

    def test_directory_snapshot_keeps_order(self):
        for name in ("clip_10.ts", "clip_2.ts", "clip_1.ts"):
            (self.root / name).write_bytes(b"source")
        snapshot = load_snapshot(str(self.root), object())
        self.assertEqual(
            [Path(path).name for path in snapshot.directory_files],
            ["clip_1.ts", "clip_2.ts", "clip_10.ts"],
        )

    def test_hls_description_keeps_sequence_and_native_paths(self):
        from merger import _ffmpeg_file_reference
        paths = tuple(
            str(self.root / name)
            for name in ("中文 空格%20_2.ts", "中文 空格%20_1.ts")
        )
        target = self.root / "local.m3u8"
        _write_hls(TrackInput(paths, (1, 2)), str(target))
        text = target.read_text(encoding="utf-8")
        references = tuple(_ffmpeg_file_reference(path) for path in paths)
        self.assertLess(text.index(references[0]), text.index(references[1]))
        self.assertIn("中文 空格%20_2.ts", text)
        self.assertNotIn("%2520", text)
        self.assertIn("#EXT-X-ENDLIST", text)


if __name__ == "__main__":
    unittest.main()