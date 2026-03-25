from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.chat_action import ChatActionSender

from bot.codex_runner import (
    DEFAULT_CODEX_SANDBOX_MODE,
    DEFAULT_REASONING_LEVELS,
    DEFAULT_SELECTED_MODELS,
    SUPPORTED_CODEX_SANDBOX_MODES,
    AsyncCommandQueue,
    BotUpdateState,
    CodexAuthState,
    CodexModelInfo,
    CodexRunner,
    EnvironmentStatus,
    GitHubAuthState,
    WhisperState,
    normalize_codex_sandbox_mode,
    normalize_model_slug,
)
from bot.conversation_store import BranchConversationState, BranchConversationStore, ConversationSummary, NO_GIT_BRANCH_KEY
from bot.config_store import (
    add_admin_id,
    remove_admin_id,
    save_admin_label,
    update_codex_preferences,
    update_workspace_settings,
)
from bot.update_notice_store import UpdateNotice, save_update_notice
from bot.security import (
    SecurityError,
    build_unauthorized_message,
    is_admin,
    validate_run_command,
)
from bot.ui import (
    DeviceAuthView,
    PendingAdminCandidate,
    PendingVoicePreview,
    admins_keyboard,
    build_admins_text,
    build_codex_text,
    build_execution_mode_text,
    build_github_text,
    build_home_text,
    build_selected_models_text,
    build_settings_text,
    build_voice_preview_text,
    build_workspaces_root_text,
    codex_keyboard,
    execution_mode_keyboard,
    github_keyboard,
    home_keyboard,
    model_label,
    response_controls_keyboard,
    selected_models_keyboard,
    settings_keyboard,
    thinking_label,
    voice_preview_keyboard,
    workspaces_root_keyboard,
)
from bot.workspaces import WorkspaceProject, choose_active_project, project_name, scan_workspace_projects
from utils.formatter import format_command_results

if TYPE_CHECKING:
    from aiogram import Bot

    from bot.config import AppConfig


logger = logging.getLogger(__name__)
router = Router(name=__name__)
UPDATE_CHECK_TTL_SECONDS = 120
STATUS_CACHE_TTL_SECONDS = 5
STATUS_ERROR_CACHE_TTL_SECONDS = 60
MODEL_CATALOG_CACHE_TTL_SECONDS = 60
STATUS_REFRESH_TIMEOUT_SECONDS = 2.5
UPDATE_REFRESH_TIMEOUT_SECONDS = 4.0
UPDATE_ERROR_CACHE_TTL_SECONDS = 300
STREAM_UPDATE_INTERVAL_SECONDS = 0.25
STREAM_KEEPALIVE_INTERVAL_SECONDS = 1.2
STREAM_DRAFT_RETRY_GRACE_SECONDS = 0.2


@dataclass(slots=True)
class DashboardSession:
    chat_id: int
    user_id: int
    message_id: int
    page: str = "home"


@dataclass(slots=True)
class PendingAdminRequest:
    inviter_id: int
    inviter_chat_id: int
    inviter_message_id: int | None = None
    candidate_id: int | None = None
    candidate_label: str | None = None


@dataclass(slots=True)
class PendingWorkspacesRootRequest:
    requester_id: int
    requester_chat_id: int


@dataclass(slots=True)
class PendingGitHubTokenRequest:
    requester_id: int
    requester_chat_id: int


@dataclass(slots=True)
class CodexLoginSession:
    owner_user_id: int
    owner_chat_id: int
    process: asyncio.subprocess.Process
    output_lines: list[str] = field(default_factory=list)
    completed: bool = False
    returncode: int | None = None
    monitor_task: asyncio.Task[None] | None = None

    def render_output(self) -> str | None:
        text = "".join(self.output_lines).strip()
        if not text:
            return None
        return text[-2500:]


@dataclass(slots=True)
class GitHubLoginSession:
    owner_user_id: int
    owner_chat_id: int
    process: asyncio.subprocess.Process
    output_lines: list[str] = field(default_factory=list)
    completed: bool = False
    returncode: int | None = None
    monitor_task: asyncio.Task[None] | None = None

    def render_output(self) -> str | None:
        text = "".join(self.output_lines).strip()
        if not text:
            return None
        return text[-2500:]


@dataclass(slots=True)
class PendingVoiceRequest:
    preview_id: str
    owner_user_id: int
    owner_chat_id: int
    prompt_text: str
    source_message_id: int
    preview_message_id: int | None = None
    language: str | None = None


@dataclass(slots=True)
class PendingImageAttachment:
    temp_path: Path
    original_name: str
    size_bytes: int


@dataclass(slots=True)
class PendingImageRequest:
    owner_user_id: int
    owner_chat_id: int
    attachments: list[PendingImageAttachment] = field(default_factory=list)


@dataclass(slots=True)
class PendingResetRequest:
    owner_user_id: int
    owner_chat_id: int
    repo_path: Path
    branch_name: str
    branch_label: str


@dataclass(slots=True)
class BranchExecutionContext:
    repo_path: Path
    branch_name: str
    branch_label: str
    head_sha: str | None
    git_repo: bool
    saved_state: BranchConversationState | None


@dataclass(slots=True)
class BotUpdateProgress:
    awaiting_confirmation: bool = False
    in_progress: bool = False
    percent: int = 0
    status_text: str = ""
    target_version: str | None = None
    latest_summary: str | None = None
    latest_notes: list[str] = field(default_factory=list)
    old_commit: str | None = None
    new_commit: str | None = None
    error_text: str | None = None


@dataclass(slots=True)
class WhisperProgress:
    in_progress: bool = False
    percent: int = 0
    status_text: str = ""
    error_text: str | None = None


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    runner: CodexRunner
    queue: AsyncCommandQueue
    conversation_store: BranchConversationStore
    admin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ui_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dashboards: dict[int, DashboardSession] = field(default_factory=dict)
    pending_admin_request: PendingAdminRequest | None = None
    pending_workspaces_root_request: PendingWorkspacesRootRequest | None = None
    pending_github_token_request: PendingGitHubTokenRequest | None = None
    pending_reset_request: PendingResetRequest | None = None
    pending_voice_requests: dict[str, PendingVoiceRequest] = field(default_factory=dict)
    pending_image_requests: dict[int, PendingImageRequest] = field(default_factory=dict)
    codex_login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    codex_login_session: CodexLoginSession | None = None
    github_login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    github_login_session: GitHubLoginSession | None = None
    flash_message: str | None = None
    cached_update_state: BotUpdateState | None = None
    update_checked_at: float = 0.0
    update_refresh_task: asyncio.Task[BotUpdateState] | None = None
    cached_environment_status: EnvironmentStatus | None = None
    environment_checked_at: float = 0.0
    environment_refresh_task: asyncio.Task[EnvironmentStatus] | None = None
    cached_auth_state: CodexAuthState | None = None
    auth_checked_at: float = 0.0
    auth_refresh_task: asyncio.Task[CodexAuthState] | None = None
    cached_github_state: GitHubAuthState | None = None
    github_checked_at: float = 0.0
    github_refresh_task: asyncio.Task[GitHubAuthState] | None = None
    cached_whisper_state: WhisperState | None = None
    whisper_checked_at: float = 0.0
    whisper_refresh_task: asyncio.Task[WhisperState] | None = None
    cached_model_catalog: list[CodexModelInfo] | None = None
    model_catalog_checked_at: float = 0.0
    update_progress: BotUpdateProgress | None = None
    update_task: asyncio.Task[None] | None = None
    whisper_progress: WhisperProgress | None = None
    whisper_task: asyncio.Task[None] | None = None


HELP_TEXT = """
<b>Available commands</b>
<code>/start</code> Open a fresh main menu message
<code>/help</code> Show this help message
<code>/reset_context</code> Reset the saved Codex context for the current repo and git branch
<code>&lt;plain message&gt;</code> Chat with Codex in the active repository
<code>&lt;photo/image file&gt;</code> Send with a caption, or send text next to use it as input for Codex
<code>&lt;voice message&gt;</code> Transcribe locally with Whisper, then approve before running Codex
<code>/run &lt;safe command&gt;</code> Run a restricted shell command
<code>/diff</code> Show the current git diff
<code>/log</code> Show the latest git commits

<b>Tip</b>
You do not need a slash command for most Codex tasks.
If you want Codex to fix, refactor, explain, or commit something, just send it as a normal chat message.

<b>Safe /run examples</b>
<code>/run pwd</code>
<code>/run ls -la</code>
<code>/run ls src</code>
<code>/run git status --short</code>
<code>/run git diff --stat</code>
<code>/run git log --oneline -n 5</code>
""".strip()


@router.message(CommandStart())
async def start_command(message: Message, app_context: AppContext) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        await message.answer(build_unauthorized_message(user_id))
        return

    bootstrapped = await _bootstrap_first_admin_if_needed(app_context, user_id, _format_user_label(message))
    if bootstrapped:
        await message.answer(
            "<b>Owner access granted</b>\n"
            "No admins were configured, so you were registered as the first admin."
        )

    if await _capture_pending_admin_candidate(message, app_context, user_id):
        return

    if not await _ensure_authorized_message(message, app_context):
        return

    await _sync_admin_label_from_message(message, app_context)

    await _open_fresh_dashboard_from_message(message, app_context, page="home")


@router.message(Command("help"))
async def help_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("reset_context"))
async def reset_context_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    if message.from_user is None:
        return

    context = await _current_branch_execution_context(app_context)
    app_context.pending_reset_request = PendingResetRequest(
        owner_user_id=message.from_user.id,
        owner_chat_id=message.chat.id,
        repo_path=context.repo_path,
        branch_name=context.branch_name,
        branch_label=context.branch_label,
    )
    await message.answer(
        (
            "<b>Reset Codex context?</b>\n\n"
            f"Repo: <code>{html.escape(context.repo_path.name or str(context.repo_path))}</code>\n"
            f"Branch: <code>{html.escape(context.branch_label)}</code>\n\n"
            "This clears the saved session id and short branch summary. "
            "Your next message will start a fresh Codex session from the current HEAD."
        ),
        reply_markup=_reset_context_keyboard(),
    )


@router.callback_query(F.data == "reset_context:confirm")
async def reset_context_confirm_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    pending = app_context.pending_reset_request
    if pending is None:
        await query.answer("This reset request is no longer active.", show_alert=True)
        return
    if query.from_user is None or query.from_user.id != pending.owner_user_id:
        await query.answer("Only the original requester can confirm this reset.", show_alert=True)
        return

    app_context.pending_reset_request = None
    removed = app_context.conversation_store.clear(pending.repo_path, pending.branch_name)
    await query.answer("Context reset." if removed else "No saved context was found.", show_alert=False)
    if query.message is not None:
        await _edit_streaming_message(
            query.message,
            (
                "<b>Codex context reset</b>\n\n"
                f"Repo: <code>{html.escape(pending.repo_path.name or str(pending.repo_path))}</code>\n"
                f"Branch: <code>{html.escape(pending.branch_label)}</code>\n\n"
                "The next message will start a fresh Codex session."
            ),
        )


@router.callback_query(F.data == "reset_context:cancel")
async def reset_context_cancel_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    pending = app_context.pending_reset_request
    if pending is None:
        await query.answer("Nothing to cancel.", show_alert=True)
        return
    if query.from_user is None or query.from_user.id != pending.owner_user_id:
        await query.answer("Only the original requester can cancel this reset.", show_alert=True)
        return

    app_context.pending_reset_request = None
    await query.answer("Reset cancelled.", show_alert=False)
    if query.message is not None:
        await _edit_streaming_message(query.message, "Context reset cancelled.")

@router.message(Command("run"))
async def run_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    raw_command = (command.args or "").strip()
    if not raw_command:
        await message.answer("Usage: <code>/run git status --short</code>")
        return

    try:
        safe_args = validate_run_command(raw_command, app_context.config.working_dir)
    except SecurityError as exc:
        logger.warning("Rejected /run from %s: %s", message.from_user.id if message.from_user else None, exc)
        await message.answer(f"<b>Rejected</b>\n{exc}")
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /run from %s: %s", user_id, raw_command)

    result = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/run by {user_id}",
        task_factory=lambda: app_context.runner.run_shell_command(safe_args),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Shell /run",
            named_results=[("shell", result)],
            max_output_chars=app_context.config.max_output_chars,
        ),
    )


@router.message(Command("diff"))
async def diff_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /diff from %s", user_id)

    result = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/diff by {user_id}",
        task_factory=lambda: app_context.runner.run_git_command(
            ["diff", "--stat", "--patch", "--no-color"],
            timeout=app_context.config.command_timeout_seconds,
        ),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Git /diff",
            named_results=[("git diff", result)],
            max_output_chars=app_context.config.max_output_chars,
        ),
    )


@router.message(Command("log"))
async def log_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /log from %s", user_id)

    result = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/log by {user_id}",
        task_factory=lambda: app_context.runner.run_git_command(
            ["log", "--oneline", "--decorate", "-n", "10"],
            timeout=app_context.config.git_timeout_seconds,
        ),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Git /log",
            named_results=[("git log", result)],
            max_output_chars=app_context.config.max_output_chars,
        ),
    )


@router.callback_query(F.data.startswith("nav:"))
async def navigation_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    if app_context.update_progress and app_context.update_progress.in_progress:
        await query.answer("Wait until the bot update finishes.", show_alert=True)
        return

    page = query.data.split(":", 1)[1]
    if page == "main_menu":
        await query.answer()
        await send_fresh_dashboard_for_chat(
            bot=query.bot,
            app_context=app_context,
            chat_id=query.message.chat.id,
            user_id=query.from_user.id if query.from_user else query.message.chat.id,
            page="home",
        )
        return
    pending_github_token = app_context.pending_github_token_request
    if (
        pending_github_token is not None
        and query.from_user is not None
        and query.message is not None
        and pending_github_token.requester_id == query.from_user.id
        and pending_github_token.requester_chat_id == query.message.chat.id
        and page != "github"
    ):
        app_context.pending_github_token_request = None
    if page in {"model", "models", "thinking"}:
        page = "home"
    elif page == "whisper":
        page = "settings"
    await query.answer()
    await _show_dashboard_from_callback(query, app_context, page=page)


