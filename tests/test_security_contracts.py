from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ModuleSecurityContractTests(unittest.TestCase):
    def source(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_every_module_parses_as_python(self) -> None:
        for path in ROOT.glob("*/*.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_downloader_keeps_tls_and_blocks_unbounded_files(self) -> None:
        source = self.source("DownloaderDtg/DownloaderDtg.py")
        self.assertNotIn('"nocheckcertificate": True', source)
        self.assertIn('"max_filesize": 50 * 1024 * 1024', source)
        self.assertIn("safe_media_url", source)

    def test_session_guard_requires_explicit_kick_policy(self) -> None:
        source = self.source("SessionGuardDtg/SessionGuardDtg.py")
        self.assertIn('return self.policy_for(item) == "kick"', source)
        self.assertIn("callback_allowed", source)

    def test_ai_response_is_bounded(self) -> None:
        source = self.source("AIAnswerDtg/AIAnswerDtg.py")
        self.assertIn("MAX_API_RESPONSE_BYTES", source)
        self.assertIn("resp.content.read(MAX_API_RESPONSE_BYTES + 1)", source)
        self.assertIn("except (TypeError, ValueError)", source)

    def test_notes_are_atomic_and_bounded(self) -> None:
        source = self.source("NoteDtg/NoteDtg.py")
        self.assertIn("MAX_NOTES = 500", source)
        self.assertIn("os.replace(temporary, self.config_path)", source)
        self.assertNotIn("except Exception:\n            pass", source)

    def test_avatar_rotation_has_safe_minimum_interval(self) -> None:
        source = self.source("AutoProfile/AutoProfile.py")
        self.assertIn("max(300, interval)", source)


if __name__ == "__main__":
    unittest.main()
