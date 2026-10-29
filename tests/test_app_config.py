import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_config import AppConfigError, AppConfigStore, DEFAULT_CONFIG


class AppConfigContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.config_path = self.directory / "preferences.json"
        self.path_patch = patch(
            "app_config.DEFAULT_CONFIG_PATH", str(self.config_path)
        )
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_preferences(self, preferences):
        self.config_path.write_text(
            json.dumps(preferences, ensure_ascii=False),
            encoding="utf-8",
        )

    def read_preferences(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def assert_no_temporary_files(self):
        self.assertEqual(list(self.directory.glob("._prefs_*.tmp")), [])

    def test_missing_file_uses_defaults_without_creating_file(self):
        store = AppConfigStore()

        self.assertEqual(store.data, DEFAULT_CONFIG)
        self.assertEqual(store.config_path, str(self.config_path))
        self.assertFalse(self.config_path.exists())

    def test_save_commits_complete_unicode_json(self):
        store = AppConfigStore()
        store.set("output_name", "测试视频", auto_save=False)

        store.save()

        self.assertEqual(self.read_preferences(), store.data)
        self.assertEqual(self.read_preferences()["output_name"], "测试视频")
        self.assert_no_temporary_files()

    def test_replace_failure_preserves_old_file_and_memory_settings(self):
        self.write_preferences({"output_name": "原配置"})
        old_bytes = self.config_path.read_bytes()
        store = AppConfigStore()

        with patch(
            "app_config.os.replace",
            side_effect=PermissionError("replacement denied"),
        ):
            with self.assertRaises(AppConfigError):
                store.set("output_name", "尚未保存的新配置")

        self.assertEqual(self.config_path.read_bytes(), old_bytes)
        self.assertEqual(store.get("output_name"), "尚未保存的新配置")
        self.assert_no_temporary_files()

    def test_disabled_preference_suppresses_set_and_update_disk_writes(self):
        self.write_preferences({
            "prefs_auto_save": False,
            "output_name": "磁盘名称",
        })
        old_bytes = self.config_path.read_bytes()
        store = AppConfigStore()

        store.set("output_name", "内存名称")
        store.update({"threads": 7})

        self.assertEqual(self.config_path.read_bytes(), old_bytes)
        self.assertEqual(store.get("output_name"), "内存名称")
        self.assertEqual(store.get("threads"), 7)

        # save 是显式持久化能力，不受自动保存偏好阻止。
        store.save()
        self.assertEqual(self.read_preferences()["output_name"], "内存名称")
        self.assertEqual(self.read_preferences()["threads"], 7)

    def test_explicit_auto_save_false_suppresses_disk_write(self):
        self.write_preferences({"output_name": "磁盘名称"})
        old_bytes = self.config_path.read_bytes()
        store = AppConfigStore()

        store.set("output_name", "内存名称", auto_save=False)
        store.update({"threads": 6}, auto_save=False)

        self.assertEqual(self.config_path.read_bytes(), old_bytes)
        self.assertEqual(store.get("threads"), 6)

    def test_enabling_auto_save_commits_current_memory_settings(self):
        self.write_preferences({
            "prefs_auto_save": False,
            "output_name": "原名称",
        })
        store = AppConfigStore()
        store.set("output_name", "新名称")

        store.set("prefs_auto_save", True)

        self.assertTrue(self.read_preferences()["prefs_auto_save"])
        self.assertEqual(self.read_preferences()["output_name"], "新名称")

    def test_invalid_json_is_reported_without_overwriting_file(self):
        self.config_path.write_text("{invalid JSON", encoding="utf-8")
        old_bytes = self.config_path.read_bytes()

        with self.assertRaises(AppConfigError) as caught:
            AppConfigStore()

        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.assertEqual(self.config_path.read_bytes(), old_bytes)

    def test_invalid_root_and_unreadable_path_are_not_treated_as_defaults(self):
        self.write_preferences([])

        with self.assertRaises(AppConfigError):
            AppConfigStore()

        os.remove(self.config_path)
        self.config_path.mkdir()

        with self.assertRaises(AppConfigError) as caught:
            AppConfigStore()

        self.assertIsInstance(caught.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()