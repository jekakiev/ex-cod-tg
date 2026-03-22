from __future__ import annotations

import asyncio
import html
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot.app_paths import get_app_paths
from bot.codex_runner import AsyncCommandQueue, CodexRunner
from bot.config import AppConfig, ConfigError
from bot.handlers import AppContext, router, send_fresh_dashboard_for_chat
from bot.update_notice_store import clear_update_notice, load_update_notice


BOT_COMMANDS = [
    BotCommand(command="start", description="Open the main menu"),
    BotCommand(command="help", description="Show available commands"),
    BotCommand(command="ask", description="Send a prompt to Codex"),
    BotCommand(command="fix", description="Ask Codex to fix something"),
    BotCommand(command="run", description="Run a safe shell command"),
    BotCommand(command="diff", description="Show the current git diff"),
    BotCommand(command="commit", description="Create a git commit"),
    BotCommand(command="log", description="Show recent git commits"),
]


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)


async def run_bot(config: AppConfig, *, log_file: Path) -> None:
    paths = get_app_paths()
    configure_logging(log_file)
    queue = AsyncCommandQueue()
    queue.start()

    app_context = AppContext(
        config=config,
        runner=CodexRunner(config),
        queue=queue,
    )

    bot = Bot(
        token=config.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher.workflow_data["app_context"] = app_context

    logging.getLogger(__name__).info("Bot started for active project %s", config.working_dir)

    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await _send_pending_update_notice(bot, app_context, paths.update_notice_file)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await queue.shutdown()
        await bot.session.close()


async def _send_pending_update_notice(bot: Bot, app_context: AppContext, notice_file: Path) -> None:
    notice = load_update_notice(notice_file)
    if notice is None:
        return

    old_commit = notice.old_commit[:7] if notice.old_commit else "unknown"
    new_commit = notice.new_commit[:7] if notice.new_commit else "unknown"
    title = f"<b>Bot updated to {notice.version}</b>" if notice.version else "<b>Bot updated</b>"
    lines = [title, "", f"Commit: <code>{old_commit}</code> → <code>{new_commit}</code>"]
    if notice.notes:
        lines.append("")
        for note in notice.notes:
            lines.append(f"• {html.escape(note)}")
    text = "\n".join(lines)
    try:
        await bot.send_message(notice.chat_id, text)
        await send_fresh_dashboard_for_chat(
            bot=bot,
            app_context=app_context,
            chat_id=notice.chat_id,
            user_id=notice.user_id,
            page="home",
        )
        clear_update_notice(notice_file)
    except Exception:
        logging.getLogger(__name__).exception("Failed to deliver post-update notice.")


def main() -> None:
    paths = get_app_paths()
    try:
        config = AppConfig.from_file(paths.config_file)
        asyncio.run(run_bot(config, log_file=paths.log_file))
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
