from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.codex_runner import BotUpdateState, CodexAuthState, EnvironmentStatus, WhisperState, normalize_model_slug
from bot.version import APP_VERSION


@dataclass(slots=True)
class PendingAdminCandidate:
    user_id: int
    label: str


@dataclass(slots=True)
class DeviceAuthView:
    verification_url: str | None
    user_code: str | None
    raw_text: str | None = None


@dataclass(slots=True)
class PendingVoicePreview:
    preview_id: str
    text: str
    source_label: str


def model_label(value: str) -> str:
    normalized = normalize_model_slug(value)
    mapping = {
        "gpt-5.4": "5.4",
        "gpt-5.4-mini": "5.4 mini",
    }
    if normalized in mapping:
        return mapping[normalized]
    if normalized.startswith("gpt-"):
        return normalized.removeprefix("gpt-").replace("-", " ")
    return normalized


def thinking_label(value: str) -> str:
    mapping = {
        "low": "Fast",
        "medium": "Balanced",
        "high": "Detailed",
        "xhigh": "Very detailed",
    }
    return mapping.get(value, value)


def build_home_text(
    *,
    environment: EnvironmentStatus,
    update_state: BotUpdateState,
    active_project_name: str,
    has_active_project: bool,
    project_count: int,
    showing_repo_list: bool,
    showing_branch_list: bool,
    whisper_summary: str | None,
) -> str:
    latest_commit_line = environment.latest_commit_summary or ("not a git repo" if environment.git_repo is False else "unavailable")
    if environment.changed_files_count is None:
        diff_line = "unavailable"
    else:
        diff_line = f"{environment.changed_files_count} file(s)"
    lines = [
        f"<b>ex-cod {APP_VERSION}</b>\n\n"
        f"Project: <code>{html.escape(active_project_name)}</code>\n"
        f"Latest commit: <code>{html.escape(latest_commit_line)}</code>\n"
        f"Diff: <code>{html.escape(diff_line)}</code>\n\n"
        f"Bot updates: <code>{html.escape(update_state.status_summary)}</code>"
    ]
    if whisper_summary:
        lines.insert(-1, f"Whisper: <code>{html.escape(whisper_summary)}</code>\n")
    if update_state.update_available:
        lines.append('\n\n<blockquote>Update available. Open Settings to update the bot.</blockquote>')
    if showing_repo_list:
        lines.append("\nSelect the repository to use below.")
    elif showing_branch_list:
        lines.append("\nSelect the git branch to use below.")
    elif has_active_project:
        lines.append(
            "\n\n<blockquote>Send a message in this chat to start a Codex task in the active repo.</blockquote>"
        )
    else:
        lines.append(
            '\n\n<blockquote>To get started, choose a repository by tapping "All repos".</blockquote>'
        )
        lines.append(
            "\n<blockquote>After that, send a message in this chat and Codex will start working on your task.</blockquote>"
        )
    return "".join(lines)