@router.callback_query(F.data == "repo:noop")
async def repo_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "repo:list")
async def repo_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()
    await _show_dashboard_from_callback(query, app_context, page="repos")


@router.callback_query(F.data == "branch:list")
async def branch_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()
    await _show_dashboard_from_callback(query, app_context, page="branches")


@router.callback_query(F.data == "codexmodel:list")
async def legacy_codexmodel_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    await quick_model_callback(query, app_context)


@router.callback_query(F.data == "thinking:list")
async def legacy_thinking_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    await quick_thinking_callback(query, app_context)


@router.callback_query(F.data.in_({"repo:prev", "repo:next"}))
async def repo_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    direction = -1 if query.data == "repo:prev" else 1
    projects = await _projects_with_active_selection(app_context)
    if len(projects) < 2:
        await query.answer()
        return

    current_index = _active_project_index(app_context, projects) or 0
    next_index = (current_index + direction) % len(projects)
    await _set_active_project(app_context, projects[next_index].path)
    await _show_dashboard_from_callback(query, app_context, page="home")
    await query.answer()


@router.callback_query(F.data.startswith("repo:select:"))
async def repo_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid repository.", show_alert=True)
        return

    projects = await _projects_with_active_selection(app_context)
    index = int(raw_index)
    if index < 0 or index >= len(projects):
        await query.answer("Repository list is out of date.", show_alert=True)
        return

    await _set_active_project(app_context, projects[index].path)
    await _show_dashboard_from_callback(query, app_context, page="home")
    await query.answer(f"Active repo: {projects[index].name}")


@router.callback_query(F.data.in_({"branch:noop", "branch:list:noop"}))
async def branch_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data.in_({"branch:prev", "branch:next"}))
async def branch_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    branches, current_branch = await _git_branches_for_active_project(app_context)
    if len(branches) < 2 or current_branch is None:
        await query.answer()
        return

    current_index = branches.index(current_branch) if current_branch in branches else 0
    direction = -1 if query.data == "branch:prev" else 1
    next_branch = branches[(current_index + direction) % len(branches)]
    result = await _checkout_branch(app_context, next_branch)
    await _show_dashboard_from_callback(query, app_context, page="home")
    if result.ok:
        await query.answer(f"Active branch: {next_branch}")
    else:
        await query.answer((result.stderr or result.stdout or "Branch switch failed.")[:180], show_alert=True)


@router.callback_query(F.data.in_({"codexmodel:prev", "codexmodel:next"}))
async def legacy_codexmodel_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    models = _effective_selected_models(app_context)
    if len(models) < 2:
        await query.answer()
        return

    current_index = models.index(app_context.config.codex_model) if app_context.config.codex_model in models else 0
    direction = -1 if query.data == "codexmodel:prev" else 1
    next_model = models[(current_index + direction) % len(models)]
    await _set_codex_preferences(
        app_context,
        codex_model=next_model,
        thinking_level=_effective_thinking_level(app_context, next_model),
    )
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Model: {model_label(next_model)}")


@router.callback_query(F.data.in_({"thinking:prev", "thinking:next"}))
async def legacy_thinking_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    levels = _thinking_levels_for_model(app_context, app_context.config.codex_model)
    if len(levels) < 2:
        await query.answer()
        return

    current_level = _effective_thinking_level(app_context, app_context.config.codex_model)
    current_index = levels.index(current_level) if current_level in levels else 0
    direction = -1 if query.data == "thinking:prev" else 1
    next_level = levels[(current_index + direction) % len(levels)]
    await _set_codex_preferences(app_context, thinking_level=next_level)
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Thinking: {thinking_label(next_level)}")


@router.callback_query(F.data.startswith("branch:select:"))
async def branch_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid branch.", show_alert=True)
        return

    branches, _ = await _git_branches_for_active_project(app_context)
    index = int(raw_index)
    if index < 0 or index >= len(branches):
        await query.answer("Branch list is out of date.", show_alert=True)
        return

    target_branch = branches[index]
    result = await _checkout_branch(app_context, target_branch)
    await _show_dashboard_from_callback(query, app_context, page="home")
    if result.ok:
        await query.answer(f"Active branch: {target_branch}")
    else:
        await query.answer((result.stderr or result.stdout or "Branch switch failed.")[:180], show_alert=True)


@router.callback_query(F.data.startswith("codexmodel:select:"))
async def legacy_codexmodel_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid model.", show_alert=True)
        return

    models = _effective_selected_models(app_context)
    index = int(raw_index)
    if index < 0 or index >= len(models):
        await query.answer("Model list is out of date.", show_alert=True)
        return

    target_model = models[index]
    await _set_codex_preferences(
        app_context,
        codex_model=target_model,
        thinking_level=_effective_thinking_level(app_context, target_model),
    )
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Model: {model_label(target_model)}")


@router.callback_query(F.data.startswith("thinking:select:"))
async def legacy_thinking_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid thinking level.", show_alert=True)
        return

    levels = _thinking_levels_for_model(app_context, app_context.config.codex_model)
    index = int(raw_index)
    if index < 0 or index >= len(levels):
        await query.answer("Thinking list is out of date.", show_alert=True)
        return

    target_level = levels[index]
    await _set_codex_preferences(app_context, thinking_level=target_level)
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Thinking: {thinking_label(target_level)}")


@router.callback_query(F.data == "quick:model")
async def quick_model_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    next_model = _cycle_current_model(app_context)
    await _set_codex_preferences(
        app_context,
        codex_model=next_model,
        thinking_level=_effective_thinking_level(app_context, next_model),
    )
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Model: {model_label(next_model)}")


@router.callback_query(F.data == "quick:thinking")
async def quick_thinking_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    next_level = _cycle_thinking_level(app_context)
    await _set_codex_preferences(app_context, thinking_level=next_level)
    await _refresh_quick_controls_target(query, app_context)
    await query.answer(f"Thinking: {thinking_label(next_level)}")


@router.callback_query(F.data.startswith("selected_models:toggle:"))
async def selected_models_toggle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    target_model = normalize_model_slug(query.data.rsplit(":", 1)[1])
    if target_model not in set(_available_model_slugs(app_context)):
        await query.answer("Unknown model.", show_alert=True)
        return

    selected = _effective_selected_models(app_context)
    if target_model in selected:
        if len(selected) == 1:
            await query.answer("At least one model must stay selected.", show_alert=True)
            return
        selected = [model for model in selected if model != target_model]
    else:
        selected = [*selected, target_model]

    current_model = app_context.config.codex_model
    next_current_model = current_model if current_model in selected else selected[0]
    await _set_codex_preferences(
        app_context,
        codex_model=next_current_model,
        selected_models=selected,
        thinking_level=_effective_thinking_level(app_context, next_current_model),
    )
    await _show_dashboard_from_callback(query, app_context, page="selected_models")
    await query.answer("Selected models updated.")


@router.callback_query(F.data.startswith("execution_mode:set:"))
async def execution_mode_set_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    target_mode = normalize_codex_sandbox_mode(query.data.rsplit(":", 1)[1])
    if target_mode not in SUPPORTED_CODEX_SANDBOX_MODES:
        await query.answer("Unknown execution mode.", show_alert=True)
        return
    if target_mode == app_context.config.codex_sandbox_mode:
        await query.answer("Execution mode already set.")
        return

    await _set_codex_preferences(app_context, sandbox_mode=target_mode)
    await _show_dashboard_from_callback(query, app_context, page="execution_mode")
    if target_mode == "danger-full-access":
        await query.answer("Full access enabled. Codex can now commit and push.", show_alert=True)
    else:
        await query.answer("Workspace write mode enabled.")


@router.callback_query(F.data == "admin:add")
async def admin_add_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    if query.from_user is None:
        await query.answer("Unable to identify user.", show_alert=True)
        return

    app_context.pending_admin_request = PendingAdminRequest(
        inviter_id=query.from_user.id,
        inviter_chat_id=query.message.chat.id,
        inviter_message_id=query.message.message_id if query.message else None,
    )
    await _show_dashboard_from_callback(query, app_context, page="admins")
    await query.answer("Waiting for a new admin.")


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    app_context.pending_admin_request = None
    await _show_dashboard_from_callback(query, app_context, page="admins")
    await query.answer("Admin enrollment cancelled.")


