from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from bot.app_paths import APP_NAME, AppPaths, get_app_paths
from bot.config_store import load_env_file, write_env_file
from bot.workspaces import choose_active_project, detect_workspaces_root


BANNER = r"""
███████╗██╗  ██╗      ██████╗ ██████╗ ██████╗       ████████╗ ██████╗
██╔════╝╚██╗██╔╝     ██╔════╝██╔═══██╗██╔══██╗         ██╔══╝██╔════╝
█████╗   ╚███╔╝█████╗██║     ██║   ██║██║  ██║         ██║   ██║  ███╗
██╔══╝   ██╔██╗╚════╝██║     ██║   ██║██║  ██║         ██║   ██║   ██║
███████╗██╔╝ ██╗     ╚██████╗╚██████╔╝██████╔╝         ██║   ╚██████╔╝
╚══════╝╚═╝  ╚═╝      ╚═════╝ ╚═════╝ ╚═════╝          ╚═╝    ╚═════╝
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive setup for ex-cod-tg.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run onboarding even if the config file already looks complete.",
    )
    args = parser.parse_args(argv)
    return ensure_configured(get_app_paths(), force=args.force)


def ensure_configured(paths: AppPaths, *, force: bool = False) -> int:
    values = load_env_file(paths.config_file)
    needs_setup = force or not config_is_complete(values)
    if not needs_setup:
        print(f"Configuration is ready: {paths.config_file}")
        return 0

    paths.config_dir.mkdir(parents=True, exist_ok=True)

    print(BANNER)
    print(f"{APP_NAME} setup")
    print(f"Config file: {paths.config_file}")
    print()
    print("Create your Telegram bot here: https://t.me/BotFather")
    print("Then paste the bot token below.")
    print()

    updated_values = prompt_for_config(values, default_working_dir=Path.cwd().resolve(strict=False))
    write_env_file(paths.config_file, updated_values)

    print()
    print(f"Saved configuration to {paths.config_file}")
    print(f"Detected workspaces root: {updated_values['WORKSPACES_ROOT']}")
    print("The first Telegram user who sends /start will become the first admin automatically.")
    return 0


def config_is_complete(values: dict[str, str]) -> bool:
    return bool(values.get("TELEGRAM_BOT_TOKEN", "").strip())


def prompt_for_config(existing: dict[str, str], *, default_working_dir: Path) -> dict[str, str]:
    default_root = existing.get("WORKSPACES_ROOT", "").strip()
    if default_root:
        workspaces_root = Path(default_root).expanduser().resolve(strict=False)
    else:
        workspaces_root = detect_workspaces_root(default_working_dir)

    active_project = choose_active_project(
        workspaces_root,
        Path(existing["ACTIVE_PROJECT_PATH"]) if existing.get("ACTIVE_PROJECT_PATH", "").strip() else default_working_dir,
    )
    values = {
        "TELEGRAM_BOT_TOKEN": "",
        "ADMIN_IDS": "",
        "ADMIN_LABELS": "",
        "WORKSPACES_ROOT": str(workspaces_root),
        "ACTIVE_PROJECT_PATH": str(active_project),
        "CODEX_BIN": existing.get("CODEX_BIN", "").strip() or "codex",
        "CODEX_MODEL": existing.get("CODEX_MODEL", "").strip() or "gpt-5.4",
        "CODEX_SELECTED_MODELS": existing.get("CODEX_SELECTED_MODELS", "").strip() or "gpt-5.4,gpt-5.4-mini",
        "CODEX_THINKING_LEVEL": existing.get("CODEX_THINKING_LEVEL", "").strip() or "high",
        "COMMAND_TIMEOUT_SECONDS": "900",
        "SHELL_TIMEOUT_SECONDS": "120",
        "GIT_TIMEOUT_SECONDS": "120",
        "MAX_OUTPUT_CHARS": "20000",
        **existing,
    }

    token = prompt(
        "Telegram bot token from BotFather",
        default=values["TELEGRAM_BOT_TOKEN"],
        required=True,
        validator=validate_and_describe_bot_token,
    )

    return {
        **values,
        "TELEGRAM_BOT_TOKEN": token,
        "WORKSPACES_ROOT": str(workspaces_root),
        "ACTIVE_PROJECT_PATH": str(active_project),
    }


def prompt(
    label: str,
    *,
    default: str,
    required: bool,
    validator: Callable[[str], str | None] | None = None,
    transform: Callable[[str], str] | None = None,
) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw_value = input(f"{label}{suffix}: ").strip()
        value = raw_value if raw_value else default
        if transform is not None:
            value = transform(value)
        if required and not value.strip():
            print("This value is required.")
            continue
        if validator is not None:
            error = validator(value)
            if error:
                print(error)
                continue
        return value


def validate_bot_token(value: str) -> str | None:
    if not value.strip():
        return "Telegram bot token is required."
    if ":" not in value:
        return "Bot token should look like 123456789:ABCDEF..."
    return None


def validate_and_describe_bot_token(value: str) -> str | None:
    format_error = validate_bot_token(value)
    if format_error:
        return format_error

    try:
        profile = fetch_bot_profile(value)
    except RuntimeError as exc:
        return str(exc)

    title = profile.get("first_name") or "Unknown bot"
    username = profile.get("username")
    print(f"Bot detected: {title}")
    if username:
        url = f"https://t.me/{username}"
        print(f"Bot link: {url}")
    else:
        print("Bot link unavailable: this bot has no public username yet.")
    return None


def fetch_bot_profile(token: str) -> dict[str, str]:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise RuntimeError("Telegram rejected this bot token.")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to reach Telegram right now: {exc.reason}")
    except json.JSONDecodeError:
        raise RuntimeError("Telegram returned an invalid response.")

    if not payload.get("ok"):
        description = payload.get("description") or "Telegram rejected this bot token."
        raise RuntimeError(str(description))

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Telegram returned an unexpected bot profile.")

    return {
        "first_name": str(result.get("first_name") or "").strip(),
        "username": str(result.get("username") or "").strip(),
    }
if __name__ == "__main__":
    sys.exit(main())