def home_keyboard(
    *,
    project_names: list[str],
    active_index: int | None,
    showing_repo_list: bool,
    branch_names: list[str],
    active_branch_index: int | None,
    showing_branch_list: bool,
    current_model: str,
    current_thinking_level: str,
) -> InlineKeyboardMarkup:
    if showing_repo_list:
        rows: list[list[InlineKeyboardButton]] = []
        current_row: list[InlineKeyboardButton] = []
        for index, project_name in enumerate(project_names):
            current_row.append(
                InlineKeyboardButton(
                    text=_truncate_button_label(project_name, limit=24),
                    callback_data=f"repo:select:{index}",
                )
            )
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        rows.append([InlineKeyboardButton(text="Back", callback_data="nav:home")])
        return _keyboard(rows)

    if showing_branch_list:
        rows = []
        current_row = []
        for index, branch_name in enumerate(branch_names):
            current_row.append(
                InlineKeyboardButton(
                    text=_truncate_button_label(branch_name, limit=24),
                    callback_data=f"branch:select:{index}",
                )
            )
            if len(current_row) == 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)
        rows.append([InlineKeyboardButton(text="Back", callback_data="nav:home")])
        return _keyboard(rows)

    has_projects = bool(project_names) and active_index is not None
    left_callback = "repo:prev" if has_projects and len(project_names) > 1 else "repo:noop"
    right_callback = "repo:next" if has_projects and len(project_names) > 1 else "repo:noop"
    center_label = project_names[active_index] if has_projects else "Choose repo"
    has_branches = bool(branch_names) and active_branch_index is not None
    branch_left_callback = "branch:prev" if has_branches and len(branch_names) > 1 else "branch:noop"
    branch_right_callback = "branch:next" if has_branches and len(branch_names) > 1 else "branch:noop"
    if has_branches:
        branch_label = branch_names[active_branch_index]
    elif has_projects:
        branch_label = "No git repo"
    else:
        branch_label = "Choose branch"

    return _keyboard(
        [
            [
                InlineKeyboardButton(text="◀️", callback_data=left_callback),
                InlineKeyboardButton(
                    text=_truncate_button_label(center_label, limit=24),
                    callback_data="repo:list",
                ),
                InlineKeyboardButton(text="▶️", callback_data=right_callback),
            ],
            [
                InlineKeyboardButton(text="◀️", callback_data=branch_left_callback),
                InlineKeyboardButton(
                    text=_truncate_button_label(branch_label, limit=24),
                    callback_data="branch:list",
                ),
                InlineKeyboardButton(text="▶️", callback_data=branch_right_callback),
            ],
            [
                InlineKeyboardButton(text="All repos", callback_data="repo:list"),
                InlineKeyboardButton(text="All branches", callback_data="branch:list"),
            ],
            [
                InlineKeyboardButton(
                    text=f"Model: {model_label(current_model)}",
                    callback_data="quick:model",
                ),
                InlineKeyboardButton(
                    text=f"Thinking: {thinking_label(current_thinking_level)}",
                    callback_data="quick:thinking",
                ),
            ],
            [InlineKeyboardButton(text="Settings ⚙️", callback_data="nav:settings")],
        ]
    )
def build_settings_text(
    *,
    auth_state: CodexAuthState,
    whisper_state: WhisperState,
    update_state: BotUpdateState,
    workspaces_root: Path,
    whisper_progress_block: str | None,
    update_progress_block: str | None,
    flash_message: str | None = None,
) -> str:
    text = (
        "<b>Settings</b>\n\n"
        "Manage admin access, Codex CLI authorization, Whisper, bot updates, and the workspaces root.\n\n"
        f"Workspaces root: <code>{html.escape(str(workspaces_root))}</code>\n"
        f"Codex auth: <code>{html.escape(auth_state.account_summary or auth_state.status_summary)}</code>\n"
        f"Whisper: <code>{html.escape(whisper_state.summary)}</code>\n"
        f"Bot updates: <code>{html.escape(update_state.status_summary)}</code>"
    )
    if whisper_progress_block:
        text = f"{text}\n\n{whisper_progress_block}"
    if update_progress_block:
        text = f"{text}\n\n{update_progress_block}"
    if flash_message:
        text = f"{text}\n\n<blockquote>{html.escape(flash_message)}</blockquote>"
    return text


