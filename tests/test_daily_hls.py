import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from downloader import (
    AcquisitionError, DecryptionError, InputRejected, KeyDeclaration,
    RequestSettings, ResourceClient, ResourceSpec, TaskCancelled, TaskWorkspace,
)
from merger import (
    MediaMetadata, TrackInput, _input_arguments, _prepare_track, _write_hls,
)
from parser import attributes, load_snapshot, parse_media
from task_engine import ExecutionRequest, TaskController, playlist_track


class PlaylistFormatTests(unittest.TestCase):
    def parse(self, declarations, segments="#EXTINF:1,\na.ts\n"):
        return parse_media(
            "#EXTM3U\n" + declarations + segments + "#EXT-X-ENDLIST\n",
            "https://example.invalid/path/index.m3u8",
        )

    def test_default_iv_uses_media_sequence(self):
        playlist = self.parse(
            '#EXT-X-MEDIA-SEQUENCE:45\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n',
            "#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n",
        )
        self.assertEqual([segment.sequence for segment in playlist.segments], [45, 46])
        self.assertEqual(playlist.segments[1].resource.iv, (46).to_bytes(16, "big"))

    def test_explicit_iv_and_none(self):
        playlist = self.parse(
            '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x12\n',
            "#EXTINF:1,\na.ts\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:1,\nb.ts\n",
        )
        self.assertEqual(playlist.segments[0].resource.iv, (18).to_bytes(16, "big"))
        self.assertIsNone(playlist.segments[1].resource.key)

    def test_key_scope_not_merged_by_uri(self):
        playlist = self.parse(
            '#EXT-X-KEY:METHOD=AES-128,URI="key"\n',
            '#EXTINF:1,\na.ts\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:1,\nb.ts\n',
        )
        self.assertNotEqual(
            playlist.segments[0].resource.key, playlist.segments[1].resource.key
        )

    def test_explicit_and_implicit_media_range(self):
        playlist = self.parse(
            "",
            "#EXTINF:1,\n#EXT-X-BYTERANGE:10@3\nblob\n"
            "#EXTINF:1,\n#EXT-X-BYTERANGE:7\nblob\n",
        )
        self.assertEqual(
            [segment.resource.byte_range for segment in playlist.segments],
            [(3, 10), (13, 7)],
        )

    def test_implicit_range_requires_previous_same_resource(self):
        for segments in (
            "#EXTINF:1,\n#EXT-X-BYTERANGE:7\nblob\n",
            "#EXTINF:1,\n#EXT-X-BYTERANGE:10@0\none\n"
            "#EXTINF:1,\n#EXT-X-BYTERANGE:7\ntwo\n",
            "#EXTINF:1,\n#EXT-X-BYTERANGE:10@0\nblob\n"
            "#EXTINF:1,\nblob\n#EXTINF:1,\n#EXT-X-BYTERANGE:7\nblob\n",
        ):
            with self.subTest(segments=segments):
                with self.assertRaises(InputRejected):
                    self.parse("", segments)

    def test_map_has_own_key_iv_and_range(self):
        playlist = self.parse(
            '#EXT-X-KEY:METHOD=AES-128,URI="init-key",IV=0x1\n'
            '#EXT-X-MAP:URI="blob",BYTERANGE="32@0"\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="media-key"\n',
        )
        segment = playlist.segments[0]
        self.assertTrue(segment.initialization.key.uri.endswith("/init-key"))
        self.assertTrue(segment.resource.key.uri.endswith("/media-key"))
        self.assertEqual(segment.initialization.byte_range, (0, 32))
        self.assertEqual(segment.initialization.iv, (1).to_bytes(16, "big"))
        self.assertIn(segment.initialization, playlist_track(playlist).all_resources())

    def test_map_changes_preserved(self):
        playlist = self.parse(
            '#EXT-X-MAP:URI="init-a"\n',
            '#EXTINF:1,\na.m4s\n#EXT-X-MAP:URI="init-b"\n#EXTINF:1,\nb.m4s\n',
        )
        self.assertTrue(playlist.segments[0].initialization.uri.endswith("/init-a"))
        self.assertTrue(playlist.segments[1].initialization.uri.endswith("/init-b"))

    def test_invalid_encryption_and_map_rejected(self):
        for declaration in (
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n',
            '#EXT-X-KEY:METHOD=AES-128,URI="key",KEYFORMAT="other"\n',
            '#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0xBADZ\n',
            '#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-MAP:URI="init"\n',
            '#EXT-X-MAP:URI="blob",BYTERANGE="32"\n',
            "#EXT-X-MEDIA-SEQUENCE:-1\n",
        ):
            with self.subTest(declaration=declaration):
                with self.assertRaises(InputRejected):
                    self.parse(declaration)

    def test_attributes_do_not_ignore_bad_suffix(self):
        for text in (
            '#EXT-X-KEY:METHOD=AES-128,URI="key"garbage',
            '#EXT-X-KEY:METHOD=AES-128,URI="key",',
            '#EXT-X-KEY:METHOD=AES-128,URI="key",URI="other"',
        ):
            with self.subTest(text=text):
                with self.assertRaises(InputRejected):
                    attributes(text)


class LocalResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = []
        self.control = TaskController(self.events.append)
        self.control.phase("preparing")
        self.settings = RequestSettings((), "", 2, 0, "test", 2, 0, True, chunk_size=7)
        self.client = ResourceClient(self.settings, self.control)
        self.addCleanup(self.client.close)
        self.key = bytes(range(16))
        self.key_path = self.root / "key"
        self.key_path.write_bytes(self.key)

    def workspace(self):
        return TaskWorkspace(str(self.root), "a" * 64, self.control.notice)

    def encrypted(self, plaintext, iv):
        path = self.root / "encrypted"
        path.write_bytes(AES.new(self.key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16)))
        return ResourceSpec(
            path.as_uri(), key=KeyDeclaration(self.key_path.as_uri(), "scope"), iv=iv
        )

    def test_decrypt_preserves_original_and_key(self):
        resource = self.encrypted(b"media" * 100, (3).to_bytes(16, "big"))
        ciphertext = (self.root / "encrypted").read_bytes()
        self.client.prepare_keys((resource,))
        with self.workspace() as workspace:
            target = self.client.acquire(resource, workspace)
            self.assertEqual(Path(target).read_bytes(), b"media" * 100)
            self.assertNotIn(
                self.key.hex(), Path(workspace.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(list(Path(workspace.directory).glob("*.part")), [])
            workspace.cleanup((target,))
        self.assertEqual((self.root / "encrypted").read_bytes(), ciphertext)
        self.assertEqual(self.key_path.read_bytes(), self.key)

    def test_local_range_and_bounds(self):
        source = self.root / "blob"
        source.write_bytes(b"0123456789")
        with self.workspace() as workspace:
            target = self.client.acquire(ResourceSpec(source.as_uri(), (2, 4)), workspace)
            self.assertEqual(Path(target).read_bytes(), b"2345")
            with self.assertRaises(InputRejected):
                self.client.acquire(ResourceSpec(source.as_uri(), (8, 4)), workspace)
        self.assertEqual(source.read_bytes(), b"0123456789")

    def test_range_selected_before_decryption(self):
        iv = bytes(16)
        encrypted = AES.new(self.key, AES.MODE_CBC, iv).encrypt(pad(b"payload", 16))
        source = self.root / "blob"
        source.write_bytes(b"prefix" + encrypted + b"suffix")
        resource = ResourceSpec(
            source.as_uri(), (6, len(encrypted)),
            KeyDeclaration(self.key_path.as_uri(), "scope"), iv,
        )
        self.client.prepare_keys((resource,))
        with self.workspace() as workspace:
            target = self.client.acquire(resource, workspace)
            self.assertEqual(Path(target).read_bytes(), b"payload")

    def test_invalid_key_length(self):
        resource = self.encrypted(b"payload", bytes(16))
        for key in (b"", b"short", bytes(17)):
            with self.subTest(length=len(key)):
                self.key_path.write_bytes(key)
                with self.assertRaises(DecryptionError):
                    self.client.prepare_keys((resource,))

    def test_bad_ciphertext_not_committed(self):
        resource = self.encrypted(b"payload", bytes(16))
        self.client.prepare_keys((resource,))
        for ciphertext in (
            b"short",
            AES.new(self.key, AES.MODE_CBC, bytes(16)).encrypt(b"x" * 16),
        ):
            with self.subTest(length=len(ciphertext)):
                (self.root / "encrypted").write_bytes(ciphertext)
                with self.workspace() as workspace:
                    with self.assertRaises(DecryptionError):
                        self.client.acquire(resource, workspace)
                    self.assertEqual(list(Path(workspace.directory).glob("*.media")), [])
                    self.assertEqual(list(Path(workspace.directory).glob("*.part")), [])

    def test_wrong_key_with_known_invalid_padding(self):
        resource = self.encrypted(b"payload", bytes(16))
        wrong = bytes(range(16, 32))
        while True:
            plaintext = AES.new(wrong, AES.MODE_CBC, bytes(16)).decrypt(
                (self.root / "encrypted").read_bytes()
            )
            padding = plaintext[-1]
            if not 1 <= padding <= 16 or plaintext[-padding:] != bytes([padding]) * padding:
                break
            wrong = bytes((value + 1) % 256 for value in wrong)
        self.key_path.write_bytes(wrong)
        self.client.prepare_keys((resource,))
        with self.workspace() as workspace:
            with self.assertRaises(DecryptionError):
                self.client.acquire(resource, workspace)

    def test_cancel_during_decryption_preserves_cache_and_cleans_parts(self):
        resource = self.encrypted(b"payload" * 100, bytes(16))
        self.client.prepare_keys((resource,))
        original = self.client._decrypt

        def decrypt(*args):
            self.control.cancel()
            return original(*args)

        with self.workspace() as workspace:
            with patch.object(self.client, "_decrypt", side_effect=decrypt):
                with self.assertRaises(TaskCancelled):
                    self.client.acquire(resource, workspace)
            self.assertEqual(list(Path(workspace.directory).glob("*.media")), [])
            self.assertEqual(list(Path(workspace.directory).glob("*.part")), [])

    def test_key_scope_once_and_separate_declarations(self):
        first = self.encrypted(b"payload", bytes(16))
        second = ResourceSpec(
            first.uri, key=KeyDeclaration(self.key_path.as_uri(), "second"), iv=bytes(16)
        )
        with patch.object(self.client, "read_bytes", return_value=(self.key, self.key_path.as_uri())) as read:
            self.client.prepare_keys((first, first, second))
            self.client.prepare_keys((first,))
        self.assertEqual(read.call_count, 2)

    def test_decryption_has_own_result_category(self):
        def operation():
            raise DecryptionError("invalid ciphertext")
        self.control._worker(operation, ())
        self.assertEqual(self.events[-1].payload.category, "decryption")

    def test_plan_protects_local_key_and_initialization(self):
        playlist = self.root / "index.m3u8"
        playlist.write_text(
            '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key",IV=0x1\n'
            '#EXT-X-MAP:URI="init"\n#EXTINF:1,\nmedia\n#EXT-X-ENDLIST\n',
            encoding="utf-8",
        )
        snapshot = load_snapshot(str(playlist), self.client)
        request = ExecutionRequest(
            snapshot, self.settings, 0, True, False,
            str(self.key_path), str(self.root / "subtitle.vtt"), "ffmpeg", 1, False,
        )
        with self.assertRaises(InputRejected):
            self.control._plan(request, self.client)


class RangeHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.body = b"0123456789abcdef"
        self.key = bytes(range(16))
        self.observed = []
        self.mode = "correct"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.observed.append((self.path, dict(self.headers)))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/blob")
                    self.end_headers()
                    return
                if self.path == "/key":
                    content = owner.key
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                if owner.mode == "ignore":
                    self.send_response(200)
                    content = owner.body
                else:
                    start, end = map(int, self.headers["Range"][6:].split("-"))
                    content = owner.body[start:end + 1]
                    self.send_response(206)
                    self.send_header(
                        "Content-Range",
                        f"bytes {start if owner.mode != 'wrong' else start + 1}-{end}/{len(owner.body)}",
                    )
                length = len(content) + (3 if owner.mode == "truncated" else 0)
                self.send_header("Content-Length", str(length))
                self.send_header("ETag", '"v1"')
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.control = TaskController(lambda event: None)
        self.control.phase("acquiring")
        self.client = ResourceClient(
            RequestSettings((), "", 2, 0, "test", 2, 0, True), self.control
        )
        self.addCleanup(self.client.close)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def workspace(self):
        return TaskWorkspace(str(self.root), "b" * 64, self.control.notice)

    def test_range_identity_and_resume(self):
        first = ResourceSpec(self.base + "/blob", (2, 5))
        second = ResourceSpec(self.base + "/blob", (8, 3))
        with self.workspace() as workspace:
            a = self.client.acquire(first, workspace)
            b = self.client.acquire(second, workspace)
            self.assertNotEqual(a, b)
            self.assertEqual(Path(a).read_bytes(), b"23456")
            self.assertEqual(Path(b).read_bytes(), b"89a")
            self.assertEqual(self.client.acquire(first, workspace), a)
            self.assertEqual(self.observed[-1][1]["Range"], "bytes=2-6")
            self.assertEqual(self.observed[-1][1]["If-None-Match"], '"v1"')

    def test_ignored_wrong_and_truncated_range_rejected(self):
        for mode in ("ignore", "wrong", "truncated"):
            with self.subTest(mode=mode):
                self.mode = mode
                with self.workspace() as workspace:
                    with self.assertRaises(AcquisitionError):
                        self.client.acquire(ResourceSpec(self.base + "/blob", (2, 5)), workspace)
                    self.assertEqual(list(Path(workspace.directory).glob("*.media")), [])
                    self.assertEqual(list(Path(workspace.directory).glob("*.part")), [])

    def test_redirect_keeps_range(self):
        with self.workspace() as workspace:
            target = self.client.acquire(ResourceSpec(self.base + "/redirect", (2, 5)), workspace)
            self.assertEqual(Path(target).read_bytes(), b"23456")
            self.assertEqual(self.observed[-1][1]["Range"], "bytes=2-6")

    def test_encrypted_truncation_is_acquisition_not_decryption(self):
        resource = ResourceSpec(
            self.base + "/blob", (0, 16),
            KeyDeclaration(self.base + "/key", "scope"), bytes(16),
        )
        self.client.prepare_keys((resource,))
        self.mode = "truncated"
        with self.workspace() as workspace:
            with self.assertRaises(AcquisitionError):
                self.client.acquire(resource, workspace)

    def test_changed_key_cannot_reuse_old_plaintext(self):
        iv = bytes(16)
        resource = ResourceSpec(
            self.base + "/blob", (0, 16),
            KeyDeclaration(self.base + "/key", "scope"), iv,
        )
        self.body = AES.new(self.key, AES.MODE_CBC, iv).encrypt(pad(b"first", 16))
        self.client.prepare_keys((resource,))
        with self.workspace() as workspace:
            first = self.client.acquire(resource, workspace)
            self.assertEqual(Path(first).read_bytes(), b"first")
            self.key = bytes(range(16, 32))
            self.body = AES.new(self.key, AES.MODE_CBC, iv).encrypt(pad(b"second", 16))
            other = ResourceClient(self.client.settings, self.control)
            try:
                other.prepare_keys((resource,))
                second = other.acquire(resource, workspace)
                self.assertNotEqual(first, second)
                self.assertEqual(Path(second).read_bytes(), b"second")
                self.assertNotIn("If-None-Match", self.observed[-1][1])
            finally:
                other.close()


class PackagingContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.control = TaskController(lambda event: None)
        self.control.phase("validating")

    def test_raw_aac_hls_is_audio(self):
        source = self.root / "audio.media"
        source.write_bytes(b"fixture")
        metadata = MediaMetadata(
            ({"codec_type": "audio", "codec_name": "aac"},), "aac", 1
        )
        with patch("merger.probe_media", return_value=metadata):
            _, kind, duration, _ = _prepare_track(
                TrackInput((str(source),), (1,)), "auto", "ffprobe", 1, self.control
            )
        self.assertEqual(kind, "audio")
        self.assertEqual(duration, 1)

    def test_fmp4_probe_receives_init_and_fragment(self):
        initialization = self.root / "init"
        fragment = self.root / "fragment"
        initialization.write_bytes(b"init")
        fragment.write_bytes(b"media")
        metadata = MediaMetadata(
            ({"codec_type": "video", "codec_name": "h264"},), "mov,mp4", 1
        )

        def probe(path, ffprobe, timeout):
            self.assertEqual(Path(path).read_bytes(), b"initmedia")
            return metadata

        with patch("merger.probe_media", side_effect=probe):
            _, kind, _, _ = _prepare_track(
                TrackInput((str(fragment),), (1,), (str(initialization),)),
                "auto", "ffprobe", 1, self.control,
            )
        self.assertEqual(kind, "video")
        self.assertEqual(initialization.read_bytes(), b"init")

    def test_packaging_has_explicit_control_and_actual_extensions(self):
        source = self.root / "source.media"
        source.write_bytes(b"ts")
        work = self.root / "work"
        work.mkdir()
        arguments = _input_arguments(
            TrackInput((str(source),), (1,)), str(work), "primary", "mpegts", self.control
        )
        self.assertEqual(arguments[-1], str(work / "primary.m3u8"))
        self.assertEqual((work / "primary_00000000.ts").read_bytes(), b"ts")
        self.assertEqual(source.read_bytes(), b"ts")

    def test_copy_observes_cancel(self):
        source = self.root / "source.media"
        source.write_bytes(b"ts")
        self.control.cancel()
        with self.assertRaises(TaskCancelled):
            _input_arguments(
                TrackInput((str(source),), (1,)), str(self.root),
                "primary", "mpegts", self.control,
            )

    def test_local_hls_preserves_map_changes_without_keys_or_ranges(self):
        target = self.root / "local.m3u8"
        _write_hls(
            TrackInput(
                (str(self.root / "a.m4s"), str(self.root / "b.m4s")),
                (1, 1), (str(self.root / "init-a.mp4"), str(self.root / "init-b.mp4")),
            ), str(target),
        )
        text = target.read_text(encoding="utf-8")
        self.assertEqual(text.count("#EXT-X-MAP:"), 2)
        self.assertNotIn("#EXT-X-KEY:", text)
        self.assertNotIn("#EXT-X-BYTERANGE:", text)


if __name__ == "__main__":
    unittest.main()