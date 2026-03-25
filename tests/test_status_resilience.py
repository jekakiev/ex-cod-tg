from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.codex_runner import CodexAuthState, CodexRunner


def _install_aiogram_stub() -> None:
    if "aiogram" in sys.modules:
        return

    aiogram = types.ModuleType("aiogram")
    exceptions = types.ModuleType("aiogram.exceptions")
    filters = types.ModuleType("aiogram.filters")
    types_module = types.ModuleType("aiogram.types")
    utils = types.ModuleType("aiogram.utils")
    chat_action = types.ModuleType("aiogram.utils.chat_action")

    class _FilterExpr:
        def __getattr__(self, _name: str) -> "_FilterExpr":
            return self

        def __call__(self, *args: object, **kwargs: object) -> "_FilterExpr":
            return self

        def __eq__(self, _other: object) -> "_FilterExpr":
            return self

        def startswith(self, *_args: object, **_kwargs: object) -> "_FilterExpr":
            return self

        def in_(self, *_args: object, **_kwargs: object) -> "_FilterExpr":
            return self

    class _Router:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def message(self, *args: object, **kwargs: object):
            def _decorator(func):
                return func

            return _decorator

        def callback_query(self, *args: object, **kwargs: object):
            def _decorator(func):
                return func

            return _decorator

    class _Command:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _CommandStart(_Command):
        pass

    class _CommandObject:
        args: str | None = None

    class _TelegramBadRequest(Exception):
        pass

    class _TelegramRetryAfter(Exception):
        retry_after = 0

    class _InlineKeyboardButton:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _InlineKeyboardMarkup:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _Message:
        pass

    class _CallbackQuery:
        pass

    class _AsyncNullContext:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

    class _ChatActionSender:
        @classmethod
        def typing(cls, *args: object, **kwargs: object) -> _AsyncNullContext:
            return _AsyncNullContext()

    aiogram.F = _FilterExpr()
    aiogram.Router = _Router
    exceptions.TelegramBadRequest = _TelegramBadRequest
    exceptions.TelegramRetryAfter = _TelegramRetryAfter
    filters.Command = _Command
    filters.CommandObject = _CommandObject
    filters.CommandStart = _CommandStart
    types_module.CallbackQuery = _CallbackQuery
    types_module.InlineKeyboardButton = _InlineKeyboardButton
    types_module.InlineKeyboardMarkup = _InlineKeyboardMarkup
    types_module.Message = _Message
    chat_action.ChatActionSender = _ChatActionSender

    sys.modules["aiogram"] = aiogram
    sys.modules["aiogram.exceptions"] = exceptions
    sys.modules["aiogram.filters"] = filters
    sys.modules["aiogram.types"] = types_module
    sys.modules["aiogram.utils"] = utils
    sys.modules["aiogram.utils.chat_action"] = chat_action


_install_aiogram_stub()

from bot.handlers import AppContext, AsyncCommandQueue, _get_auth_state


class _FakeProcess:
    def __init__(self) -> None:
        self.calls = 0
        self.returncode = None

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(0.05)
        self.returncode = 0
        return b"", b""

    def kill(self) -> None:
        raise ProcessLookupError()


class StatusResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_process_ignores_process_lookup_error_after_timeout(self) -> None:
        runner = CodexRunner(
            SimpleNamespace(
                working_dir=Path.home(),
                working_dir_exists=True,
                codex_bin="codex",
            )
        )

        with patch("bot.codex_runner.asyncio.create_subprocess_exec", return_value=_FakeProcess()):
            result = await runner._run_process(
                ["sleep", "10"],
                cwd=Path.home(),
                timeout=0.01,
                log_command=False,
            )

        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -1)
        self.assertIn("Command timed out", result.stderr)

    async def test_auth_state_returns_fast_fallback_and_updates_cache_later(self) -> None:
        async def slow_auth_probe() -> CodexAuthState:
            await asyncio.sleep(0.05)
            return CodexAuthState(
                cli_path="/usr/bin/codex",
                cli_version="1.0.0",
                logged_in=True,
                auth_mode="chatgpt",
                auth_provider="openai",
                account_name="Yevhenii",
                account_email="yevhenii@example.com",
                status_summary="Logged in",
                raw_status="logged in",
            )

        app_context = AppContext(
            config=SimpleNamespace(
                workspaces_root=Path.home(),
                working_dir=Path.home(),
                working_dir_exists=True,
                codex_model="gpt-5.4",
                codex_thinking_level="medium",
            ),
            runner=SimpleNamespace(
                collect_codex_auth_state=slow_auth_probe,
                codex_path=lambda: "/usr/bin/codex",
                whisper_model_name=lambda: "tiny",
            ),
            queue=AsyncCommandQueue(),
            conversation_store=object(),
        )

        with patch("bot.handlers.STATUS_REFRESH_TIMEOUT_SECONDS", 0.01):
            fallback_state = await _get_auth_state(app_context)

        self.assertFalse(fallback_state.probe_ok)
        self.assertEqual(fallback_state.status_summary, "Status unavailable")
        self.assertEqual(
            fallback_state.raw_status,
            "Codex CLI status check is taking too long. Open Settings -> Codex CLI and tap Refresh.",
        )

        await asyncio.sleep(0.08)

        refreshed_state = await _get_auth_state(app_context)
        self.assertTrue(refreshed_state.probe_ok)
        self.assertTrue(refreshed_state.logged_in)
        self.assertEqual(refreshed_state.account_email, "yevhenii@example.com")


if __name__ == "__main__":
    unittest.main()