def settings_keyboard(
    *,
    whisper_state: WhisperState,
    whisper_busy: bool,
    update_busy: bool,
    update_confirm_pending: bool,
) -> InlineKeyboardMarkup:
    if update_confirm_pending:
        return _keyboard(
            [
                [
                    InlineKeyboardButton(text="✅ Confirm update", callback_data="update:confirm"),
                    InlineKeyboardButton(text="❌ Cancel", callback_data="update:cancel"),
                ],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")],
            ]
        )

    rows = [
        [
            InlineKeyboardButton(text="Admins", callback_data="nav:admins"),
            InlineKeyboardButton(text="Codex CLI", callback_data="nav:codex"),
        ],
        [
            InlineKeyboardButton(text="Selected models", callback_data="nav:selected_models"),
            InlineKeyboardButton(
                text=(
                    "⏳ Whisper…"
                    if whisper_busy
                    else "🗑 Delete Whisper"
                    if whisper_state.installed
                    else "⬇️ Install Whisper"
                ),
                callback_data=(
                    "whisper:noop"
                    if whisper_busy
                    else "whisper:delete"
                    if whisper_state.installed
                    else "whisper:install"
                ),
            ),
        ],
    ]
    if update_busy:
        rows.append(
            [
                InlineKeyboardButton(text="⏳ Updating bot…", callback_data="update:noop"),
                InlineKeyboardButton(text="Workspaces Root", callback_data="nav:workspaces_root"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(text="⬆️ Update bot", callback_data="update:run"),
                InlineKeyboardButton(text="Workspaces Root", callback_data="nav:workspaces_root"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:home")])
    return _keyboard(rows)


def build_selected_models_text(*, selected_models: list[str]) -> str:
    selected_text = ", ".join(model_label(model) for model in selected_models) or "None"
    return "\n".join(
        [
            "<b>Selected models</b>",
            "",
            "Choose which models should be used in quick model switching.",
            f"Current selection: <code>{html.escape(selected_text)}</code>",
        ]
    )


def selected_models_keyboard(*, available_models: list[str], selected_models: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    selected_set = set(selected_models)
    current_row: list[InlineKeyboardButton] = []
    for model in available_models:
        prefix = "✅" if model in selected_set else "⬜️"
        current_row.append(
            InlineKeyboardButton(
                text=f"{prefix} {model_label(model)}",
                callback_data=f"selected_models:toggle:{model}",
            )
        )
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:settings")])
    return _keyboard(rows)


def build_workspaces_root_text(
    *,
    workspaces_root: Path,
    active_project: Path,
    repo_names: list[str],
    waiting_for_path: bool,
) -> str:
    lines = [
        "<b>Workspaces Root</b>",
        "",
        f"Current root: <code>{html.escape(str(workspaces_root))}</code>",
        f"Active project: <code>{html.escape(active_project.name or str(active_project))}</code>",
        f"Repos found: <code>{len(repo_names)}</code>",
    ]
    if waiting_for_path:
        lines.extend(
            [
                "",
                "<b>Waiting for a new path</b>",
                "Send an absolute folder path in this chat.",
            ]
        )
    return "\n".join(lines)


def workspaces_root_keyboard(*, waiting_for_path: bool) -> InlineKeyboardMarkup:
    if waiting_for_path:
        return _keyboard(
            [
                [InlineKeyboardButton(text="Cancel", callback_data="root:cancel")],
                [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:settings")],
            ]
        )
    return _keyboard(
        [
            [InlineKeyboardButton(text="Change root", callback_data="root:change")],
            [InlineKeyboardButton(text="⬅️ Back", callback_data="nav:settings")],
        ]
    )


def build_admins_text(
    *,
    admin_items: list[tuple[int, str]],
    candidate: PendingAdminCandidate | None,
    waiting_for_candidate: bool,
    can_remove_admins: bool,
) -> str:
    lines = ["<b>Admins</b>", ""]
    if admin_items:
        lines.append("Current admins:")
        for _, label in admin_items:
            lines.append(f"• <code>{html.escape(label)}</code>")
    else:
        lines.append("Admin list is empty.")

    lines.append("")
    if candidate is not None:
        lines.append("<b>Confirm admin access</b>")
        lines.append(f"Candidate: <code>{html.escape(candidate.label)}</code>")
        lines.append("Confirm or cancel the admin access request below.")
    elif waiting_for_candidate:
        lines.append("<b>Waiting for a new admin</b>")
        lines.append("Ask the other person to send any message to this bot.")
        lines.append("After that, confirm their access here.")
    else:
        lines.append("Press Add to start waiting for a new admin.")
        if not can_remove_admins:
            lines.append("At least one admin must always remain.")

    return "\n".join(lines)


def admins_keyboard(
    *,
    admin_items: list[tuple[int, str]],
    candidate: PendingAdminCandidate | None,
    waiting_for_candidate: bool,
    can_remove_admins: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if candidate is not None:
        rows.append(
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="admin:confirm"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="admin:cancel"),
            ]
        )
    elif waiting_for_candidate:
        rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="admin:cancel")])
    else:
        if can_remove_admins:
            for admin_id, label in admin_items:
                rows.append(
                    [
                        InlineKeyboardButton(text=f"Remove {label}", callback_data=f"admin:remove:{admin_id}")
                    ]
                )
        rows.append([InlineKeyboardButton(text="➕ Add", callback_data="admin:add")])

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:settings")])
    return _keyboard(rows)


def build_codex_text(
    *,
    auth_state: CodexAuthState,
    device_auth: DeviceAuthView | None,
    login_active: bool,
) -> str:
    lines = [
        "<b>Codex CLI</b>",
        "",
        f"CLI: <code>{html.escape(auth_state.cli_version or 'missing')}</code>",
        f"Status: <code>{html.escape(auth_state.status_summary)}</code>",
    ]

    if auth_state.account_name or auth_state.account_email:
        lines.append(
            f"Account: <code>{html.escape(auth_state.account_name or 'unknown')}</code>"
        )
        if auth_state.account_email:
            lines.append(f"Email: <code>{html.escape(auth_state.account_email)}</code>")

    if login_active:
        lines.extend(
            [
                "",
                "<b>Authorization in progress</b>",
                "1. Copy this code:",
            ]
        )
        if device_auth and device_auth.user_code:
            lines.append(f"<pre><code>{html.escape(device_auth.user_code)}</code></pre>")
        lines.extend(
            [
                "2. Open this link and paste the copied code after authorization:",
            ]
        )
    elif device_auth and device_auth.raw_text:
        lines.extend(["", "<b>Last login flow</b>"])

    if device_auth and device_auth.verification_url:
        lines.append(f'<a href="{html.escape(device_auth.verification_url)}">{html.escape(device_auth.verification_url)}</a>')

    if device_auth and device_auth.raw_text and not (device_auth.verification_url or device_auth.user_code):
        lines.append("")
        lines.append("<b>Details</b>")
        lines.append(f"<pre><code>{html.escape(device_auth.raw_text)}</code></pre>")

    return "\n".join(lines)


def codex_keyboard(*, auth_state: CodexAuthState, login_active: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if login_active:
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="codex:refresh")])
        rows.append([InlineKeyboardButton(text="❌ Cancel login", callback_data="codex:cancel_login")])
    elif auth_state.logged_in:
        rows.append(
            [
                InlineKeyboardButton(text="🚪 Log out", callback_data="codex:logout"),
                InlineKeyboardButton(text="🔄 Refresh", callback_data="codex:refresh"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton(text="🔐 Log in", callback_data="codex:login")])
        rows.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="codex:refresh")])

    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="nav:settings")])
    return _keyboard(rows)


def build_voice_preview_text(*, preview: PendingVoicePreview) -> str:
    preview_text = preview.text.strip()
    if len(preview_text) > 3200:
        preview_text = f"{preview_text[:3200].rstrip()}\n\n[truncated]"
    return "\n".join(
        [
            "<b>Voice transcription</b>",
            "",
            f"<pre><code>{html.escape(preview_text)}</code></pre>",
            "",
            "You can copy this text by tapping it, fix any mistakes if needed, and just send that message.",
        ]
    )


def voice_preview_keyboard(preview_id: str) -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                InlineKeyboardButton(text="✅ Approve", callback_data=f"voice:approve:{preview_id}"),
                InlineKeyboardButton(text="❌ Cancel", callback_data=f"voice:cancel:{preview_id}"),
            ]
        ]
    )


def response_controls_keyboard(*, current_model: str, current_thinking_level: str) -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                InlineKeyboardButton(
                    text=f"Model: {model_label(current_model)}",
                    callback_data="quick:model",
                ),
                InlineKeyboardButton(
                    text=f"Thinking: {thinking_label(current_thinking_level)}",
                    callback_data="quick:thinking",
                ),
            ]
        ]
    )


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _truncate_button_label(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: max(1, limit - 1)]}…"