@router.callback_query(F.data == "admin:confirm")
async def admin_confirm_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    pending = app_context.pending_admin_request
    if pending is None or pending.candidate_id is None:
        await query.answer("No candidate to confirm.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="admins")
        return

    async with app_context.admin_lock:
        updated_admin_ids = add_admin_id(
            app_context.config.config_file,
            pending.candidate_id,
            pending.candidate_label,
        )
        _refresh_admin_ids(app_context, updated_admin_ids)
        _refresh_admin_labels(app_context, pending.candidate_id, pending.candidate_label or str(pending.candidate_id))

    app_context.pending_admin_request = None

    await _show_dashboard_from_callback(query, app_context, page="admins")
    await query.answer("Admin added.")


@router.callback_query(F.data.startswith("admin:remove:"))
async def admin_remove_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_value = query.data.rsplit(":", 1)[1]
    if not raw_value.isdigit():
        await query.answer("Invalid user id.", show_alert=True)
        return

    target_user_id = int(raw_value)
    if len(app_context.config.admin_ids) == 1 and target_user_id in app_context.config.admin_ids:
        await query.answer("You cannot remove the last admin.", show_alert=True)
        return

    if target_user_id not in app_context.config.admin_ids:
        await query.answer("This user is no longer an admin.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="admins")
        return

    async with app_context.admin_lock:
        updated_admin_ids = remove_admin_id(app_context.config.config_file, target_user_id)
        _refresh_admin_ids(app_context, updated_admin_ids)
        _remove_admin_label(app_context, target_user_id)
        if app_context.pending_admin_request and app_context.pending_admin_request.candidate_id == target_user_id:
            app_context.pending_admin_request = None

    await _show_dashboard_from_callback(query, app_context, page="admins")
    await query.answer("Admin removed.")


@router.callback_query(F.data == "codex:refresh")
async def codex_refresh_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    app_context.cached_auth_state = None
    app_context.auth_checked_at = 0.0
    app_context.auth_refresh_task = None
    await _show_dashboard_from_callback(query, app_context, page="codex")
    await query.answer("Refreshed.")


@router.callback_query(F.data == "codex:login")
async def codex_login_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    if query.from_user is None or query.message is None:
        await query.answer("Unable to start login.", show_alert=True)
        return

    async with app_context.codex_login_lock:
        existing = app_context.codex_login_session
        if existing and not existing.completed:
            await query.answer("Login already in progress.", show_alert=True)
            return

        codex_path = app_context.runner.codex_path()
        if codex_path is None:
            await query.answer("Codex CLI not found on PATH.", show_alert=True)
            return

        process = await asyncio.create_subprocess_exec(
            app_context.config.codex_bin,
            "login",
            "--device-auth",
            cwd=str(app_context.config.working_dir if app_context.config.working_dir_exists else Path.home()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session = CodexLoginSession(
            owner_user_id=query.from_user.id,
            owner_chat_id=query.message.chat.id,
            process=process,
        )
        session.monitor_task = asyncio.create_task(
            _monitor_codex_login_session(app_context, query.bot, session)
        )
        app_context.codex_login_session = session

    await asyncio.sleep(1)
    await _show_dashboard_from_callback(query, app_context, page="codex")
    await query.answer("Login flow started.")


@router.callback_query(F.data == "codex:cancel_login")
async def codex_cancel_login_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    session = app_context.codex_login_session
    if session is None or session.completed:
        await query.answer("No active login flow.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="codex")
        return

    with suppress(ProcessLookupError):
        session.process.kill()
    with suppress(ProcessLookupError):
        await session.process.wait()
    session.completed = True
    session.returncode = session.process.returncode
    app_context.cached_auth_state = None
    app_context.auth_checked_at = 0.0
    app_context.auth_refresh_task = None

    await _show_dashboard_from_callback(query, app_context, page="codex")
    await query.answer("Login flow cancelled.")


@router.callback_query(F.data == "codex:logout")
async def codex_logout_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    if query.message is None:
        await query.answer("Unable to log out.", show_alert=True)
        return

    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        result = await app_context.runner.run_codex_logout()

    app_context.cached_auth_state = None
    app_context.auth_checked_at = 0.0
    app_context.auth_refresh_task = None
    await _show_dashboard_from_callback(query, app_context, page="codex")
    if result.ok:
        await query.answer("Logged out.")
    else:
        output = (result.stderr or result.stdout or "Logout failed.")[:180]
        await query.answer(output, show_alert=True)


@router.callback_query(F.data == "github:refresh")
async def github_refresh_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    await _show_dashboard_from_callback(query, app_context, page="github")
    await query.answer("Refreshed.")


@router.callback_query(F.data == "github:login")
async def github_login_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.from_user is None or query.message is None:
        await query.answer("Unable to start GitHub login.", show_alert=True)
        return

    async with app_context.github_login_lock:
        existing = app_context.github_login_session
        if existing and not existing.completed:
            await query.answer("Login already in progress.", show_alert=True)
            return

        gh_path = app_context.runner.gh_path()
        if gh_path is None:
            await query.answer("GitHub CLI not found on PATH.", show_alert=True)
            return

        env = os.environ.copy()
        env["BROWSER"] = "true"
        env["GH_PROMPT_DISABLED"] = "1"
        process = await asyncio.create_subprocess_exec(
            "gh",
            "auth",
            "login",
            "--hostname",
            "github.com",
            "--git-protocol",
            "https",
            "--skip-ssh-key",
            "--web",
            "--clipboard",
            cwd=str(app_context.config.working_dir if app_context.config.working_dir_exists else Path.home()),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        session = GitHubLoginSession(
            owner_user_id=query.from_user.id,
            owner_chat_id=query.message.chat.id,
            process=process,
        )
        session.monitor_task = asyncio.create_task(
            _monitor_github_login_session(app_context, query.bot, session)
        )
        app_context.github_login_session = session
        app_context.pending_github_token_request = None

    await asyncio.sleep(1)
    await _show_dashboard_from_callback(query, app_context, page="github")
    await query.answer("Login flow started.")


@router.callback_query(F.data == "github:cancel_login")
async def github_cancel_login_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    session = app_context.github_login_session
    if session is None or session.completed:
        await query.answer("No active login flow.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="github")
        return

    with suppress(ProcessLookupError):
        session.process.kill()
    with suppress(ProcessLookupError):
        await session.process.wait()
    session.completed = True
    session.returncode = session.process.returncode
    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None

    await _show_dashboard_from_callback(query, app_context, page="github")
    await query.answer("Login flow cancelled.")


@router.callback_query(F.data == "github:token")
async def github_token_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.from_user is None or query.message is None:
        await query.answer("Unable to start token login.", show_alert=True)
        return

    app_context.pending_github_token_request = PendingGitHubTokenRequest(
        requester_id=query.from_user.id,
        requester_chat_id=query.message.chat.id,
    )
    await _show_dashboard_from_callback(query, app_context, page="github")
    await query.answer("Send the token in the next message.")


@router.callback_query(F.data == "github:cancel_token")
async def github_cancel_token_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    app_context.pending_github_token_request = None
    await _show_dashboard_from_callback(query, app_context, page="github")
    await query.answer("Token login cancelled.")


@router.callback_query(F.data == "github:logout")
async def github_logout_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None:
        await query.answer("Unable to log out.", show_alert=True)
        return

    github_state = await _get_github_state(app_context)
    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        result = await app_context.runner.run_github_logout(github_state.login)

    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    await _show_dashboard_from_callback(query, app_context, page="github")
    if result.ok:
        await query.answer("Logged out.")
    else:
        output = (result.stderr or result.stdout or "Logout failed.")[:180]
        await query.answer(output, show_alert=True)


@router.callback_query(F.data == "github:test")
async def github_test_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None:
        await query.answer("Unable to run the GitHub check.", show_alert=True)
        return

    await query.answer("Running GitHub access checks…")
    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        github_state = await _get_github_state(app_context, force=True)
        api_result = await app_context.queue.submit(
            f"github api user by {query.from_user.id if query.from_user else query.message.chat.id}",
            app_context.runner.run_github_api_user,
        )
        push_result = await app_context.queue.submit(
            f"github push dry-run by {query.from_user.id if query.from_user else query.message.chat.id}",
            lambda: app_context.runner.run_git_command(
                ["push", "--dry-run", "origin", "HEAD"],
                timeout=app_context.config.git_timeout_seconds,
                log_command=False,
            ),
        )

    lines: list[str] = []
    if github_state.logged_in:
        lines.append(f"Status check: OK ({github_state.login or 'connected'})")
    else:
        lines.append("Status check: Failed")

    if api_result.ok:
        account_login = api_result.stdout.strip() or "unknown"
        lines.append(f"API check: OK ({account_login})")
    else:
        api_error = (api_result.stderr or api_result.stdout or "unknown error").strip().splitlines()[0]
        lines.append(f"API check: Failed ({api_error})")

    if push_result.ok:
        push_note = (push_result.stdout or "Everything up-to-date").strip().splitlines()[0]
        lines.append(f"Push dry-run: OK ({push_note})")
    else:
        push_error = (push_result.stderr or push_result.stdout or "unknown error").strip().splitlines()[0]
        lines.append(f"Push dry-run: Failed ({push_error})")

    all_ok = github_state.logged_in and api_result.ok and push_result.ok
    prefix = "✅ GitHub access looks good." if all_ok else "⚠️ GitHub access is not fully ready."
    app_context.flash_message = f"{prefix}\n" + "\n".join(lines)
    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    await _show_dashboard_from_callback(query, app_context, page="github")


@router.callback_query(F.data == "whisper:noop")
async def whisper_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "whisper:install")
async def whisper_install_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to install Whisper.", show_alert=True)
        return
    if app_context.whisper_task and not app_context.whisper_task.done():
        await query.answer("Whisper is already being installed.", show_alert=True)
        return

    app_context.whisper_progress = WhisperProgress(
        in_progress=True,
        percent=10,
        status_text="Installing Whisper runtime",
    )
    await query.answer("Installing Whisper…")
    await _show_dashboard_from_callback(query, app_context, page="settings")
    app_context.whisper_task = asyncio.create_task(
        _perform_whisper_install(
            app_context,
            bot=query.bot,
            chat_id=query.message.chat.id,
            user_id=query.from_user.id,
        )
    )


@router.callback_query(F.data == "whisper:delete")
async def whisper_delete_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to delete Whisper.", show_alert=True)
        return
    if app_context.whisper_task and not app_context.whisper_task.done():
        await query.answer("Whisper is already busy.", show_alert=True)
        return

    app_context.whisper_progress = WhisperProgress(
        in_progress=True,
        percent=10,
        status_text="Removing Whisper runtime",
    )
    await query.answer("Deleting Whisper…")
    await _show_dashboard_from_callback(query, app_context, page="settings")
    app_context.whisper_task = asyncio.create_task(
        _perform_whisper_delete(
            app_context,
            bot=query.bot,
            chat_id=query.message.chat.id,
            user_id=query.from_user.id,
        )
    )


@router.callback_query(F.data == "update:noop")
async def update_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "update:blocked")
async def update_blocked_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer("Wait until the bot update finishes.", show_alert=True)


@router.callback_query(F.data == "update:run")
async def update_run_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to update the bot.", show_alert=True)
        return
    if app_context.update_task and not app_context.update_task.done():
        await query.answer("Bot update is already running.", show_alert=True)
        return

    update_state = await _get_bot_update_state(app_context, force=True)
    if not update_state.update_available:
        await query.answer("You're already up to date.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="settings")
        return

    app_context.update_progress = BotUpdateProgress(
        awaiting_confirmation=True,
        percent=0,
        status_text="Waiting for confirmation",
        target_version=update_state.latest_version,
        latest_summary=update_state.latest_summary,
        latest_notes=update_state.latest_notes,
        old_commit=update_state.installed_commit,
        new_commit=update_state.latest_commit,
    )
    await query.answer("Review the update details below.")
    await _show_dashboard_from_callback(query, app_context, page="settings")


@router.callback_query(F.data == "update:cancel")
async def update_cancel_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    app_context.update_progress = None
    await query.answer("Update cancelled.")
    await _show_dashboard_from_callback(query, app_context, page="settings")


@router.callback_query(F.data == "update:confirm")
async def update_confirm_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to start the update.", show_alert=True)
        return

    progress = app_context.update_progress
    if progress is None or not progress.awaiting_confirmation:
        await query.answer("No update is waiting for confirmation.", show_alert=True)
        await _show_dashboard_from_callback(query, app_context, page="settings")
        return
    if app_context.update_task and not app_context.update_task.done():
        await query.answer("Bot update is already running.", show_alert=True)
        return

    progress.awaiting_confirmation = False
    progress.in_progress = True
    progress.percent = 5
    progress.status_text = "Preparing update"
    progress.error_text = None
    await query.answer("Starting update…")
    await _show_dashboard_from_callback(query, app_context, page="settings")
    app_context.update_task = asyncio.create_task(
        _perform_bot_update(
            app_context,
            bot=query.bot,
            chat_id=query.message.chat.id,
            user_id=query.from_user.id,
        )
    )


@router.callback_query(F.data == "root:change")
async def root_change_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.from_user is None or query.message is None:
        await query.answer("Unable to update the root path.", show_alert=True)
        return

    app_context.pending_workspaces_root_request = PendingWorkspacesRootRequest(
        requester_id=query.from_user.id,
        requester_chat_id=query.message.chat.id,
    )
    await _show_dashboard_from_callback(query, app_context, page="workspaces_root")
    await query.answer("Send the new absolute path in this chat.")


@router.callback_query(F.data == "root:cancel")
async def root_cancel_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    app_context.pending_workspaces_root_request = None
    await _show_dashboard_from_callback(query, app_context, page="workspaces_root")
    await query.answer("Root path update cancelled.")


@router.message()
async def generic_message_handler(message: Message, app_context: AppContext) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None:
        return
    if await _capture_pending_admin_candidate(message, app_context, user_id):
        return

    if not await _ensure_authorized_message(message, app_context):
        return

    await _sync_admin_label_from_message(message, app_context)

    if await _handle_pending_github_token_message(message, app_context):
        return

    if await _handle_pending_workspaces_root_message(message, app_context):
        return

    if message.voice is not None:
        await _handle_voice_message(message, app_context)
        return

    if message.photo or message.document is not None:
        if await _handle_image_message(message, app_context):
            return

    text = (message.text or "").strip()
    if not text:
        return

    if text.startswith("/"):
        command_name = text.split(maxsplit=1)[0].lower()
        if command_name in {"/ask", "/fix", "/commit"}:
            await message.answer(
                "This shortcut was removed. Send the same request as a normal chat message instead."
            )
            return
        await message.answer("Unknown command. Use /help or send a plain message for Codex.")
        return

    _clear_pending_voice_requests_for_user(app_context, user_id)
    pending_images = _take_pending_images_for_chat(app_context, message.chat.id)
    try:
        await _run_codex_chat(
            message,
            app_context,
            text,
            image_paths=[attachment.temp_path for attachment in pending_images],
        )
    finally:
        _cleanup_image_attachments(pending_images)


async def _bootstrap_first_admin_if_needed(app_context: AppContext, user_id: int, label: str) -> bool:
    async with app_context.admin_lock:
        if app_context.config.admin_ids:
            return False
        updated_admin_ids = add_admin_id(app_context.config.config_file, user_id, label)
        _refresh_admin_ids(app_context, updated_admin_ids)
        _refresh_admin_labels(app_context, user_id, label)
        logger.info("Bootstrapped first admin from /start: %s", user_id)
        return True


async def _capture_pending_admin_candidate(
    message: Message,
    app_context: AppContext,
    user_id: int,
) -> bool:
    pending = app_context.pending_admin_request
    if pending is None:
        return False
    if user_id in app_context.config.admin_ids:
        return False
    if pending.candidate_id is not None:
        return False

    label = _format_user_label(message)
    pending.candidate_id = user_id
    pending.candidate_label = label

    await message.answer(
        "Your access request was received. Wait for the admin to confirm it."
    )
    await _refresh_dashboard_for_chat(
        bot=message.bot,
        app_context=app_context,
        chat_id=pending.inviter_chat_id,
        user_id=pending.inviter_id,
        page="admins",
    )
    return True


async def _ensure_authorized_message(message: Message, app_context: AppContext) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if is_admin(user_id, app_context.config.admin_ids):
        return True

    logger.warning("Unauthorized Telegram command from %s", user_id)
    await message.answer(build_unauthorized_message(user_id))
    return False


async def _ensure_authorized_callback(query: CallbackQuery, app_context: AppContext) -> bool:
    user_id = query.from_user.id if query.from_user else None
    if is_admin(user_id, app_context.config.admin_ids):
        return True

    logger.warning("Unauthorized Telegram callback from %s", user_id)
    await query.answer("Access denied.", show_alert=True)
    if query.message:
        await query.message.answer(build_unauthorized_message(user_id))
    return False


async def _run_serialized(
    *,
    message: Message,
    app_context: AppContext,
    label: str,
    task_factory: Callable[[], Awaitable[Any]],
) -> Any:
    jobs_ahead = app_context.queue.jobs_ahead()
    if jobs_ahead > 0:
        await message.answer(
            f"Queued. Jobs ahead: <b>{jobs_ahead}</b>. "
            "This bot runs commands one by one to avoid overlapping sessions."
        )

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        return await app_context.queue.submit(label, task_factory)


async def _send_result_chunks(message: Message, chunks: list[str]) -> None:
    for chunk in chunks:
        await message.answer(chunk)


async def _projects_with_active_selection(app_context: AppContext) -> list[WorkspaceProject]:
    projects = scan_workspace_projects(app_context.config.workspaces_root)
    desired = choose_active_project(app_context.config.workspaces_root, app_context.config.working_dir)
    if desired != app_context.config.working_dir:
        await _set_active_project(app_context, desired)
        projects = scan_workspace_projects(app_context.config.workspaces_root)
    return projects


def _unavailable_auth_state(app_context: AppContext, reason: str) -> CodexAuthState:
    cli_path = app_context.runner.codex_path()
    if cli_path is None:
        return CodexAuthState(
            cli_path=None,
            cli_version=None,
            logged_in=False,
            auth_mode=None,
            auth_provider=None,
            account_name=None,
            account_email=None,
            status_summary="CLI not found",
            raw_status="Codex CLI was not found on PATH.",
        )
    return CodexAuthState(
        cli_path=cli_path,
        cli_version=None,
        logged_in=False,
        auth_mode=None,
        auth_provider=None,
        account_name=None,
        account_email=None,
        status_summary="Status unavailable",
        raw_status=reason,
        probe_ok=False,
    )


def _unavailable_github_state(app_context: AppContext, reason: str) -> GitHubAuthState:
    cli_path = app_context.runner.gh_path()
    if cli_path is None:
        return GitHubAuthState(
            cli_path=None,
            cli_version=None,
            logged_in=False,
            host="github.com",
            login=None,
            token_source=None,
            scopes=None,
            git_protocol=None,
            status_summary="CLI not found",
            raw_status="GitHub CLI was not found on PATH.",
        )
    return GitHubAuthState(
        cli_path=cli_path,
        cli_version=None,
        logged_in=False,
        host="github.com",
        login=None,
        token_source=None,
        scopes=None,
        git_protocol=None,
        status_summary="Status unavailable",
        raw_status=reason,
        probe_ok=False,
    )


def _unavailable_whisper_state(app_context: AppContext, reason: str) -> WhisperState:
    return WhisperState(
        installed=False,
        model_name=app_context.runner.whisper_model_name(),
        summary="Status unavailable",
        details=reason,
        probe_ok=False,
    )


def _unavailable_update_state(app_context: AppContext, reason: str) -> BotUpdateState:
    cached = app_context.cached_update_state
    return BotUpdateState(
        installed_commit=cached.installed_commit if cached else None,
        latest_commit=cached.latest_commit if cached else None,
        latest_version=cached.latest_version if cached else None,
        latest_summary=cached.latest_summary if cached else None,
        latest_notes=list(cached.latest_notes) if cached else [],
        update_available=False,
        check_ok=False,
        status_summary="Update check unavailable",
    )


def _unavailable_environment_status(app_context: AppContext) -> EnvironmentStatus:
    git_repo = (app_context.config.working_dir / ".git").exists() if app_context.config.working_dir_exists else False
    return EnvironmentStatus(
        workspaces_root=app_context.config.workspaces_root,
        workspaces_root_exists=app_context.config.workspaces_root_exists,
        working_dir=app_context.config.working_dir,
        working_dir_exists=app_context.config.working_dir_exists,
        codex_path=app_context.runner.codex_path(),
        git_repo=git_repo,
        git_branch=None,
        latest_commit_summary=None,
        changed_files_count=None,
        active_job=app_context.queue.active_label,
        queued_jobs=app_context.queue.waiting_count,
    )


def _track_refresh_task(
    app_context: AppContext,
    *,
    task_attr: str,
    cached_attr: str,
    checked_at_attr: str,
    task: asyncio.Task[Any],
    label: str,
) -> None:
    setattr(app_context, task_attr, task)

    def _store_result(done_task: asyncio.Task[Any]) -> None:
        if getattr(app_context, task_attr) is done_task:
            setattr(app_context, task_attr, None)
        try:
            result = done_task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("%s refresh failed", label)
            return
        setattr(app_context, cached_attr, result)
        setattr(app_context, checked_at_attr, time.monotonic())

    task.add_done_callback(_store_result)


async def _resolve_refresh(
    app_context: AppContext,
    *,
    force: bool,
    cached_attr: str,
    checked_at_attr: str,
    task_attr: str,
    success_ttl: float,
    error_ttl: float,
    timeout_seconds: float,
    fetcher: Callable[[], Awaitable[Any]],
    fallback_factory: Callable[[], Any],
    ok_attr: str,
    label: str,
) -> Any:
    now = time.monotonic()
    cached = getattr(app_context, cached_attr)
    checked_at = getattr(app_context, checked_at_attr)
    ttl = success_ttl if cached is None else (success_ttl if getattr(cached, ok_attr, True) else error_ttl)
    if not force and cached is not None and now - checked_at < ttl:
        return cached

    task = getattr(app_context, task_attr)
    if task is None or task.done():
        task = asyncio.create_task(fetcher(), name=f"{label}-refresh")
        _track_refresh_task(
            app_context,
            task_attr=task_attr,
            cached_attr=cached_attr,
            checked_at_attr=checked_at_attr,
            task=task,
            label=label,
        )

    if cached is not None and not force:
        return cached

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("%s refresh exceeded %.1fs", label, timeout_seconds)
    except Exception:
        logger.exception("%s refresh failed", label)

    fallback = cached if cached is not None else fallback_factory()
    setattr(app_context, cached_attr, fallback)
    setattr(app_context, checked_at_attr, now)
    return fallback


async def _get_bot_update_state(app_context: AppContext, *, force: bool = False) -> BotUpdateState:
    return await _resolve_refresh(
        app_context,
        force=force,
        cached_attr="cached_update_state",
        checked_at_attr="update_checked_at",
        task_attr="update_refresh_task",
        success_ttl=UPDATE_CHECK_TTL_SECONDS,
        error_ttl=UPDATE_ERROR_CACHE_TTL_SECONDS,
        timeout_seconds=UPDATE_REFRESH_TIMEOUT_SECONDS,
        fetcher=app_context.runner.collect_bot_update_state,
        fallback_factory=lambda: _unavailable_update_state(
            app_context,
            "Update check is taking too long. Try again from Settings.",
        ),
        ok_attr="check_ok",
        label="bot update state",
    )


async def _get_environment_status(app_context: AppContext, *, force: bool = False) -> EnvironmentStatus:
    return await _resolve_refresh(
        app_context,
        force=force,
        cached_attr="cached_environment_status",
        checked_at_attr="environment_checked_at",
        task_attr="environment_refresh_task",
        success_ttl=STATUS_CACHE_TTL_SECONDS,
        error_ttl=STATUS_ERROR_CACHE_TTL_SECONDS,
        timeout_seconds=STATUS_REFRESH_TIMEOUT_SECONDS,
        fetcher=lambda: app_context.runner.collect_environment_status(
            active_job=app_context.queue.active_label,
            queued_jobs=app_context.queue.waiting_count,
        ),
        fallback_factory=lambda: _unavailable_environment_status(app_context),
        ok_attr="working_dir_exists",
        label="environment status",
    )


async def _get_auth_state(app_context: AppContext, *, force: bool = False) -> CodexAuthState:
    return await _resolve_refresh(
        app_context,
        force=force,
        cached_attr="cached_auth_state",
        checked_at_attr="auth_checked_at",
        task_attr="auth_refresh_task",
        success_ttl=STATUS_CACHE_TTL_SECONDS,
        error_ttl=STATUS_ERROR_CACHE_TTL_SECONDS,
        timeout_seconds=STATUS_REFRESH_TIMEOUT_SECONDS,
        fetcher=app_context.runner.collect_codex_auth_state,
        fallback_factory=lambda: _unavailable_auth_state(
            app_context,
            "Codex CLI status check is taking too long. Open Settings -> Codex CLI and tap Refresh.",
        ),
        ok_attr="probe_ok",
        label="codex auth state",
    )


async def _get_github_state(app_context: AppContext, *, force: bool = False) -> GitHubAuthState:
    return await _resolve_refresh(
        app_context,
        force=force,
        cached_attr="cached_github_state",
        checked_at_attr="github_checked_at",
        task_attr="github_refresh_task",
        success_ttl=STATUS_CACHE_TTL_SECONDS,
        error_ttl=STATUS_ERROR_CACHE_TTL_SECONDS,
        timeout_seconds=STATUS_REFRESH_TIMEOUT_SECONDS,
        fetcher=app_context.runner.collect_github_auth_state,
        fallback_factory=lambda: _unavailable_github_state(
            app_context,
            "GitHub status check is taking too long. Open Settings -> GitHub and tap Refresh.",
        ),
        ok_attr="probe_ok",
        label="github auth state",
    )


def _home_codex_notice(auth_state: CodexAuthState) -> str | None:
    if auth_state.cli_path is None:
        return "Codex CLI is not installed. Re-run the installer to install it automatically."
    if not auth_state.probe_ok:
        return "Codex CLI status is temporarily unavailable. Open Settings -> Codex CLI to retry."
    if auth_state.logged_in:
        return None
    return "Codex CLI is not authorized yet. Open Settings -> Codex CLI to log in."


async def _get_whisper_state(app_context: AppContext, *, force: bool = False) -> WhisperState:
    return await _resolve_refresh(
        app_context,
        force=force,
        cached_attr="cached_whisper_state",
        checked_at_attr="whisper_checked_at",
        task_attr="whisper_refresh_task",
        success_ttl=STATUS_CACHE_TTL_SECONDS,
        error_ttl=STATUS_ERROR_CACHE_TTL_SECONDS,
        timeout_seconds=STATUS_REFRESH_TIMEOUT_SECONDS,
        fetcher=app_context.runner.collect_whisper_state,
        fallback_factory=lambda: _unavailable_whisper_state(
            app_context,
            "Whisper runtime detection is taking too long. Open Settings and retry from there.",
        ),
        ok_attr="probe_ok",
        label="whisper state",
    )


def _get_model_catalog(app_context: AppContext, *, force: bool = False) -> list[CodexModelInfo]:
    now = time.monotonic()
    if (
        not force
        and app_context.cached_model_catalog is not None
        and now - app_context.model_catalog_checked_at < MODEL_CATALOG_CACHE_TTL_SECONDS
    ):
        return app_context.cached_model_catalog

    catalog = app_context.runner.collect_model_catalog()
    app_context.cached_model_catalog = catalog
    app_context.model_catalog_checked_at = now
    return catalog


def _available_model_slugs(app_context: AppContext) -> list[str]:
    return [item.slug for item in _get_model_catalog(app_context)] or list(DEFAULT_SELECTED_MODELS)


def _model_info(app_context: AppContext, model_slug: str) -> CodexModelInfo | None:
    normalized = normalize_model_slug(model_slug)
    for item in _get_model_catalog(app_context):
        if item.slug == normalized:
            return item
    return None


def _thinking_levels_for_model(app_context: AppContext, model_slug: str) -> list[str]:
    info = _model_info(app_context, model_slug)
    if info is None:
        return list(DEFAULT_REASONING_LEVELS)
    return list(info.supported_reasoning_levels) or list(DEFAULT_REASONING_LEVELS)


def _default_thinking_for_model(app_context: AppContext, model_slug: str) -> str:
    info = _model_info(app_context, model_slug)
    if info is None or not info.default_reasoning_level:
        return DEFAULT_REASONING_LEVELS[1]
    return info.default_reasoning_level


def _effective_thinking_level(app_context: AppContext, model_slug: str) -> str:
    current = app_context.config.codex_thinking_level
    levels = _thinking_levels_for_model(app_context, model_slug)
    if current in levels:
        return current
    default_level = _default_thinking_for_model(app_context, model_slug)
    return default_level if default_level in levels else levels[0]


def _render_update_progress_block(progress: BotUpdateProgress | None) -> str | None:
    if progress is None:
        return None

    percent = max(0, min(progress.percent, 100))
    filled = min(10, max(0, round(percent / 10)))
    bar = f"[{'#' * filled}{'·' * (10 - filled)}] {percent}%"
    lines = [
        "<b>Bot update</b>",
        f"Status: <code>{html.escape(progress.status_text or 'Idle')}</code>",
        f"Progress: <code>{bar}</code>",
    ]
    if progress.target_version:
        lines.append(f"Target version: <code>{html.escape(progress.target_version)}</code>")
    if progress.awaiting_confirmation:
        lines.append("")
        lines.append("Confirm the update below.")
    if progress.awaiting_confirmation and progress.latest_notes:
        lines.append("")
        lines.append("<b>Update details</b>")
        lines.extend(f"• {html.escape(note)}" for note in progress.latest_notes[:3])
    if progress.error_text:
        lines.append("")
        lines.append(f"<blockquote>{html.escape(progress.error_text)}</blockquote>")
    return "\n".join(lines)


def _render_whisper_progress_block(progress: WhisperProgress | None) -> str | None:
    if progress is None:
        return None

    percent = max(0, min(progress.percent, 100))
    filled = min(10, max(0, round(percent / 10)))
    bar = f"[{'#' * filled}{'·' * (10 - filled)}] {percent}%"
    lines = [
        "<b>Whisper</b>",
        f"Status: <code>{html.escape(progress.status_text or 'Idle')}</code>",
        f"Progress: <code>{bar}</code>",
    ]
    if progress.error_text:
        lines.append("")
        lines.append(f"<blockquote>{html.escape(progress.error_text)}</blockquote>")
    return "\n".join(lines)


def _effective_selected_models(app_context: AppContext) -> list[str]:
    available_models = set(_available_model_slugs(app_context))
    selected = [normalize_model_slug(model) for model in app_context.config.codex_selected_models if normalize_model_slug(model) in available_models]
    return selected or [model for model in DEFAULT_SELECTED_MODELS if model in available_models] or _available_model_slugs(app_context)[:2]


def _cycle_current_model(app_context: AppContext) -> str:
    selected = _effective_selected_models(app_context)
    current = app_context.config.codex_model
    current_index = selected.index(current) if current in selected else 0
    return selected[(current_index + 1) % len(selected)]


def _cycle_thinking_level(app_context: AppContext) -> str:
    levels = _thinking_levels_for_model(app_context, app_context.config.codex_model)
    current = _effective_thinking_level(app_context, app_context.config.codex_model)
    current_index = levels.index(current) if current in levels else 0
    return levels[(current_index + 1) % len(levels)]


def _active_project_index(app_context: AppContext, projects: list[WorkspaceProject]) -> int | None:
    for index, project in enumerate(projects):
        if project.path == app_context.config.working_dir:
            return index
    return None


async def _set_active_project(app_context: AppContext, project_path: Path) -> None:
    resolved = project_path.expanduser().resolve(strict=False)
    update_workspace_settings(
        app_context.config.config_file,
        workspaces_root=app_context.config.workspaces_root,
        active_project_path=resolved,
    )
    object.__setattr__(app_context.config, "active_project_path", resolved)
    app_context.cached_environment_status = None
    app_context.environment_checked_at = 0.0
    app_context.environment_refresh_task = None
    app_context.cached_auth_state = None
    app_context.auth_checked_at = 0.0
    app_context.auth_refresh_task = None
    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    app_context.cached_whisper_state = None
    app_context.whisper_checked_at = 0.0
    app_context.whisper_refresh_task = None


async def _set_codex_preferences(
    app_context: AppContext,
    *,
    codex_model: str | None = None,
    selected_models: list[str] | None = None,
    thinking_level: str | None = None,
    sandbox_mode: str | None = None,
) -> None:
    normalized_model = normalize_model_slug(codex_model) if codex_model is not None else None
    normalized_selected = [normalize_model_slug(item) for item in selected_models] if selected_models is not None else None
    normalized_sandbox_mode = normalize_codex_sandbox_mode(sandbox_mode) if sandbox_mode is not None else None
    update_codex_preferences(
        app_context.config.config_file,
        codex_model=normalized_model,
        selected_models=normalized_selected,
        thinking_level=thinking_level,
        sandbox_mode=normalized_sandbox_mode,
    )
    if normalized_model is not None:
        object.__setattr__(app_context.config, "codex_model", normalized_model)
    if normalized_selected is not None:
        object.__setattr__(app_context.config, "codex_selected_models", tuple(normalized_selected))
    if thinking_level is not None:
        object.__setattr__(app_context.config, "codex_thinking_level", thinking_level)
    if normalized_sandbox_mode is not None:
        object.__setattr__(app_context.config, "codex_sandbox_mode", normalized_sandbox_mode)


async def _git_branches_for_active_project(app_context: AppContext) -> tuple[list[str], str | None]:
    probe = await app_context.runner.run_git_command(
        ["rev-parse", "--is-inside-work-tree"],
        timeout=15,
        log_command=False,
    )
    if not probe.ok or probe.stdout.strip() != "true":
        return [], None

    branch_result = await app_context.runner.run_git_command(
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
        timeout=15,
        log_command=False,
    )
    if not branch_result.ok:
        return [], None

    branches = [line.strip() for line in branch_result.stdout.splitlines() if line.strip()]
    current_branch_result = await app_context.runner.run_git_command(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        timeout=15,
        log_command=False,
    )
    current_branch = current_branch_result.stdout.strip() if current_branch_result.ok else None
    if current_branch and current_branch not in branches:
        branches.insert(0, current_branch)
    return branches, current_branch or None


async def _checkout_branch(app_context: AppContext, branch_name: str) -> Any:
    return await app_context.queue.submit(
        f"branch switch to {branch_name}",
        lambda: app_context.runner.run_git_command(
            ["checkout", branch_name],
            timeout=app_context.config.command_timeout_seconds,
        ),
    )


async def _set_workspaces_root(app_context: AppContext, root_path: Path) -> None:
    resolved_root = root_path.expanduser().resolve(strict=False)
    active_project = choose_active_project(resolved_root, app_context.config.working_dir)
    update_workspace_settings(
        app_context.config.config_file,
        workspaces_root=resolved_root,
        active_project_path=active_project,
    )
    object.__setattr__(app_context.config, "workspaces_root", resolved_root)
    object.__setattr__(app_context.config, "active_project_path", active_project)
    app_context.cached_environment_status = None
    app_context.environment_checked_at = 0.0
    app_context.environment_refresh_task = None
    app_context.cached_auth_state = None
    app_context.auth_checked_at = 0.0
    app_context.auth_refresh_task = None
    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    app_context.cached_whisper_state = None
    app_context.whisper_checked_at = 0.0
    app_context.whisper_refresh_task = None


async def _handle_pending_workspaces_root_message(message: Message, app_context: AppContext) -> bool:
    pending = app_context.pending_workspaces_root_request
    if pending is None:
        return False

    if message.from_user is None:
        return False

    if message.from_user.id != pending.requester_id or message.chat.id != pending.requester_chat_id:
        return False

    raw_path = (message.text or "").strip()
    if not raw_path:
        await message.answer("Send an absolute folder path.")
        return True

    raw_candidate = Path(raw_path).expanduser()
    if not raw_candidate.is_absolute():
        await message.answer("Use an absolute path.")
        return True
    candidate = raw_candidate.resolve(strict=False)
    if not candidate.exists() or not candidate.is_dir():
        await message.answer("Folder not found.")
        return True

    await _set_workspaces_root(app_context, candidate)
    app_context.pending_workspaces_root_request = None
    app_context.flash_message = f"✅ Workspaces root updated to {candidate}"
    await _show_dashboard_from_message(message, app_context, page="home")
    return True


async def _handle_pending_github_token_message(message: Message, app_context: AppContext) -> bool:
    pending = app_context.pending_github_token_request
    if pending is None or message.from_user is None:
        return False
    if pending.requester_id != message.from_user.id or pending.requester_chat_id != message.chat.id:
        return False

    token = (message.text or "").strip()
    if not token or token.startswith("/"):
        await message.answer("Send the GitHub token as plain text, or tap Cancel in Settings -> GitHub.")
        return True

    app_context.pending_github_token_request = None
    with suppress(Exception):
        await message.delete()

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        login_result = await app_context.queue.submit(
            f"github token login by {message.from_user.id}",
            lambda: app_context.runner.run_github_token_login(token),
        )
        setup_result = None
        if login_result.ok:
            setup_result = await app_context.queue.submit(
                f"github setup-git by {message.from_user.id}",
                app_context.runner.run_github_setup_git,
            )

    app_context.cached_github_state = None
    app_context.github_checked_at = 0.0
    app_context.github_refresh_task = None
    github_state = await _get_github_state(app_context, force=True)

    if login_result.ok and github_state.logged_in:
        if setup_result is None or setup_result.ok:
            app_context.flash_message = "✅ GitHub connected."
        else:
            app_context.flash_message = "✅ GitHub connected, but git credential helper setup failed."
    else:
        error_text = (login_result.stderr or login_result.stdout or "GitHub login failed.")[:600]
        app_context.flash_message = f"❌ {error_text}"

    await _show_dashboard_from_message(message, app_context, page="github")
    return True


@router.callback_query(F.data.startswith("voice:approve:"))
async def voice_approve_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to approve this transcription.", show_alert=True)
        return

    preview_id = query.data.rsplit(":", 1)[1]
    pending = app_context.pending_voice_requests.get(preview_id)
    if pending is None:
        await query.answer("This transcription is no longer available.", show_alert=True)
        return
    if pending.owner_user_id != query.from_user.id:
        await query.answer("Only the original sender can approve this transcription.", show_alert=True)
        return

    app_context.pending_voice_requests.pop(preview_id, None)
    await _edit_streaming_message(query.message, "Approved. Running Codex…")
    await query.answer("Approved.")
    await _run_codex_chat_request(
        query.message,
        app_context,
        pending.prompt_text,
        user_id=query.from_user.id,
    )


@router.callback_query(F.data.startswith("voice:cancel:"))
async def voice_cancel_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to cancel this transcription.", show_alert=True)
        return

    preview_id = query.data.rsplit(":", 1)[1]
    pending = app_context.pending_voice_requests.get(preview_id)
    if pending is None:
        await query.answer("This transcription is no longer available.", show_alert=True)
        return
    if pending.owner_user_id != query.from_user.id:
        await query.answer("Only the original sender can cancel this transcription.", show_alert=True)
        return

    app_context.pending_voice_requests.pop(preview_id, None)
    await _edit_streaming_message(query.message, "Voice transcription cancelled.")
    await query.answer("Cancelled.")


async def _run_codex_chat(
    message: Message,
    app_context: AppContext,
    prompt: str,
    *,
    image_paths: list[Path] | None = None,
) -> None:
    await _run_codex_chat_request(
        message,
        app_context,
        prompt,
        user_id=message.from_user.id if message.from_user else None,
        image_paths=image_paths,
    )


def _reset_context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm", callback_data="reset_context:confirm"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="reset_context:cancel"),
            ]
        ]
    )


async def _current_branch_execution_context(app_context: AppContext) -> BranchExecutionContext:
    repo_path = app_context.config.working_dir.resolve(strict=False)
    probe = await app_context.runner.run_git_command(
        ["rev-parse", "--is-inside-work-tree"],
        timeout=15,
        log_command=False,
    )
    if not probe.ok or probe.stdout.strip() != "true":
        saved_state = app_context.conversation_store.get(repo_path, NO_GIT_BRANCH_KEY)
        return BranchExecutionContext(
            repo_path=repo_path,
            branch_name=NO_GIT_BRANCH_KEY,
            branch_label="workspace",
            head_sha=None,
            git_repo=False,
            saved_state=saved_state,
        )

    branch_result = await app_context.runner.run_git_command(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        timeout=15,
        log_command=False,
    )
    head_result = await app_context.runner.run_git_command(
        ["rev-parse", "HEAD"],
        timeout=15,
        log_command=False,
    )
    branch_name = branch_result.stdout.strip() if branch_result.ok and branch_result.stdout.strip() else "HEAD"
    head_sha = head_result.stdout.strip() if head_result.ok and head_result.stdout.strip() else None
    saved_state = app_context.conversation_store.get(repo_path, branch_name)
    return BranchExecutionContext(
        repo_path=repo_path,
        branch_name=branch_name,
        branch_label=branch_name,
        head_sha=head_sha,
        git_repo=True,
        saved_state=saved_state,
    )


def _build_context_prefix(summary: ConversationSummary | None) -> str:
    if summary is None:
        return ""

    lines: list[str] = []
    if summary.request:
        lines.append(f"request: {summary.request}")
    if summary.done:
        lines.append(f"done: {summary.done}")
    if summary.next:
        lines.append(f"next: {summary.next}")
    if not lines:
        return ""

    return (
        "Previous branch context:\n"
        + "\n".join(lines)
        + "\n\nUse this only as compact context from earlier work on the same branch.\n\n"
    )


def _build_conversation_summary(*, prompt: str, final_text: str) -> ConversationSummary:
    request = _clip_summary_text(prompt, limit=220)
    done = _clip_summary_text(_summary_first_line(final_text), limit=320)
    next_value = _clip_summary_text(_summary_last_line(final_text), limit=220)
    if not next_value:
        next_value = "Continue from the current repository state on this branch."
    return ConversationSummary(
        request=request,
        done=done,
        next=next_value,
    )


def _summary_first_line(text: str) -> str:
    for chunk in re.split(r"\n{2,}|\n", text):
        cleaned = " ".join(chunk.split()).strip()
        if cleaned:
            return cleaned
    return ""


def _summary_last_line(text: str) -> str:
    parts = [
        " ".join(chunk.split()).strip()
        for chunk in re.split(r"\n{2,}|\n", text)
        if " ".join(chunk.split()).strip()
    ]
    if not parts:
        return ""
    if len(parts) == 1:
        return ""
    return parts[-1]


def _clip_summary_text(value: str, *, limit: int) -> str:
    normalized = " ".join(value.split()).strip()
    if len(normalized) <= limit:
        return normalized
    clipped = normalized[: limit - 1].rstrip()
    cutoff = clipped.rfind(" ")
    if cutoff >= max(limit // 2, 24):
        clipped = clipped[:cutoff]
    return f"{clipped}…"


def _looks_like_resume_session_failure(error_text: str) -> bool:
    normalized = error_text.lower()
    return any(
        marker in normalized
        for marker in (
            "session",
            "thread",
            "conversation",
            "resume",
            "not found",
            "does not exist",
            "unknown",
            "invalid value",
            "invalid uuid",
        )
    )


def _persist_branch_conversation_state(
    app_context: AppContext,
    *,
    context: BranchExecutionContext,
    prompt: str,
    result: Any,
    session_id_override: str | None = None,
) -> None:
    if not result.result.ok:
        return

    session_id = session_id_override if session_id_override is not None else result.session_id
    summary_text = result.final_text.strip() or result.preview_text.strip()
    summary = _build_conversation_summary(prompt=prompt, final_text=summary_text)
    app_context.conversation_store.set(
        repo_path=context.repo_path,
        branch_name=context.branch_name,
        session_id=session_id,
        last_seen_head=context.head_sha,
        codex_sandbox_mode=normalize_codex_sandbox_mode(app_context.config.codex_sandbox_mode),
        summary=summary,
    )


def _can_resume_saved_session(saved_state: BranchConversationState, current_sandbox_mode: str) -> bool:
    if not saved_state.session_id:
        return False
    saved_mode = normalize_codex_sandbox_mode(saved_state.codex_sandbox_mode or DEFAULT_CODEX_SANDBOX_MODE)
    current_mode = normalize_codex_sandbox_mode(current_sandbox_mode)
    return saved_mode == current_mode


async def _run_branch_scoped_codex_prompt(
    *,
    app_context: AppContext,
    prompt: str,
    image_paths: list[Path],
    on_update: Callable[[str], Awaitable[None]] | None,
) -> Any:
    context = await _current_branch_execution_context(app_context)
    saved_state = context.saved_state
    context_prefix = _build_context_prefix(saved_state.summary if saved_state is not None else None)

    if image_paths:
        image_prompt = f"{context_prefix}{prompt}" if context_prefix else prompt
        result = await app_context.runner.run_codex_streaming_prompt(
            image_prompt,
            image_paths=image_paths,
            on_update=on_update,
        )
        _persist_branch_conversation_state(app_context, context=context, prompt=prompt, result=result)
        return result

    if saved_state is not None and saved_state.session_id:
        if not _can_resume_saved_session(saved_state, app_context.config.codex_sandbox_mode):
            logger.warning(
                "Skipping resume for %s [%s] because sandbox mode changed from %s to %s.",
                context.repo_path,
                context.branch_label,
                saved_state.codex_sandbox_mode or DEFAULT_CODEX_SANDBOX_MODE,
                app_context.config.codex_sandbox_mode,
            )
        else:
            result = await app_context.runner.run_codex_streaming_prompt(
                prompt,
                resume_session_id=saved_state.session_id,
                on_update=on_update,
            )
            if result.result.ok:
                _persist_branch_conversation_state(
                    app_context,
                    context=context,
                    prompt=prompt,
                    result=result,
                    session_id_override=result.session_id or saved_state.session_id,
                )
                return result

            error_text = result.result.stderr or result.result.stdout or ""
            if not _looks_like_resume_session_failure(error_text):
                return result

            logger.warning(
                "Resume failed for %s [%s], falling back to a fresh session with saved summary.",
                context.repo_path,
                context.branch_label,
            )

    fresh_prompt = f"{context_prefix}{prompt}" if context_prefix else prompt
    fresh_result = await app_context.runner.run_codex_streaming_prompt(
        fresh_prompt,
        image_paths=image_paths,
        on_update=on_update,
    )
    _persist_branch_conversation_state(app_context, context=context, prompt=prompt, result=fresh_result)
    return fresh_result


async def _run_codex_chat_request(
    message: Message,
    app_context: AppContext,
    prompt: str,
    *,
    user_id: int | None,
    image_paths: list[Path] | None = None,
) -> None:
    image_count = len(image_paths or [])
    logger.info(
        "Telegram chat prompt from %s in %s with %s image(s): %s",
        user_id,
        app_context.config.working_dir,
        image_count,
        prompt[:500],
    )

    jobs_ahead = app_context.queue.jobs_ahead()
    if jobs_ahead > 0:
        await message.answer(
            f"Queued. Jobs ahead: <b>{jobs_ahead}</b>. "
            "This bot runs Codex requests one by one."
        )

    if _supports_native_streaming_drafts(message):
        await _run_codex_chat_request_native(
            message,
            app_context,
            prompt,
            user_id=user_id,
            image_paths=image_paths or [],
        )
        return

    reply = await message.reply(
        _render_streaming_message(
            body="Thinking…",
            finished=False,
        ),
        reply_markup=response_controls_keyboard(
            current_model=app_context.config.codex_model,
            current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
        ),
    )

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        result = await app_context.queue.submit(
            f"chat by {user_id}",
            lambda: _execute_codex_chat_stream(
                reply=reply,
                prompt=prompt,
                app_context=app_context,
                image_paths=image_paths or [],
            ),
        )

    if result.result.ok:
        final_body = result.final_text.strip() or result.preview_text.strip()
        if final_body:
            await _edit_streaming_message(
                reply,
                _render_streaming_message(
                    body=final_body,
                    finished=True,
                ),
                reply_markup=response_controls_keyboard(
                    current_model=app_context.config.codex_model,
                    current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
                ),
            )
        else:
            await _edit_streaming_message(
                reply,
                _render_streaming_message(
                    body="Completed with no assistant text.",
                    finished=True,
                ),
                reply_markup=response_controls_keyboard(
                    current_model=app_context.config.codex_model,
                    current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
                ),
            )
    else:
        error_text = result.result.stderr or result.result.stdout or "Codex failed."
        await _edit_streaming_message(
            reply,
            _render_streaming_message(
                body=error_text,
                finished=True,
                failed=True,
            ),
            reply_markup=response_controls_keyboard(
                current_model=app_context.config.codex_model,
                current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
            ),
        )


async def _run_codex_chat_request_native(
    message: Message,
    app_context: AppContext,
    prompt: str,
    *,
    user_id: int | None,
    image_paths: list[Path],
) -> None:
    draft_id = _build_stream_draft_id(message)

    result = await app_context.queue.submit(
        f"chat by {user_id}",
        lambda: _execute_codex_chat_stream_native(
            message=message,
            draft_id=draft_id,
            prompt=prompt,
            app_context=app_context,
            image_paths=image_paths,
        ),
    )

    current_thinking_level = _effective_thinking_level(app_context, app_context.config.codex_model)
    markup = response_controls_keyboard(
        current_model=app_context.config.codex_model,
        current_thinking_level=current_thinking_level,
    )
    if result.result.ok:
        final_body = result.final_text.strip() or result.preview_text.strip() or "Completed with no assistant text."
        await message.reply(
            _render_final_stream_message(body=final_body),
            reply_markup=markup,
        )
    else:
        error_text = result.result.stderr or result.result.stdout or "Codex failed."
        await message.reply(
            _render_final_stream_message(body=error_text, failed=True),
            reply_markup=markup,
        )


async def _handle_voice_message(message: Message, app_context: AppContext) -> None:
    if message.voice is None or message.from_user is None:
        return

    whisper_state = await _get_whisper_state(app_context)
    if not whisper_state.installed:
        await message.reply("Whisper is not installed yet. Open Settings and install it first.")
        return

    jobs_ahead = app_context.queue.jobs_ahead()
    if jobs_ahead > 0:
        await message.answer(
            f"Queued. Jobs ahead: <b>{jobs_ahead}</b>. "
            "Voice transcription also runs one by one."
        )

    reply = await message.reply("Transcribing voice…")

    async def task_factory() -> Any:
        if message.voice is None:
            raise RuntimeError("Voice payload is missing.")
        file_info = await message.bot.get_file(message.voice.file_id)
        suffix = Path(file_info.file_path or "voice.ogg").suffix or ".ogg"
        temp_file = tempfile.NamedTemporaryFile(prefix="ex-cod-tg-voice-", suffix=suffix, delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()
        try:
            with temp_path.open("wb") as handle:
                await message.bot.download_file(file_info.file_path, destination=handle)
            return await app_context.runner.transcribe_voice_file(temp_path)
        finally:
            with suppress(OSError):
                temp_path.unlink()

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        transcription = await app_context.queue.submit(
            f"voice transcription by {message.from_user.id}",
            task_factory,
        )

    if not transcription.result.ok:
        error_text = transcription.result.stderr or transcription.result.stdout or "Voice transcription failed."
        await _edit_streaming_message(reply, f"Failed to transcribe voice.\n\n{html.escape(_clip_stream_text(error_text))}")
        return

    transcript_text = transcription.text.strip()
    if not transcript_text:
        await _edit_streaming_message(reply, "No speech was detected in this voice message.")
        return

    preview_id = uuid.uuid4().hex[:12]
    app_context.pending_voice_requests[preview_id] = PendingVoiceRequest(
        preview_id=preview_id,
        owner_user_id=message.from_user.id,
        owner_chat_id=message.chat.id,
        prompt_text=transcript_text,
        source_message_id=message.message_id,
        preview_message_id=reply.message_id,
        language=transcription.language,
    )
    preview = PendingVoicePreview(
        preview_id=preview_id,
        text=transcript_text,
        source_label=transcription.language or "unknown",
    )
    await reply.edit_text(
        build_voice_preview_text(preview=preview),
        reply_markup=voice_preview_keyboard(preview_id),
    )


async def _execute_codex_chat_stream(
    *,
    reply: Message,
    prompt: str,
    app_context: AppContext,
    image_paths: list[Path],
) -> Any:
    last_preview_emit_at = 0.0
    last_preview_text = ""
    heartbeat_tick = 0
    spinner_stop = asyncio.Event()
    render_lock = asyncio.Lock()

    async def render_preview(body: str, *, heartbeat: int = 0) -> None:
        in_progress_body = _append_stream_keepalive_footer(body, heartbeat_tick=heartbeat)
        async with render_lock:
            await _edit_streaming_message(
                reply,
                _render_streaming_message(
                    body=in_progress_body,
                    finished=False,
                ),
                reply_markup=response_controls_keyboard(
                    current_model=app_context.config.codex_model,
                    current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
                ),
            )

    async def spinner() -> None:
        nonlocal heartbeat_tick
        started_at = time.monotonic()
        frame_index = 0
        frames = (
            "Thinking",
            "Thinking.",
            "Thinking..",
            "Thinking...",
            "Generating reply",
            "Preparing reply",
        )
        while not spinner_stop.is_set():
            await asyncio.sleep(STREAM_KEEPALIVE_INTERVAL_SECONDS)
            if spinner_stop.is_set():
                return
            if last_preview_text:
                heartbeat_tick += 1
                await render_preview(last_preview_text, heartbeat=heartbeat_tick)
                continue
            elapsed = int(time.monotonic() - started_at)
            frame = frames[frame_index % len(frames)]
            frame_index += 1
            await render_preview(f"{frame} {elapsed}s")

    async def on_update(text: str) -> None:
        nonlocal last_preview_emit_at
        nonlocal last_preview_text
        nonlocal heartbeat_tick
        now = time.monotonic()
        text = text.strip()
        if not text:
            return
        if text == last_preview_text and now - last_preview_emit_at < STREAM_UPDATE_INTERVAL_SECONDS:
            return
        if now - last_preview_emit_at < STREAM_UPDATE_INTERVAL_SECONDS:
            return
        last_preview_text = text
        heartbeat_tick = 0
        await render_preview(text)
        last_preview_emit_at = now

    spinner_task = asyncio.create_task(spinner())
    try:
        return await _run_branch_scoped_codex_prompt(
            app_context=app_context,
            prompt=prompt,
            image_paths=image_paths,
            on_update=on_update,
        )
    finally:
        spinner_stop.set()
        spinner_task.cancel()
        with suppress(asyncio.CancelledError):
            await spinner_task


async def _execute_codex_chat_stream_native(
    *,
    message: Message,
    draft_id: int,
    prompt: str,
    app_context: AppContext,
    image_paths: list[Path],
) -> Any:
    last_preview_emit_at = 0.0
    last_preview_text = ""
    spinner_stop = asyncio.Event()
    keepalive_tick = 0
    blocked_until = 0.0
    draft_lock = asyncio.Lock()

    async def update_draft(text: str, *, heartbeat: int = 0) -> None:
        nonlocal blocked_until
        now = time.monotonic()
        if now < blocked_until:
            return
        rendered_text = _render_draft_stream_message(text, heartbeat_tick=heartbeat)
        try:
            async with draft_lock:
                await message.bot.send_message_draft(
                    chat_id=message.chat.id,
                    draft_id=draft_id,
                    text=rendered_text,
                )
        except TelegramRetryAfter as exc:
            blocked_until = time.monotonic() + float(exc.retry_after) + STREAM_DRAFT_RETRY_GRACE_SECONDS
            logger.warning(
                "Telegram draft update throttled in chat %s. Backing off for %.1fs.",
                message.chat.id,
                float(exc.retry_after),
            )
        except Exception:
            logger.exception("Failed to send Telegram draft update")

    async def spinner() -> None:
        nonlocal keepalive_tick
        started_at = time.monotonic()
        frame_index = 0
        frames = (
            "Thinking",
            "Thinking.",
            "Thinking..",
            "Thinking...",
            "Generating reply",
            "Preparing reply",
        )
        while not spinner_stop.is_set():
            await asyncio.sleep(STREAM_KEEPALIVE_INTERVAL_SECONDS)
            if spinner_stop.is_set():
                return
            if last_preview_text:
                keepalive_tick += 1
                await update_draft(last_preview_text, heartbeat=keepalive_tick)
                continue
            elapsed = int(time.monotonic() - started_at)
            frame = frames[frame_index % len(frames)]
            frame_index += 1
            await update_draft(f"{frame} {elapsed}s")

    async def on_update(text: str) -> None:
        nonlocal last_preview_emit_at
        nonlocal last_preview_text
        nonlocal keepalive_tick
        now = time.monotonic()
        text = text.strip()
        if not text:
            return
        if text == last_preview_text and now - last_preview_emit_at < STREAM_UPDATE_INTERVAL_SECONDS:
            return
        if now - last_preview_emit_at < STREAM_UPDATE_INTERVAL_SECONDS:
            return
        last_preview_text = text
        keepalive_tick = 0
        await update_draft(text)
        last_preview_emit_at = now

    await update_draft("Thinking…")
    spinner_task = asyncio.create_task(spinner())
    try:
        return await _run_branch_scoped_codex_prompt(
            app_context=app_context,
            prompt=prompt,
            image_paths=image_paths,
            on_update=on_update,
        )
    finally:
        spinner_stop.set()
        spinner_task.cancel()
        with suppress(asyncio.CancelledError):
            await spinner_task


async def _handle_image_message(message: Message, app_context: AppContext) -> bool:
    if message.from_user is None:
        return False

    supported = message.photo or _is_supported_image_document(message)
    if not supported:
        if message.document is not None:
            await message.reply("Please send a photo or an image file.")
            return True
        return False

    try:
        attachments = await _download_image_attachments(message, app_context)
    except ValueError as exc:
        await message.reply(html.escape(str(exc)))
        return True
    except OSError:
        logger.exception("Failed to download Telegram image attachment")
        await message.reply("Failed to download the image. Please try again.")
        return True

    caption = (message.caption or "").strip()
    if caption:
        pending_attachments = _take_pending_images_for_chat(app_context, message.chat.id)
        all_attachments = [*pending_attachments, *attachments]
        max_images = app_context.config.telegram_max_images_per_request
        if len(all_attachments) > max_images:
            _cleanup_image_attachments(attachments)
            app_context.pending_image_requests[message.chat.id] = PendingImageRequest(
                owner_user_id=message.from_user.id,
                owner_chat_id=message.chat.id,
                attachments=pending_attachments,
            )
            await message.reply(f"Too many images. Limit is {max_images} per request.")
            return True

        try:
            await _run_codex_chat(
                message,
                app_context,
                caption,
                image_paths=[attachment.temp_path for attachment in all_attachments],
            )
        finally:
            _cleanup_image_attachments(all_attachments)
        return True

    pending_request = app_context.pending_image_requests.get(message.chat.id)
    if pending_request is None:
        pending_request = PendingImageRequest(
            owner_user_id=message.from_user.id,
            owner_chat_id=message.chat.id,
        )
        app_context.pending_image_requests[message.chat.id] = pending_request

    pending_request.owner_user_id = message.from_user.id
    pending_request.attachments.extend(attachments)

    max_images = app_context.config.telegram_max_images_per_request
    if len(pending_request.attachments) > max_images:
        overflow = pending_request.attachments[max_images:]
        pending_request.attachments = pending_request.attachments[:max_images]
        _cleanup_image_attachments(overflow)
        await message.reply(f"Image limit reached. Kept the first {max_images} images for the next Codex task.")
        return True

    count = len(pending_request.attachments)
    noun = "image" if count == 1 else "images"
    await message.reply(f"{count} {noun} received. Now send a task for Codex.")
    return True


async def _download_image_attachments(message: Message, app_context: AppContext) -> list[PendingImageAttachment]:
    candidates: list[tuple[str, str | None, int | None]] = []
    if message.photo:
        best_photo = message.photo[-1]
        candidates.append((best_photo.file_id, None, best_photo.file_size))
    elif message.document is not None:
        candidates.append((message.document.file_id, message.document.file_name, message.document.file_size))
    else:
        return []

    attachments: list[PendingImageAttachment] = []
    max_bytes = app_context.config.telegram_image_max_bytes
    try:
        for file_id, original_name, file_size in candidates:
            if file_size is not None and file_size > max_bytes:
                raise ValueError(f"Image is too large. Limit is {_format_bytes(max_bytes)}.")

            file_info = await message.bot.get_file(file_id)
            source_name = original_name or Path(file_info.file_path or "image").name or "image"
            suffix = Path(source_name).suffix or ".jpg"
            temp_file = tempfile.NamedTemporaryFile(prefix="ex-cod-tg-image-", suffix=suffix, delete=False)
            temp_path = Path(temp_file.name)
            temp_file.close()

            try:
                with temp_path.open("wb") as handle:
                    await message.bot.download_file(file_info.file_path, destination=handle)
            except Exception:
                with suppress(OSError):
                    temp_path.unlink()
                raise

            size_bytes = temp_path.stat().st_size
            if size_bytes > max_bytes:
                with suppress(OSError):
                    temp_path.unlink()
                raise ValueError(f"Image is too large. Limit is {_format_bytes(max_bytes)}.")

            attachments.append(
                PendingImageAttachment(
                    temp_path=temp_path,
                    original_name=source_name,
                    size_bytes=size_bytes,
                )
            )
        return attachments
    except Exception:
        _cleanup_image_attachments(attachments)
        raise


def _is_supported_image_document(message: Message) -> bool:
    document = message.document
    if document is None:
        return False
    mime_type = (document.mime_type or "").lower()
    if mime_type.startswith("image/"):
        return True
    suffix = Path(document.file_name or "").suffix.lower()
    return suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"}


def _take_pending_images_for_chat(app_context: AppContext, chat_id: int) -> list[PendingImageAttachment]:
    pending = app_context.pending_image_requests.pop(chat_id, None)
    if pending is None:
        return []
    return pending.attachments


def _cleanup_image_attachments(attachments: list[PendingImageAttachment]) -> None:
    for attachment in attachments:
        with suppress(OSError):
            attachment.temp_path.unlink()


def _format_bytes(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB")
    size = float(size_bytes)
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    precision = 0 if unit == "B" else 1
    return f"{size:.{precision}f} {unit}"


async def _show_dashboard_from_message(message: Message, app_context: AppContext, *, page: str) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id
    dashboard = app_context.dashboards.get(chat_id)
    if dashboard is None:
        text, markup = await _render_page(app_context, page=page)
        sent = await message.answer(text, reply_markup=markup)
        app_context.dashboards[chat_id] = DashboardSession(
            chat_id=chat_id,
            user_id=user_id,
            message_id=sent.message_id,
            page=page,
        )
        return

    await _refresh_dashboard_for_chat(
        bot=message.bot,
        app_context=app_context,
        chat_id=chat_id,
        user_id=user_id,
        page=page,
    )


async def _open_fresh_dashboard_from_message(message: Message, app_context: AppContext, *, page: str) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else chat_id
    text, markup = await _render_page(app_context, page=page)
    sent = await message.answer(text, reply_markup=markup)
    app_context.dashboards[chat_id] = DashboardSession(
        chat_id=chat_id,
        user_id=user_id,
        message_id=sent.message_id,
        page=page,
    )


async def send_fresh_dashboard_for_chat(
    *,
    bot: Bot,
    app_context: AppContext,
    chat_id: int,
    user_id: int,
    page: str = "home",
) -> None:
    text, markup = await _render_page(app_context, page=page)
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    app_context.dashboards[chat_id] = DashboardSession(
        chat_id=chat_id,
        user_id=user_id,
        message_id=sent.message_id,
        page=page,
    )


async def _show_dashboard_from_callback(query: CallbackQuery, app_context: AppContext, *, page: str) -> None:
    if query.message is None:
        return

    chat_id = query.message.chat.id
    user_id = query.from_user.id if query.from_user else chat_id
    text, markup = await _render_page(app_context, page=page)
    app_context.dashboards[chat_id] = DashboardSession(
        chat_id=chat_id,
        user_id=user_id,
        message_id=query.message.message_id,
        page=page,
    )
    await _edit_dashboard_message(query.message, text, markup)


async def _refresh_dashboard_for_chat(
    *,
    bot: Bot,
    app_context: AppContext,
    chat_id: int,
    user_id: int,
    page: str | None = None,
) -> None:
    dashboard = app_context.dashboards.get(chat_id)
    if dashboard is None:
        return

    target_page = page or dashboard.page
    text, markup = await _render_page(app_context, page=target_page)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=dashboard.message_id,
            text=text,
            reply_markup=markup,
        )
    except TelegramBadRequest:
        return

    app_context.dashboards[chat_id] = DashboardSession(
        chat_id=chat_id,
        user_id=user_id,
        message_id=dashboard.message_id,
        page=target_page,
    )


async def _refresh_quick_controls_target(query: CallbackQuery, app_context: AppContext) -> None:
    if query.message is None:
        return

    chat_id = query.message.chat.id
    dashboard = app_context.dashboards.get(chat_id)
    if dashboard and dashboard.message_id == query.message.message_id:
        await _refresh_dashboard_for_chat(
            bot=query.bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=query.from_user.id if query.from_user else dashboard.user_id,
            page=dashboard.page,
        )
        return

    try:
        await query.message.edit_reply_markup(
            reply_markup=response_controls_keyboard(
                current_model=app_context.config.codex_model,
                current_thinking_level=_effective_thinking_level(app_context, app_context.config.codex_model),
            )
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _edit_dashboard_message(
    message: Message,
    text: str,
    markup: Any,
) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


async def _render_page(app_context: AppContext, *, page: str) -> tuple[str, Any]:
    projects: list[WorkspaceProject] = []
    project_names: list[str] = []
    active_index: int | None = None
    branch_names: list[str] = []
    active_branch_index: int | None = None
    if page in {"home", "repos", "branches", "workspaces_root"}:
        projects = await _projects_with_active_selection(app_context)
        project_names = [project.name for project in projects]
        active_index = _active_project_index(app_context, projects)
        branch_names, current_branch = await _git_branches_for_active_project(app_context)
        active_branch_index = branch_names.index(current_branch) if current_branch in branch_names else None

    selected_models = _effective_selected_models(app_context)
    active_thinking_level = _effective_thinking_level(app_context, app_context.config.codex_model)

    if page == "home":
        environment, auth_state, whisper_state, update_state = await asyncio.gather(
            _get_environment_status(app_context),
            _get_auth_state(app_context),
            _get_whisper_state(app_context),
            _get_bot_update_state(app_context),
        )
        flash_message = app_context.flash_message
        app_context.flash_message = None
        text = build_home_text(
            environment=environment,
            update_state=update_state,
            active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
            has_active_project=active_index is not None,
            project_count=len(projects),
            showing_repo_list=False,
            showing_branch_list=False,
            whisper_summary=None if whisper_state.installed else whisper_state.summary,
            codex_notice=_home_codex_notice(auth_state),
        )
        if flash_message:
            text = f"{text}\n\n<blockquote>{flash_message}</blockquote>"
        return text, home_keyboard(
            project_names=project_names,
            active_index=active_index,
            showing_repo_list=False,
            branch_names=branch_names,
            active_branch_index=active_branch_index,
            showing_branch_list=False,
            current_model=app_context.config.codex_model,
            current_thinking_level=active_thinking_level,
        )

    if page == "repos":
        environment, auth_state, whisper_state, update_state = await asyncio.gather(
            _get_environment_status(app_context),
            _get_auth_state(app_context),
            _get_whisper_state(app_context),
            _get_bot_update_state(app_context),
        )
        return (
            build_home_text(
                environment=environment,
                update_state=update_state,
                active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
                has_active_project=active_index is not None,
                project_count=len(projects),
                showing_repo_list=True,
                showing_branch_list=False,
                whisper_summary=None if whisper_state.installed else whisper_state.summary,
                codex_notice=_home_codex_notice(auth_state),
            ),
            home_keyboard(
                project_names=project_names,
                active_index=active_index,
                showing_repo_list=True,
                branch_names=branch_names,
                active_branch_index=active_branch_index,
                showing_branch_list=False,
                current_model=app_context.config.codex_model,
                current_thinking_level=active_thinking_level,
            ),
        )

    if page == "branches":
        environment, auth_state, whisper_state, update_state = await asyncio.gather(
            _get_environment_status(app_context),
            _get_auth_state(app_context),
            _get_whisper_state(app_context),
            _get_bot_update_state(app_context),
        )
        return (
            build_home_text(
                environment=environment,
                update_state=update_state,
                active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
                has_active_project=active_index is not None,
                project_count=len(projects),
                showing_repo_list=False,
                showing_branch_list=True,
                whisper_summary=None if whisper_state.installed else whisper_state.summary,
                codex_notice=_home_codex_notice(auth_state),
            ),
            home_keyboard(
                project_names=project_names,
                active_index=active_index,
                showing_repo_list=False,
                branch_names=branch_names,
                active_branch_index=active_branch_index,
                showing_branch_list=True,
                current_model=app_context.config.codex_model,
                current_thinking_level=active_thinking_level,
            ),
        )

    if page == "settings":
        auth_state, github_state, whisper_state, update_state = await asyncio.gather(
            _get_auth_state(app_context),
            _get_github_state(app_context),
            _get_whisper_state(app_context),
            _get_bot_update_state(app_context),
        )
        update_progress = app_context.update_progress
        whisper_progress = app_context.whisper_progress
        flash_message = app_context.flash_message
        app_context.flash_message = None
        whisper_busy = bool(whisper_progress and whisper_progress.in_progress)
        return (
            build_settings_text(
                auth_state=auth_state,
                github_state=github_state,
                whisper_state=whisper_state,
                update_state=update_state,
                codex_execution_mode=app_context.config.codex_sandbox_mode,
                workspaces_root=app_context.config.workspaces_root,
                whisper_progress_block=_render_whisper_progress_block(whisper_progress),
                update_progress_block=_render_update_progress_block(update_progress),
                flash_message=flash_message,
            ),
            settings_keyboard(
                codex_execution_mode=app_context.config.codex_sandbox_mode,
                whisper_state=whisper_state,
                whisper_busy=whisper_busy,
                update_busy=bool(update_progress and update_progress.in_progress),
                update_confirm_pending=bool(update_progress and update_progress.awaiting_confirmation),
            ),
        )

    if page == "execution_mode":
        return (
            build_execution_mode_text(codex_execution_mode=app_context.config.codex_sandbox_mode),
            execution_mode_keyboard(codex_execution_mode=app_context.config.codex_sandbox_mode),
        )

    if page == "github":
        github_state = await _get_github_state(app_context)
        session = app_context.github_login_session
        device_auth = _extract_device_auth_view(session.render_output() if session else None)
        login_active = bool(session and not session.completed)
        waiting_for_token = app_context.pending_github_token_request is not None
        flash_message = app_context.flash_message
        app_context.flash_message = None
        return (
            build_github_text(
                auth_state=github_state,
                device_auth=device_auth,
                login_active=login_active,
                waiting_for_token=waiting_for_token,
                flash_message=flash_message,
            ),
            github_keyboard(
                auth_state=github_state,
                login_active=login_active,
                waiting_for_token=waiting_for_token,
            ),
        )

    if page == "selected_models":
        return (
            build_selected_models_text(selected_models=selected_models),
            selected_models_keyboard(
                available_models=_available_model_slugs(app_context),
                selected_models=selected_models,
            ),
        )

    if page == "workspaces_root":
        waiting_for_path = app_context.pending_workspaces_root_request is not None
        return (
            build_workspaces_root_text(
                workspaces_root=app_context.config.workspaces_root,
                active_project=app_context.config.working_dir,
                repo_names=project_names,
                waiting_for_path=waiting_for_path,
            ),
            workspaces_root_keyboard(waiting_for_path=waiting_for_path),
        )

    if page == "admins":
        pending = app_context.pending_admin_request
        candidate = None
        if pending and pending.candidate_id is not None:
            candidate = PendingAdminCandidate(
                user_id=pending.candidate_id,
                label=pending.candidate_label or "unknown",
            )
        waiting_for_candidate = pending is not None and pending.candidate_id is None
        admin_items = _sorted_admin_items(app_context)
        can_remove_admins = len(admin_items) > 1
        return (
            build_admins_text(
                admin_items=admin_items,
                candidate=candidate,
                waiting_for_candidate=waiting_for_candidate,
                can_remove_admins=can_remove_admins,
            ),
            admins_keyboard(
                admin_items=admin_items,
                candidate=candidate,
                waiting_for_candidate=waiting_for_candidate,
                can_remove_admins=can_remove_admins,
            ),
        )

    if page == "codex":
        auth_state = await _get_auth_state(app_context)
        session = app_context.codex_login_session
        device_auth = _extract_device_auth_view(session.render_output() if session else None)
        login_active = bool(session and not session.completed)
        return (
            build_codex_text(
                auth_state=auth_state,
                device_auth=device_auth,
                login_active=login_active,
            ),
            codex_keyboard(auth_state=auth_state, login_active=login_active),
        )

    return "Unknown page", home_keyboard(
        project_names=[],
        active_index=None,
        showing_repo_list=False,
        branch_names=[],
        active_branch_index=None,
        showing_branch_list=False,
        current_model=app_context.config.codex_model,
        current_thinking_level=active_thinking_level,
    )


async def _monitor_codex_login_session(
    app_context: AppContext,
    bot: Bot,
    session: CodexLoginSession,
) -> None:
    if session.process.stdout is None:
        session.completed = True
        return

    try:
        initial_auth_state = await app_context.runner.collect_codex_auth_state()
        while True:
            line = await session.process.stdout.readline()
            if not line:
                break
            session.output_lines.append(line.decode("utf-8", errors="replace"))
            if sum(len(item) for item in session.output_lines) > 5000:
                joined = "".join(session.output_lines)[-4000:]
                session.output_lines = [joined]
        await session.process.wait()
        session.returncode = session.process.returncode
    finally:
        session.completed = True
        final_auth_state = await app_context.runner.collect_codex_auth_state()
        app_context.cached_auth_state = final_auth_state
        app_context.auth_checked_at = time.monotonic()
        if not initial_auth_state.logged_in and final_auth_state.logged_in:
            app_context.flash_message = "✅ Successfully authorized"
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=session.owner_chat_id,
                user_id=session.owner_user_id,
                page="home",
            )
        else:
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=session.owner_chat_id,
                user_id=session.owner_user_id,
                page="codex",
            )


async def _monitor_github_login_session(
    app_context: AppContext,
    bot: Bot,
    session: GitHubLoginSession,
) -> None:
    if session.process.stdout is None:
        session.completed = True
        return

    setup_result = None
    initial_github_state = await app_context.runner.collect_github_auth_state()
    try:
        while True:
            line = await session.process.stdout.readline()
            if not line:
                break
            session.output_lines.append(line.decode("utf-8", errors="replace"))
            if sum(len(item) for item in session.output_lines) > 5000:
                joined = "".join(session.output_lines)[-4000:]
                session.output_lines = [joined]
        await session.process.wait()
        session.returncode = session.process.returncode
        if session.returncode == 0:
            setup_result = await app_context.runner.run_github_setup_git()
    finally:
        session.completed = True
        final_github_state = await app_context.runner.collect_github_auth_state()
        app_context.cached_github_state = final_github_state
        app_context.github_checked_at = time.monotonic()
        if not initial_github_state.logged_in and final_github_state.logged_in:
            if setup_result is None or setup_result.ok:
                app_context.flash_message = "✅ GitHub connected."
            else:
                app_context.flash_message = "✅ GitHub connected, but git credential helper setup failed."
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=session.owner_chat_id,
            user_id=session.owner_user_id,
            page="github",
        )


def _refresh_admin_ids(app_context: AppContext, admin_ids: list[int]) -> None:
    object.__setattr__(app_context.config, "admin_ids", frozenset(admin_ids))


def _refresh_admin_labels(app_context: AppContext, user_id: int, label: str) -> None:
    admin_labels = dict(app_context.config.admin_labels)
    admin_labels[user_id] = label
    object.__setattr__(app_context.config, "admin_labels", admin_labels)


def _remove_admin_label(app_context: AppContext, user_id: int) -> None:
    admin_labels = dict(app_context.config.admin_labels)
    admin_labels.pop(user_id, None)
    object.__setattr__(app_context.config, "admin_labels", admin_labels)


def _sorted_admin_items(app_context: AppContext) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for admin_id in sorted(app_context.config.admin_ids):
        items.append((admin_id, app_context.config.admin_labels.get(admin_id, str(admin_id))))
    return items


def _clear_pending_voice_requests_for_user(app_context: AppContext, user_id: int | None) -> None:
    if user_id is None:
        return
    stale_ids = [
        preview_id
        for preview_id, pending in app_context.pending_voice_requests.items()
        if pending.owner_user_id == user_id
    ]
    for preview_id in stale_ids:
        app_context.pending_voice_requests.pop(preview_id, None)


async def _schedule_post_update_notice(
    app_context: AppContext,
    *,
    chat_id: int,
    user_id: int,
    old_commit: str | None,
    new_commit: str | None,
    version: str | None,
    notes: list[str],
) -> None:
    notice = UpdateNotice(
        chat_id=chat_id,
        user_id=user_id,
        old_commit=old_commit,
        new_commit=new_commit,
        version=version,
        notes=notes,
    )
    save_update_notice(app_context.config.config_file.parent / "update_notice.json", notice)


async def _perform_bot_update(
    app_context: AppContext,
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
) -> None:
    progress = app_context.update_progress
    if progress is None:
        return

    try:
        progress.in_progress = True
        progress.awaiting_confirmation = False
        progress.percent = 10
        progress.status_text = "Downloading the installer"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        script_result, installer_path = await app_context.queue.submit(
            f"bot update download by {user_id}",
            lambda: app_context.runner.download_self_update_installer(progress.new_commit),
        )
        if not script_result.ok or installer_path is None:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Update failed"
            progress.error_text = (script_result.stderr or script_result.stdout or "Unable to download the installer.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 40
        progress.status_text = "Running the installer"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        install_result = await app_context.queue.submit(
            f"bot update install by {user_id}",
            lambda: app_context.runner.run_self_update_installer(installer_path, progress.new_commit),
        )
        app_context.cached_update_state = None
        app_context.update_checked_at = 0.0
        app_context.update_refresh_task = None

        if not install_result.ok:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Update failed"
            progress.error_text = (install_result.stderr or install_result.stdout or "Unknown update error.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 75
        progress.status_text = "Reinstalling the background service"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        await _schedule_post_update_notice(
            app_context,
            chat_id=chat_id,
            user_id=user_id,
            old_commit=progress.old_commit,
            new_commit=progress.new_commit,
            version=progress.target_version,
            notes=progress.latest_notes,
        )

        progress.percent = 90
        progress.status_text = "Update installed. Restarting the service"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        await asyncio.sleep(0.5)
        await app_context.runner.trigger_service_reinstall()
    except Exception as exc:
        logger.exception("Bot self-update failed.")
        if app_context.update_progress is not None:
            app_context.update_progress.in_progress = False
            app_context.update_progress.percent = 100
            app_context.update_progress.status_text = "Update failed"
            app_context.update_progress.error_text = str(exc)
        with suppress(Exception):
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )


async def _perform_whisper_install(
    app_context: AppContext,
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
) -> None:
    progress = app_context.whisper_progress
    if progress is None:
        return

    try:
        runtime_result = await app_context.queue.submit(
            f"whisper install runtime by {user_id}",
            app_context.runner.install_whisper_runtime,
        )
        if not runtime_result.ok:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Whisper installation failed"
            progress.error_text = (runtime_result.stderr or runtime_result.stdout or "Whisper runtime installation failed.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 65
        progress.status_text = "Downloading the Whisper model"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        model_result = await app_context.queue.submit(
            f"whisper preload by {user_id}",
            app_context.runner.preload_whisper_model,
        )
        if not model_result.ok:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Whisper installation failed"
            progress.error_text = (model_result.stderr or model_result.stdout or "Whisper model download failed.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 90
        progress.status_text = "Finalizing Whisper setup"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        app_context.cached_whisper_state = None
        app_context.whisper_checked_at = 0.0
        app_context.whisper_refresh_task = None
        app_context.cached_environment_status = None
        app_context.environment_checked_at = 0.0
        app_context.environment_refresh_task = None
        app_context.whisper_progress = None
        app_context.flash_message = f"✅ Whisper installed ({app_context.runner.whisper_model_name()})"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )
    except Exception as exc:
        logger.exception("Whisper install failed.")
        if app_context.whisper_progress is not None:
            app_context.whisper_progress.in_progress = False
            app_context.whisper_progress.percent = 100
            app_context.whisper_progress.status_text = "Whisper installation failed"
            app_context.whisper_progress.error_text = str(exc)
        with suppress(Exception):
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )


async def _perform_whisper_delete(
    app_context: AppContext,
    *,
    bot: Bot,
    chat_id: int,
    user_id: int,
) -> None:
    progress = app_context.whisper_progress
    if progress is None:
        return

    try:
        runtime_result = await app_context.queue.submit(
            f"whisper uninstall runtime by {user_id}",
            app_context.runner.uninstall_whisper_runtime,
        )
        if not runtime_result.ok:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Whisper removal failed"
            progress.error_text = (runtime_result.stderr or runtime_result.stdout or "Whisper runtime removal failed.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 70
        progress.status_text = "Removing cached Whisper models"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        cleanup_result = await app_context.queue.submit(
            f"whisper cleanup by {user_id}",
            app_context.runner.cleanup_whisper_models,
        )
        if not cleanup_result.ok:
            progress.in_progress = False
            progress.percent = 100
            progress.status_text = "Whisper removal failed"
            progress.error_text = (cleanup_result.stderr or cleanup_result.stdout or "Whisper cache cleanup failed.")[:600]
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )
            return

        progress.percent = 90
        progress.status_text = "Finalizing Whisper removal"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )

        app_context.cached_whisper_state = None
        app_context.whisper_checked_at = 0.0
        app_context.whisper_refresh_task = None
        app_context.cached_environment_status = None
        app_context.environment_checked_at = 0.0
        app_context.environment_refresh_task = None
        app_context.whisper_progress = None
        app_context.flash_message = "✅ Whisper removed"
        await _refresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=chat_id,
            user_id=user_id,
            page="settings",
        )
    except Exception as exc:
        logger.exception("Whisper delete failed.")
        if app_context.whisper_progress is not None:
            app_context.whisper_progress.in_progress = False
            app_context.whisper_progress.percent = 100
            app_context.whisper_progress.status_text = "Whisper removal failed"
            app_context.whisper_progress.error_text = str(exc)
        with suppress(Exception):
            await _refresh_dashboard_for_chat(
                bot=bot,
                app_context=app_context,
                chat_id=chat_id,
                user_id=user_id,
                page="settings",
            )


async def _sync_admin_label_from_message(message: Message, app_context: AppContext) -> None:
    user_id = message.from_user.id if message.from_user else None
    if user_id is None or user_id not in app_context.config.admin_ids:
        return
    label = _format_user_label(message)
    if app_context.config.admin_labels.get(user_id) == label:
        return
    async with app_context.admin_lock:
        save_admin_label(app_context.config.config_file, user_id, label)
        _refresh_admin_labels(app_context, user_id, label)


def _format_user_label(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "unknown"
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def _render_streaming_message(
    *,
    body: str,
    finished: bool,
    failed: bool = False,
) -> str:
    clipped_body = _clip_stream_text(body)
    if failed:
        return f"Failed to run Codex.\n\n{html.escape(clipped_body)}"
    return html.escape(clipped_body if finished or clipped_body else "Thinking…")


def _render_draft_stream_message(body: str, *, heartbeat_tick: int = 0) -> str:
    rendered = _clip_stream_text(body, limit=3800)
    return _append_stream_keepalive_footer(rendered, heartbeat_tick=heartbeat_tick)


def _render_final_stream_message(*, body: str, failed: bool = False) -> str:
    clipped_body = _clip_stream_text(body)
    if failed:
        return f"Failed to run Codex.\n\n{html.escape(clipped_body)}"
    return html.escape(clipped_body)


def _supports_native_streaming_drafts(message: Message) -> bool:
    chat = getattr(message, "chat", None)
    bot = getattr(message, "bot", None)
    return bool(chat and chat.type == "private" and bot and hasattr(bot, "send_message_draft"))


def _build_stream_draft_id(message: Message) -> int:
    base = int(time.time_ns() & 0x7FFFFFFF)
    fallback = message.message_id if message.message_id > 0 else 1
    return base or fallback


async def _edit_streaming_message(message: Message, text: str, reply_markup: Any | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _clip_stream_text(value: str, *, limit: int = 3000) -> str:
    normalized = value.replace("\x00", "").strip()
    if not normalized:
        return "Thinking…"
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[-limit:]}\n\n[truncated]"


def _append_stream_keepalive_footer(value: str, *, heartbeat_tick: int) -> str:
    if heartbeat_tick <= 0:
        return value
    return f"{value}\n\n{_stream_keepalive_label(heartbeat_tick)}"


def _stream_keepalive_label(heartbeat_tick: int) -> str:
    frames = ("Working.", "Working..", "Working...")
    return frames[(heartbeat_tick - 1) % len(frames)]


def _extract_device_auth_view(raw_output: str | None) -> DeviceAuthView | None:
    if not raw_output:
        return None

    cleaned = _strip_ansi(raw_output)
    url_match = re.search(r"https://[^\s]+", cleaned)
    code_match = re.search(r"\b[A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+\b", cleaned)

    verification_url = url_match.group(0) if url_match else None
    user_code = code_match.group(0) if code_match else None

    remaining = cleaned.strip()
    if verification_url:
        remaining = remaining.replace(verification_url, "").strip()
    if user_code:
        remaining = remaining.replace(user_code, "").strip()

    return DeviceAuthView(
        verification_url=verification_url,
        user_code=user_code,
        raw_text=remaining or None,
    )


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value).replace("[0m", "").replace("[90m", "").replace("[94m", "")


@router.callback_query(F.data.in_({"codexmodel:noop", "thinking:noop"}))
async def legacy_model_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()
