from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Sequence


class SecurityError(ValueError):
    """Raised when a Telegram command violates the local safety policy."""


FORBIDDEN_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "<<", "&"}
FORBIDDEN_COMMANDS = {
    "rm",
    "sudo",
    "shutdown",
    "reboot",
    "kill",
    "pkill",
    "launchctl",
    "osascript",
    "open",
    "curl",
    "wget",
    "scp",
    "ssh",
    "python",
    "python3",
    "bash",
    "zsh",
    "sh",
    "chmod",
    "chown",
    "mv",
    "cp",
}
SAFE_SIMPLE_COMMANDS = {"pwd", "whoami"}
SAFE_PATH_COMMANDS = {"ls"}
SAFE_GIT_SUBCOMMANDS = {"status", "diff", "log", "branch", "rev-parse", "show"}
SAFE_FLAG_RE = re.compile(r"^-{1,2}[a-zA-Z0-9][\w-]*$")
SAFE_TOKEN_RE = re.compile(r"^[\w./:@+=,-]+$")


def is_admin(user_id: int | None, admin_ids: frozenset[int]) -> bool:
    return user_id is not None and user_id in admin_ids


def build_unauthorized_message(user_id: int | None) -> str:
    readable_id = "unknown" if user_id is None else str(user_id)
    return (
        "<b>Access denied</b>\n"
        f"Your Telegram user ID: <code>{readable_id}</code>\n\n"
        "Ask an existing admin to add you from the Admins screen, "
        "then send <code>/start</code> again."
    )


def validate_run_command(raw_command: str, working_dir: Path) -> list[str]:
    try:
        tokens = shlex.split(raw_command)
    except ValueError as exc:
        raise SecurityError(f"Invalid shell syntax: {exc}") from exc

    if not tokens:
        raise SecurityError("Command is empty.")

    for token in tokens:
        if token in FORBIDDEN_TOKENS:
            raise SecurityError(f"Shell operators are not allowed: {token}")

    command_name = tokens[0]
    if command_name in FORBIDDEN_COMMANDS:
        raise SecurityError(f"Command {command_name!r} is blocked.")

    if command_name in SAFE_SIMPLE_COMMANDS:
        if len(tokens) != 1:
            raise SecurityError(f"Command {command_name!r} does not accept arguments.")
        return tokens

    if command_name in SAFE_PATH_COMMANDS:
        _validate_path_command(tokens, working_dir)
        return tokens

    if command_name == "git":
        _validate_git_command(tokens)
        return tokens

    raise SecurityError(
        "Command is not on the safe allowlist. Use /help to see supported /run examples."
    )


def _validate_path_command(tokens: Sequence[str], working_dir: Path) -> None:
    for token in tokens[1:]:
        if token.startswith("-"):
            if not SAFE_FLAG_RE.match(token):
                raise SecurityError(f"Unsupported option: {token}")
            continue
        _ensure_safe_path(token, working_dir)


def _validate_git_command(tokens: Sequence[str]) -> None:
    if len(tokens) < 2:
        raise SecurityError("Git command must include a subcommand.")

    subcommand = tokens[1]
    if subcommand not in SAFE_GIT_SUBCOMMANDS:
        raise SecurityError(f"Git subcommand {subcommand!r} is not allowed.")

    for token in tokens[2:]:
        if token in FORBIDDEN_TOKENS:
            raise SecurityError(f"Shell operators are not allowed: {token}")
        if token.startswith("-"):
            if not SAFE_FLAG_RE.match(token):
                raise SecurityError(f"Unsupported git option: {token}")
            continue
        if token.startswith("/"):
            raise SecurityError("Absolute paths are not allowed in /run git commands.")
        if ".." in token:
            raise SecurityError("Parent directory traversal is not allowed.")
        if not SAFE_TOKEN_RE.match(token):
            raise SecurityError(f"Unsafe git argument: {token}")


def _ensure_safe_path(raw_path: str, working_dir: Path) -> None:
    if raw_path.startswith("~"):
        raise SecurityError("Home-directory shortcuts are not allowed.")
    if ".." in raw_path:
        raise SecurityError("Parent directory traversal is not allowed.")
    if not SAFE_TOKEN_RE.match(raw_path):
        raise SecurityError(f"Unsafe path argument: {raw_path}")

    candidate = Path(raw_path)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
    else:
        resolved = (working_dir / candidate).resolve(strict=False)

    try:
        resolved.relative_to(working_dir)
    except ValueError as exc:
        raise SecurityError("Path escapes the active project and is not allowed.") from exc
