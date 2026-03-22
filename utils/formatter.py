from __future__ import annotations

import html
from typing import Sequence

from bot.codex_runner import CommandResult, EnvironmentStatus


TELEGRAM_HARD_LIMIT = 4096
CODE_CHUNK_LIMIT = 3000


def format_start_message(*, user_id: int, status: EnvironmentStatus) -> str:
    setup_hints: list[str] = []
    if status.codex_path is None:
        setup_hints.append(
            "Install Codex CLI and make sure <code>codex --version</code> works for the same user."
        )
    if not status.working_dir_exists:
        setup_hints.append(
            "Open Settings and choose an active project under the configured workspaces root."
        )
    elif not status.git_repo:
        setup_hints.append(
            "Choose a git repository as the active project if you want /diff, /commit, and /log."
        )
    if not setup_hints:
        setup_hints.append("Ready. Send <code>/help</code> to see the available commands.")

    setup_steps = [f"{index}. {hint}" for index, hint in enumerate(setup_hints, start=1)]

    return (
        "<b>ex-cod-tg</b>\n"
        f"Telegram user ID: <code>{user_id}</code>\n"
        f"Active project: <code>{html.escape(str(status.working_dir))}</code>\n"
        f"Workspaces root: <code>{html.escape(str(status.workspaces_root))}</code>\n"
        f"Codex CLI: <code>{html.escape(status.codex_path or 'missing')}</code>\n"
        f"Git branch: <code>{html.escape(status.git_branch or ('not a git repo' if status.working_dir_exists else 'unavailable'))}</code>\n"
        f"Queue: <code>{html.escape(_queue_state(status))}</code>\n\n"
        "<b>Onboarding</b>\n"
        + "\n".join(setup_steps)
    )


def format_status_message(status: EnvironmentStatus) -> str:
    working_dir_state = "ready" if status.working_dir_exists else "missing"
    codex_state = status.codex_path or "missing"
    git_state = status.git_branch or ("not a git repo" if status.working_dir_exists else "unavailable")

    return (
        "<b>Local status</b>\n"
        f"Active project: <code>{html.escape(str(status.working_dir))}</code> ({working_dir_state})\n"
        f"Workspaces root: <code>{html.escape(str(status.workspaces_root))}</code>\n"
        f"Codex CLI: <code>{html.escape(codex_state)}</code>\n"
        f"Git branch: <code>{html.escape(git_state)}</code>\n"
        f"Queue: <code>{html.escape(_queue_state(status))}</code>"
    )


def format_command_results(
    *,
    title: str,
    named_results: Sequence[tuple[str, CommandResult]],
    max_output_chars: int,
) -> list[str]:
    sections: list[str] = []
    for label, result in named_results:
        sections.append(_render_result_block(label, result))

    payload, truncated = trim_output("\n\n".join(sections), max_output_chars=max_output_chars)
    if truncated:
        title = f"{title} (truncated)"
    return to_code_chunks(title, payload)


def trim_output(text: str, *, max_output_chars: int) -> tuple[str, bool]:
    normalized = _normalize_text(text)
    if len(normalized) <= max_output_chars:
        return normalized or "(no output)", False

    clipped = normalized[:max_output_chars].rsplit("\n", 1)[0].rstrip()
    if not clipped:
        clipped = normalized[:max_output_chars]
    return f"{clipped}\n\n[output truncated]", True


def to_code_chunks(title: str, text: str) -> list[str]:
    chunks = _split_for_telegram(_normalize_text(text), CODE_CHUNK_LIMIT)
    total = len(chunks)
    messages: list[str] = []

    for index, chunk in enumerate(chunks, start=1):
        suffix = f" ({index}/{total})" if total > 1 else ""
        heading = f"<b>{html.escape(title)}{suffix}</b>"
        messages.append(f"{heading}\n<pre><code>{html.escape(chunk)}</code></pre>")

    return messages


def _render_result_block(label: str, result: CommandResult) -> str:
    sections = [
        f"[{label}]",
        f"$ {result.command}",
        f"cwd: {result.cwd}",
        f"exit_code: {result.exit_code}",
        f"duration: {result.duration_seconds:.2f}s",
    ]

    if result.stdout.strip():
        sections.append(f"\nSTDOUT\n{result.stdout.strip()}")
    if result.stderr.strip():
        sections.append(f"\nSTDERR\n{result.stderr.strip()}")
    if not result.stdout.strip() and not result.stderr.strip():
        sections.append("\n(no output)")

    return "\n".join(sections).strip()


def _normalize_text(text: str) -> str:
    return text.replace("\x00", "").replace("\r\n", "\n").strip()


def _queue_state(status: EnvironmentStatus) -> str:
    if status.active_job:
        return f"busy: {status.active_job}; waiting={status.queued_jobs}"
    if status.queued_jobs:
        return f"idle worker; waiting={status.queued_jobs}"
    return "idle"


def _split_for_telegram(text: str, limit: int) -> list[str]:
    if not text:
        return ["(no output)"]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if _escaped_length(current + line) <= limit:
            current += line
            continue

        if current:
            chunks.append(current.rstrip("\n"))
            current = ""

        if _escaped_length(line) <= limit:
            current = line
            continue

        remainder = line
        while remainder:
            piece = _take_fitting_prefix(remainder, limit)
            chunks.append(piece.rstrip("\n"))
            remainder = remainder[len(piece):]

    if current:
        chunks.append(current.rstrip("\n"))

    return chunks or ["(no output)"]


def _take_fitting_prefix(text: str, limit: int) -> str:
    current = ""
    for character in text:
        if current and _escaped_length(current + character) > limit:
            return current
        current += character
    return current


def _escaped_length(text: str) -> int:
    return len(html.escape(text))
