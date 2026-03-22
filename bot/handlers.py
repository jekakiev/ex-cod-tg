from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from bot.codex_runner import AsyncCommandQueue, BotUpdateState, CodexRunner
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
    build_home_text,
    build_model_text,
    build_settings_text,
    build_voice_preview_text,
    build_whisper_text,
    build_workspaces_root_text,
    codex_keyboard,
    home_keyboard,
    model_keyboard,
    settings_keyboard,
    voice_preview_keyboard,
    whisper_keyboard,
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
AVAILABLE_CODEX_MODELS = ["gpt-5.4", "gpt-5", "gpt-5-codex-mini"]
AVAILABLE_THINKING_LEVELS = ["low", "medium", "high"]


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
class PendingVoiceRequest:
    preview_id: str
    owner_user_id: int
    owner_chat_id: int
    prompt_text: str
    source_message_id: int
    preview_message_id: int | None = None
    language: str | None = None


@dataclass(slots=True)
class AppContext:
    config: AppConfig
    runner: CodexRunner
    queue: AsyncCommandQueue
    admin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ui_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dashboards: dict[int, DashboardSession] = field(default_factory=dict)
    pending_admin_request: PendingAdminRequest | None = None
    pending_workspaces_root_request: PendingWorkspacesRootRequest | None = None
    pending_voice_requests: dict[str, PendingVoiceRequest] = field(default_factory=dict)
    codex_login_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    codex_login_session: CodexLoginSession | None = None
    flash_message: str | None = None
    cached_update_state: BotUpdateState | None = None
    update_checked_at: float = 0.0


