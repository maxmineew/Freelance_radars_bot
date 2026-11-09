import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from leadbot import classifier, codex_backend


class CodexTests(unittest.TestCase):
    def test_subscription_provider_excludes_api_fallback(self):
        with patch.object(classifier, "LLM_BACKEND", "codex_cli"), \
                patch.dict(os.environ, {"CODEX_MODEL": "test-model"}):
            self.assertEqual(classifier.provider_list(), [("codex_cli", "test-model")])

    def test_cli_is_isolated_and_removes_api_credentials(self):
        def run(cmd, **kw):
            self.assertNotIn("OPENAI_API_KEY", kw["env"])
            self.assertNotIn("CODEX_API_KEY", kw["env"])
            self.assertIn('forced_login_method="chatgpt"', cmd)
            self.assertIn("features.shell_tool=false", cmd)
            self.assertIn("--ignore-user-config", cmd)
            self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
            self.assertEqual(kw["input"], "system\n\nprompt")
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{"ok":true}', encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret"}), \
                patch.object(codex_backend.subprocess, "run", side_effect=run):
            self.assertEqual(json.loads(codex_backend.call("system", "prompt", "default")), {"ok": True})

    def test_login_and_quota_errors_are_retryable_and_redacted(self):
        for message, delay in [("401 authentication secret", 3600), ("429 quota secret", 900)]:
            with self.subTest(message=message), \
                    patch.object(codex_backend.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", message)):
                with self.assertRaises(classifier.CliUnavailable) as caught:
                    codex_backend.call("", "", "default")
                self.assertEqual(caught.exception.delay, delay)
                self.assertNotIn("secret", str(caught.exception))

    def test_single_dispatch_uses_codex(self):
        with patch.object(classifier, "LLM_BACKEND", "codex_cli"), \
                patch.object(codex_backend, "classify", return_value={"ok": True}) as run, \
                patch.object(classifier, "remaining_cooldown", return_value=0):
            self.assertEqual(classifier.classify("sample"), {"ok": True})
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
