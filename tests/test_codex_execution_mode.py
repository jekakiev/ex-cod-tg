from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from bot.codex_runner import CodexRunner, normalize_codex_sandbox_mode
from bot.config_store import write_env_file


def _install_dotenv_stub() -> None:
    if "dotenv" in sys.modules:
        return

    dotenv = types.ModuleType("dotenv")

    def dotenv_values(path: str | Path) -> dict[str, str]:
        result: dict[str, str] = {}
        raw_path = Path(path)
        if not raw_path.exists():
            return result
        for raw_line in raw_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"')
        return result

    dotenv.dotenv_values = dotenv_values
    sys.modules["dotenv"] = dotenv


_install_dotenv_stub()

from bot.config import AppConfig


class CodexExecutionModeTests(unittest.TestCase):
    def _make_runner(self, sandbox_mode: str) -> CodexRunner:
        return CodexRunner(
            AppConfig(
                telegram_bot_token="123456:ABCDEF_test_token",
                admin_ids=frozenset(),
                admin_labels={},
                workspaces_root=Path.home(),
                active_project_path=Path.home(),
                codex_bin="codex",
                codex_model="gpt-5.4",
                codex_selected_models=("gpt-5.4",),
                codex_thinking_level="high",
                codex_sandbox_mode=sandbox_mode,
                command_timeout_seconds=900,
                shell_timeout_seconds=120,
                git_timeout_seconds=120,
                max_output_chars=20000,
                telegram_max_images_per_request=10,
                telegram_image_max_bytes=20 * 1024 * 1024,
                config_file=Path("/tmp/config.env"),
                project_root=Path("/tmp/project"),
            )
        )

    def test_normalize_codex_sandbox_mode_aliases(self) -> None:
        self.assertEqual(normalize_codex_sandbox_mode("workspace"), "workspace-write")
        self.assertEqual(normalize_codex_sandbox_mode("danger"), "danger-full-access")
        self.assertEqual(normalize_codex_sandbox_mode("full-access"), "danger-full-access")

    def test_app_config_reads_codex_sandbox_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.env"
            write_env_file(
                config_path,
                {
                    "TELEGRAM_BOT_TOKEN": "123456:ABCDEF_test_token",
                    "WORKSPACES_ROOT": temp_dir,
                    "ACTIVE_PROJECT_PATH": temp_dir,
                    "CODEX_SANDBOX_MODE": "danger",
                },
            )

            config = AppConfig.from_file(config_path)

        self.assertEqual(config.codex_sandbox_mode, "danger-full-access")

    def test_runner_uses_full_access_args_for_danger_mode(self) -> None:
        runner = self._make_runner("danger-full-access")
        self.assertEqual(runner._codex_execution_args(), ["--dangerously-bypass-approvals-and-sandbox"])
        self.assertEqual(
            runner._codex_exec_base_args(),
            ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
        )
        self.assertEqual(
            runner._codex_resume_base_args(),
            ["codex", "exec", "resume", "--dangerously-bypass-approvals-and-sandbox"],
        )

    def test_runner_uses_full_auto_for_workspace_write(self) -> None:
        runner = self._make_runner("workspace-write")
        self.assertEqual(runner._codex_execution_args(), ["--full-auto"])
        self.assertEqual(runner._codex_exec_base_args(), ["codex", "exec", "--full-auto"])
        self.assertEqual(runner._codex_resume_base_args(), ["codex", "exec", "resume", "--full-auto"])


if __name__ == "__main__":
    unittest.main()