HELP_TEXT = """
<b>Available commands</b>
<code>/start</code> Open or refresh the main dashboard
<code>/help</code> Show this help message
<code>&lt;plain message&gt;</code> Chat with Codex in the active repository
<code>&lt;voice message&gt;</code> Transcribe locally with Whisper, then approve before running Codex
<code>/model</code> Open the model and thinking selector
<code>/ask &lt;text&gt;</code> Send a prompt to the local Codex CLI
<code>/fix &lt;task&gt;</code> Ask Codex to make a focused code fix
<code>/run &lt;safe command&gt;</code> Run a restricted shell command
<code>/status</code> Refresh the main dashboard
<code>/diff</code> Show the current git diff
<code>/commit &lt;message&gt;</code> Stage all changes and create a git commit
<code>/log</code> Show the latest git commits
<code>/admins</code> Open the admin management screen

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

    await _show_dashboard_from_message(message, app_context, page="home")


@router.message(Command("help"))
async def help_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("status"))
async def status_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    await _show_dashboard_from_message(message, app_context, page="home")


@router.message(Command("admins"))
async def admins_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    await _sync_admin_label_from_message(message, app_context)
    await _show_dashboard_from_message(message, app_context, page="admins")


@router.message(Command("model"))
async def model_command(message: Message, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return
    await _show_dashboard_from_message(message, app_context, page="model")


@router.message(Command("ask"))
async def ask_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer("Usage: <code>/ask explain the failing test</code>")
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /ask from %s: %s", user_id, prompt[:500])

    result = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/ask by {user_id}",
        task_factory=lambda: app_context.runner.run_codex_prompt(prompt),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Codex /ask",
            named_results=[("codex", result)],
            max_output_chars=app_context.config.max_output_chars,
        ),
    )


@router.message(Command("fix"))
async def fix_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    task = (command.args or "").strip()
    if not task:
        await message.answer("Usage: <code>/fix update the failing parser tests</code>")
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /fix from %s: %s", user_id, task[:500])

    prompt = (
        "You are operating through a Telegram bridge on the user's macOS machine. "
        f"The repository root is {app_context.config.working_dir}. "
        "Make the smallest safe production-ready change to solve the task below. "
        "Run relevant checks if possible, avoid unnecessary edits, and end with a short summary.\n\n"
        f"Task: {task}"
    )

    result = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/fix by {user_id}",
        task_factory=lambda: app_context.runner.run_codex_prompt(prompt),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Codex /fix",
            named_results=[("codex", result)],
            max_output_chars=app_context.config.max_output_chars,
        ),
    )


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


@router.message(Command("commit"))
async def commit_command(message: Message, command: CommandObject, app_context: AppContext) -> None:
    if not await _ensure_authorized_message(message, app_context):
        return

    commit_message = (command.args or "").strip()
    if not commit_message:
        await message.answer("Usage: <code>/commit fix login race condition</code>")
        return

    user_id = message.from_user.id if message.from_user else None
    logger.info("Telegram /commit from %s: %s", user_id, commit_message[:200])

    results = await _run_serialized(
        message=message,
        app_context=app_context,
        label=f"/commit by {user_id}",
        task_factory=lambda: app_context.runner.run_commit(commit_message),
    )
    await _send_result_chunks(
        message,
        format_command_results(
            title="Git /commit",
            named_results=results,
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

    page = query.data.split(":", 1)[1]
    await _show_dashboard_from_callback(query, app_context, page=page)
    await query.answer()


@router.callback_query(F.data == "repo:noop")
async def repo_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data.in_({"codexmodel:noop", "thinking:noop"}))
async def model_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "repo:list")
async def repo_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await _show_dashboard_from_callback(query, app_context, page="repos")
    await query.answer()


@router.callback_query(F.data == "branch:list")
async def branch_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await _show_dashboard_from_callback(query, app_context, page="branches")
    await query.answer()


@router.callback_query(F.data == "codexmodel:list")
async def codexmodel_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await _show_dashboard_from_callback(query, app_context, page="models")
    await query.answer()


@router.callback_query(F.data == "thinking:list")
async def thinking_list_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await _show_dashboard_from_callback(query, app_context, page="thinking")
    await query.answer()


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
async def codexmodel_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    current_index = AVAILABLE_CODEX_MODELS.index(app_context.config.codex_model) if app_context.config.codex_model in AVAILABLE_CODEX_MODELS else 0
    direction = -1 if query.data == "codexmodel:prev" else 1
    next_model = AVAILABLE_CODEX_MODELS[(current_index + direction) % len(AVAILABLE_CODEX_MODELS)]
    await _set_codex_preferences(app_context, codex_model=next_model)
    await _show_dashboard_from_callback(query, app_context, page="model")
    await query.answer(f"Model: {next_model}")


@router.callback_query(F.data.in_({"thinking:prev", "thinking:next"}))
async def thinking_cycle_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    current_index = (
        AVAILABLE_THINKING_LEVELS.index(app_context.config.codex_thinking_level)
        if app_context.config.codex_thinking_level in AVAILABLE_THINKING_LEVELS
        else 0
    )
    direction = -1 if query.data == "thinking:prev" else 1
    next_level = AVAILABLE_THINKING_LEVELS[(current_index + direction) % len(AVAILABLE_THINKING_LEVELS)]
    await _set_codex_preferences(app_context, thinking_level=next_level)
    await _show_dashboard_from_callback(query, app_context, page="model")
    await query.answer(f"Thinking: {next_level}")


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
async def codexmodel_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid model.", show_alert=True)
        return

    index = int(raw_index)
    if index < 0 or index >= len(AVAILABLE_CODEX_MODELS):
        await query.answer("Model list is out of date.", show_alert=True)
        return

    target_model = AVAILABLE_CODEX_MODELS[index]
    await _set_codex_preferences(app_context, codex_model=target_model)
    await _show_dashboard_from_callback(query, app_context, page="model")
    await query.answer(f"Model: {target_model}")


@router.callback_query(F.data.startswith("thinking:select:"))
async def thinking_select_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return

    raw_index = query.data.rsplit(":", 1)[1]
    if not raw_index.isdigit():
        await query.answer("Invalid thinking level.", show_alert=True)
        return

    index = int(raw_index)
    if index < 0 or index >= len(AVAILABLE_THINKING_LEVELS):
        await query.answer("Thinking list is out of date.", show_alert=True)
        return

    target_level = AVAILABLE_THINKING_LEVELS[index]
    await _set_codex_preferences(app_context, thinking_level=target_level)
    await _show_dashboard_from_callback(query, app_context, page="model")
    await query.answer(f"Thinking: {target_level}")


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

    session.process.kill()
    with suppress(ProcessLookupError):
        await session.process.wait()
    session.completed = True
    session.returncode = session.process.returncode

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

    await _show_dashboard_from_callback(query, app_context, page="codex")
    if result.ok:
        await query.answer("Logged out.")
    else:
        output = (result.stderr or result.stdout or "Logout failed.")[:180]
        await query.answer(output, show_alert=True)


@router.callback_query(F.data == "whisper:noop")
async def whisper_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "whisper:install")
async def whisper_install_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None:
        await query.answer("Unable to install Whisper.", show_alert=True)
        return

    await query.answer("Installing Whisper…")
    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        results = await app_context.queue.submit(
            f"whisper install by {query.from_user.id if query.from_user else 'unknown'}",
            app_context.runner.install_whisper,
        )

    install_ok = all(result.ok for _, result in results)
    app_context.flash_message = (
        f"✅ Whisper installed ({app_context.runner.whisper_model_name()})"
        if install_ok
        else "❌ Whisper installation failed"
    )
    await _show_dashboard_from_callback(query, app_context, page="whisper")


@router.callback_query(F.data == "whisper:delete")
async def whisper_delete_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None:
        await query.answer("Unable to delete Whisper.", show_alert=True)
        return

    await query.answer("Deleting Whisper…")
    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        results = await app_context.queue.submit(
            f"whisper delete by {query.from_user.id if query.from_user else 'unknown'}",
            app_context.runner.uninstall_whisper,
        )

    uninstall_ok = all(result.ok for _, result in results)
    app_context.flash_message = (
        "✅ Whisper removed" if uninstall_ok else "❌ Whisper removal failed"
    )
    await _show_dashboard_from_callback(query, app_context, page="whisper")


@router.callback_query(F.data == "update:noop")
async def update_noop_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    await query.answer()


@router.callback_query(F.data == "update:run")
async def update_run_callback(query: CallbackQuery, app_context: AppContext) -> None:
    if not await _ensure_authorized_callback(query, app_context):
        return
    if query.message is None or query.from_user is None:
        await query.answer("Unable to update the bot.", show_alert=True)
        return

    update_state = await _get_bot_update_state(app_context, force=True)
    if not update_state.update_available:
        await _show_dashboard_from_callback(query, app_context, page="settings")
        await query.answer("No update available.", show_alert=True)
        return

    old_commit = update_state.installed_commit
    new_commit = update_state.latest_commit
    latest_version = update_state.latest_version
    latest_notes = update_state.latest_notes

    await query.answer("Updating bot…")
    async with ChatActionSender.typing(bot=query.bot, chat_id=query.message.chat.id):
        results = await app_context.queue.submit(
            f"bot update by {query.from_user.id}",
            app_context.runner.install_self_update,
        )

    update_ok = all(result.ok for _, result in results)
    app_context.cached_update_state = None
    app_context.update_checked_at = 0.0

    if not update_ok:
        app_context.flash_message = "❌ Bot update failed"
        await _show_dashboard_from_callback(query, app_context, page="settings")
        return

    await _edit_dashboard_message(
        query.message,
        "<b>Updating bot</b>\n\nUpdate installed. Restarting the service now…",
        None,
    )
    await _schedule_post_update_notice(
        app_context,
        chat_id=query.message.chat.id,
        user_id=query.from_user.id,
        old_commit=old_commit,
        new_commit=new_commit,
        version=latest_version,
        notes=latest_notes,
    )
    await app_context.runner.trigger_service_restart()


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

    if await _handle_pending_workspaces_root_message(message, app_context):
        return

    if message.voice is not None:
        await _handle_voice_message(message, app_context)
        return

    text = (message.text or "").strip()
    if not text:
        return

    if text.startswith("/"):
        await message.answer("Unknown command. Use /help or send a plain message for Codex.")
        return

    _clear_pending_voice_requests_for_user(app_context, user_id)
    await _run_codex_chat(message, app_context, text)


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


async def _get_bot_update_state(app_context: AppContext, *, force: bool = False) -> BotUpdateState:
    now = time.monotonic()
    if (
        not force
        and app_context.cached_update_state is not None
        and now - app_context.update_checked_at < UPDATE_CHECK_TTL_SECONDS
    ):
        return app_context.cached_update_state

    update_state = await app_context.runner.collect_bot_update_state()
    app_context.cached_update_state = update_state
    app_context.update_checked_at = now
    return update_state


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


async def _set_codex_preferences(
    app_context: AppContext,
    *,
    codex_model: str | None = None,
    thinking_level: str | None = None,
) -> None:
    update_codex_preferences(
        app_context.config.config_file,
        codex_model=codex_model,
        thinking_level=thinking_level,
    )
    if codex_model is not None:
        object.__setattr__(app_context.config, "codex_model", codex_model)
    if thinking_level is not None:
        object.__setattr__(app_context.config, "codex_thinking_level", thinking_level)


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


async def _run_codex_chat(message: Message, app_context: AppContext, prompt: str) -> None:
    await _run_codex_chat_request(message, app_context, prompt, user_id=message.from_user.id if message.from_user else None)


async def _run_codex_chat_request(
    message: Message,
    app_context: AppContext,
    prompt: str,
    *,
    user_id: int | None,
) -> None:
    logger.info("Telegram chat prompt from %s in %s: %s", user_id, app_context.config.working_dir, prompt[:500])

    jobs_ahead = app_context.queue.jobs_ahead()
    if jobs_ahead > 0:
        await message.answer(
            f"Queued. Jobs ahead: <b>{jobs_ahead}</b>. "
            "This bot runs Codex requests one by one."
        )

    reply = await message.reply(
        _render_streaming_message(
            body="Thinking…",
            finished=False,
        )
    )

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        result = await app_context.queue.submit(
            f"chat by {user_id}",
            lambda: _execute_codex_chat_stream(
                reply=reply,
                prompt=prompt,
                app_context=app_context,
            ),
        )

    if result.result.ok:
        if result.final_text.strip():
            await _edit_streaming_message(
                reply,
                _render_streaming_message(
                    body=result.final_text,
                    finished=True,
                ),
            )
        else:
            await _edit_streaming_message(
                reply,
                _render_streaming_message(
                    body="Completed with no assistant text.",
                    finished=True,
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
        )


async def _handle_voice_message(message: Message, app_context: AppContext) -> None:
    if message.voice is None or message.from_user is None:
        return

    whisper_state = await app_context.runner.collect_whisper_state()
    if not whisper_state.installed:
        await message.reply("Whisper is not installed yet. Open Settings → Whisper and install it first.")
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
) -> Any:
    last_edit_at = 0.0

    async def on_update(text: str) -> None:
        nonlocal last_edit_at
        now = time.monotonic()
        if now - last_edit_at < 0.8:
            return
        await _edit_streaming_message(
            reply,
            _render_streaming_message(
                body=text or "Thinking…",
                finished=False,
            ),
        )
        last_edit_at = now

    return await app_context.runner.run_codex_streaming_prompt(prompt, on_update=on_update)


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
    projects = await _projects_with_active_selection(app_context)
    project_names = [project.name for project in projects]
    active_index = _active_project_index(app_context, projects)
    branch_names, current_branch = await _git_branches_for_active_project(app_context)
    active_branch_index = branch_names.index(current_branch) if current_branch in branch_names else None

    if page == "home":
        environment = await app_context.runner.collect_environment_status(
            active_job=app_context.queue.active_label,
            queued_jobs=app_context.queue.waiting_count,
        )
        auth_state = await app_context.runner.collect_codex_auth_state()
        update_state = await _get_bot_update_state(app_context)
        flash_message = app_context.flash_message
        app_context.flash_message = None
        text = build_home_text(
            environment=environment,
            auth_state=auth_state,
            update_state=update_state,
            active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
            has_active_project=active_index is not None,
            project_count=len(projects),
            showing_repo_list=False,
            showing_branch_list=False,
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
        )

    if page == "repos":
        environment = await app_context.runner.collect_environment_status(
            active_job=app_context.queue.active_label,
            queued_jobs=app_context.queue.waiting_count,
        )
        auth_state = await app_context.runner.collect_codex_auth_state()
        update_state = await _get_bot_update_state(app_context)
        return (
            build_home_text(
                environment=environment,
                auth_state=auth_state,
                update_state=update_state,
                active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
                has_active_project=active_index is not None,
                project_count=len(projects),
                showing_repo_list=True,
                showing_branch_list=False,
            ),
            home_keyboard(
                project_names=project_names,
                active_index=active_index,
                showing_repo_list=True,
                branch_names=branch_names,
                active_branch_index=active_branch_index,
                showing_branch_list=False,
            ),
        )

    if page == "branches":
        environment = await app_context.runner.collect_environment_status(
            active_job=app_context.queue.active_label,
            queued_jobs=app_context.queue.waiting_count,
        )
        auth_state = await app_context.runner.collect_codex_auth_state()
        update_state = await _get_bot_update_state(app_context)
        return (
            build_home_text(
                environment=environment,
                auth_state=auth_state,
                update_state=update_state,
                active_project_name=project_name(app_context.config.working_dir) if active_index is not None else "No repo selected",
                has_active_project=active_index is not None,
                project_count=len(projects),
                showing_repo_list=False,
                showing_branch_list=True,
            ),
            home_keyboard(
                project_names=project_names,
                active_index=active_index,
                showing_repo_list=False,
                branch_names=branch_names,
                active_branch_index=active_branch_index,
                showing_branch_list=True,
            ),
        )

    if page == "model":
        return (
            build_model_text(
                current_model=app_context.config.codex_model,
                current_thinking_level=app_context.config.codex_thinking_level,
                showing_model_list=False,
                showing_thinking_list=False,
            ),
            model_keyboard(
                models=AVAILABLE_CODEX_MODELS,
                active_model_index=AVAILABLE_CODEX_MODELS.index(app_context.config.codex_model)
                if app_context.config.codex_model in AVAILABLE_CODEX_MODELS
                else None,
                thinking_levels=AVAILABLE_THINKING_LEVELS,
                active_thinking_index=AVAILABLE_THINKING_LEVELS.index(app_context.config.codex_thinking_level)
                if app_context.config.codex_thinking_level in AVAILABLE_THINKING_LEVELS
                else None,
                showing_model_list=False,
                showing_thinking_list=False,
            ),
        )

    if page == "models":
        return (
            build_model_text(
                current_model=app_context.config.codex_model,
                current_thinking_level=app_context.config.codex_thinking_level,
                showing_model_list=True,
                showing_thinking_list=False,
            ),
            model_keyboard(
                models=AVAILABLE_CODEX_MODELS,
                active_model_index=AVAILABLE_CODEX_MODELS.index(app_context.config.codex_model)
                if app_context.config.codex_model in AVAILABLE_CODEX_MODELS
                else None,
                thinking_levels=AVAILABLE_THINKING_LEVELS,
                active_thinking_index=AVAILABLE_THINKING_LEVELS.index(app_context.config.codex_thinking_level)
                if app_context.config.codex_thinking_level in AVAILABLE_THINKING_LEVELS
                else None,
                showing_model_list=True,
                showing_thinking_list=False,
            ),
        )

    if page == "thinking":
        return (
            build_model_text(
                current_model=app_context.config.codex_model,
                current_thinking_level=app_context.config.codex_thinking_level,
                showing_model_list=False,
                showing_thinking_list=True,
            ),
            model_keyboard(
                models=AVAILABLE_CODEX_MODELS,
                active_model_index=AVAILABLE_CODEX_MODELS.index(app_context.config.codex_model)
                if app_context.config.codex_model in AVAILABLE_CODEX_MODELS
                else None,
                thinking_levels=AVAILABLE_THINKING_LEVELS,
                active_thinking_index=AVAILABLE_THINKING_LEVELS.index(app_context.config.codex_thinking_level)
                if app_context.config.codex_thinking_level in AVAILABLE_THINKING_LEVELS
                else None,
                showing_model_list=False,
                showing_thinking_list=True,
            ),
        )

    if page == "settings":
        auth_state = await app_context.runner.collect_codex_auth_state()
        whisper_state = await app_context.runner.collect_whisper_state()
        update_state = await _get_bot_update_state(app_context)
        return (
            build_settings_text(
                auth_state=auth_state,
                whisper_state=whisper_state,
                update_state=update_state,
                workspaces_root=app_context.config.workspaces_root,
            ),
            settings_keyboard(
                update_available=update_state.update_available,
                update_busy=bool(app_context.queue.active_label and app_context.queue.active_label.startswith("bot update")),
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
        return (
            build_admins_text(
                admin_items=admin_items,
                candidate=candidate,
                waiting_for_candidate=waiting_for_candidate,
            ),
            admins_keyboard(
                admin_items=admin_items,
                candidate=candidate,
                waiting_for_candidate=waiting_for_candidate,
            ),
        )

    if page == "codex":
        auth_state = await app_context.runner.collect_codex_auth_state()
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

    if page == "whisper":
        whisper_state = await app_context.runner.collect_whisper_state()
        busy = bool(app_context.queue.active_label and app_context.queue.active_label.startswith("whisper "))
        text = build_whisper_text(whisper_state=whisper_state)
        flash_message = app_context.flash_message
        app_context.flash_message = None
        if flash_message:
            text = f"{text}\n\n<blockquote>{flash_message}</blockquote>"
        return text, whisper_keyboard(whisper_state=whisper_state, busy=busy)

    return "Unknown page", home_keyboard(
        project_names=[],
        active_index=None,
        showing_repo_list=False,
        branch_names=[],
        active_branch_index=None,
        showing_branch_list=False,
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


async def _edit_streaming_message(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
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
