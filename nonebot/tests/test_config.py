import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import utils


class PersonaFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "src" / "utils.py"
        self.source.parent.mkdir()
        self.location = patch.object(utils, "__file__", str(self.source))
        self.location.start()
        utils.load_config.cache_clear()

    def tearDown(self):
        utils.load_config.cache_clear()
        self.location.stop()
        self.temp.cleanup()

    def config(self, text):
        (self.root / "config.yaml").write_text(text, encoding="utf-8")

    def test_relative_file_is_loaded_with_bom_and_takes_precedence(self):
        (self.root / "persona.txt").write_text("鲸鱼娘的人设\n", encoding="utf-8-sig")
        self.config("persona_file: persona.txt\npersona: old inline persona\n")
        cfg = utils.load_config()
        self.assertEqual(cfg["persona"], "鲸鱼娘的人设")
        self.assertEqual(cfg["persona_file"], "persona.txt")

    def test_absolute_file_is_supported(self):
        persona = self.root / "persona.txt"
        persona.write_text("绝对路径人设", encoding="utf-8")
        self.config("persona_file: " + persona.as_posix() + "\n")
        self.assertEqual(utils.load_config()["persona"], "绝对路径人设")

    def test_original_inline_persona_still_works(self):
        self.config("persona: inline persona\nreply: {max_history: 20}\n")
        self.assertEqual(utils.load_config()["persona"], "inline persona")

    def test_missing_file_has_explicit_error(self):
        self.config("persona_file: missing.txt\n")
        with self.assertRaisesRegex(ValueError, "无法读取.*人设文件"):
            utils.load_config()

    def test_empty_file_has_explicit_error(self):
        (self.root / "persona.txt").write_text(" \n\t", encoding="utf-8")
        self.config("persona_file: persona.txt\n")
        with self.assertRaisesRegex(ValueError, "人设文件不能为空"):
            utils.load_config()

    def test_invalid_file_setting_is_rejected(self):
        for setting in ['""', "null", "123", "[]"]:
            self.config("persona_file: " + setting + "\n")
            with self.assertRaisesRegex(ValueError, "persona_file 必须"):
                utils.load_config()
            utils.load_config.cache_clear()

    def test_file_changes_take_effect_after_cache_clear_or_restart(self):
        persona = self.root / "persona.txt"
        persona.write_text("旧人设", encoding="utf-8")
        self.config("persona_file: persona.txt\n")
        self.assertEqual(utils.load_config()["persona"], "旧人设")
        persona.write_text("新人设", encoding="utf-8")
        self.assertEqual(utils.load_config()["persona"], "旧人设")
        utils.load_config.cache_clear()
        self.assertEqual(utils.load_config()["persona"], "新人设")

    def test_persona_reload_without_clearing_config_cache(self):
        persona = self.root / "persona.txt"
        persona.write_text("旧人设", encoding="utf-8")
        self.config("persona_file: persona.txt\n")
        cfg = utils.load_config()
        persona.write_text("新人设", encoding="utf-8-sig")
        self.assertEqual(utils.load_persona(cfg), "新人设")
        persona.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            utils.load_persona(cfg)
        persona.unlink()
        with self.assertRaisesRegex(ValueError, "无法读取"):
            utils.load_persona(cfg)
        persona.write_text("恢复人设", encoding="utf-8")
        self.assertEqual(utils.load_persona(cfg), "恢复人设")


if __name__ == "__main__":
    unittest.main()
