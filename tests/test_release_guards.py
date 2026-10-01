"""Small release checks that cannot use a real API or audio device."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyzer


class OfflineGuardTests(unittest.TestCase):
    def test_local_failure_never_calls_cloud_even_with_key(self):
        cfg = {"transcribe_engine": "local", "api_key": "not-a-real-key",
               "local_asr_model": "sense_voice"}
        progress = analyzer.AnalyzeProgress(None)
        with patch("local_asr.model_available", return_value=False), \
             patch.object(analyzer.api_client, "transcribe") as cloud:
            with self.assertRaisesRegex(RuntimeError, "没有上传"):
                analyzer._transcribe(Path("nonexistent.mp3"), cfg, progress)
            cloud.assert_not_called()

    def test_unknown_engine_never_calls_cloud(self):
        progress = analyzer.AnalyzeProgress(None)
        with patch.object(analyzer.api_client, "transcribe") as cloud:
            with self.assertRaisesRegex(ValueError, "未知"):
                analyzer._transcribe(Path("nonexistent.mp3"),
                                     {"transcribe_engine": "mystery"}, progress)
            cloud.assert_not_called()


if __name__ == "__main__":
    unittest.main()
