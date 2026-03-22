from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from bot.app_paths import APP_NAME, AppPaths, get_app_paths
from bot.bootstrap import ensure_configured
from bot.config import AppConfig, ConfigError
from bot.config_store import load_env_file
from bot.launchd import install_launch_agent, restart_launch_agent, uninstall_launch_agent
from bot.main import run_bot
from bot.systemd import install_systemd_service, restart_systemd_service, uninstall_systemd_service
from bot.workspaces import detect_workspaces_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Telegram bridge for a local Codex CLI on macOS and Linux.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="Start the Telegram bot.")
    subparsers.add_parser("configure", help="Run interactive configuration.")
    subparsers.add_parser("doctor", help="Check config, Codex, git, and service state.")

    service_parser = subparsers.add_parser("service", help="Manage the background service.")
    service_subparsers = service_parser.add_subparsers(dest="service_command", required=True)
    service_subparsers.add_parser("install", help="Install and load the background service.")
    service_subparsers.add_parser("restart", help="Restart the background service after updates.")
    service_subparsers.add_parser("uninstall", help="Unload and remove the background service.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = get_app_paths()

    try:
        if args.command in (None, "run"):
            return run_command(paths)
        if args.command == "configure":
            return ensure_configured(paths, force=True)
        if args.command == "doctor":
            return doctor_command(paths)
        if args.command == "service":
            return service_command(paths, args.service_command)
    except KeyboardInterrupt:
        return 130
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    parser.print_help()
    return 1


def run_command(paths: AppPaths) -> int:
    ensure_configured(paths, force=False)
    config = AppConfig.from_file(paths.config_file)
    asyncio.run(run_bot(config, log_file=paths.log_file))
    return 0


def doctor_command(paths: AppPaths) -> int:
    values = load_env_file(paths.config_file)
    token_present = bool(values.get("TELEGRAM_BOT_TOKEN", "").strip())
    admin_ids = values.get("ADMIN_IDS", "").strip()
    workspaces_root_raw = values.get("WORKSPACES_ROOT", "").strip()
    active_project_raw = values.get("ACTIVE_PROJECT_PATH", "").strip() or values.get("WORKING_DIR", "").strip()
    workspaces_root = (
        Path(workspaces_root_raw).expanduser().resolve(strict=False)
        if workspaces_root_raw
        else detect_workspaces_root(Path.cwd())
    )
    active_project = (
        Path(active_project_raw).expanduser().resolve(strict=False)
        if active_project_raw
        else workspaces_root
    )
    codex_bin = values.get("CODEX_BIN", "codex").strip() or "codex"
    codex_path = resolve_executable(codex_bin)
    git_repo = is_git_repo(active_project)
    git_branch = current_branch(active_project) if git_repo else None
    service_installed = paths.service_file.exists()

    print(f"{APP_NAME} doctor")
    print(f"config file: {paths.config_file} [{'present' if paths.config_file.exists() else 'missing'}]")
    print(f"log file: {paths.log_file}")
    print(f"telegram token: {'present' if token_present else 'missing'}")
    print(f"admin ids: {admin_ids or 'empty'}")
    print(
        f"workspaces root: {workspaces_root} "
        f"[{'exists' if workspaces_root.exists() else 'missing'}]"
    )
    print(
        f"active project: {active_project} "
        f"[{'exists' if active_project.exists() else 'missing'}"
        f"{'; git' if git_repo else ''}"
        f"{'; branch=' + git_branch if git_branch else ''}]"
    )
    print(f"codex cli: {codex_path or f'missing ({codex_bin})'}")
    print(
        f"{paths.service_kind} service: "
        f"{'installed at ' + str(paths.service_file) if service_installed else 'not installed'}"
    )
    print(f"executable: {Path(sys.argv[0]).resolve(strict=False)}")

    healthy = token_present and workspaces_root.exists() and active_project.exists() and codex_path is not None
    return 0 if healthy else 1


def service_command(paths: AppPaths, action: str) -> int:
    if action == "install":
        ensure_configured(paths, force=False)
        executable = Path(sys.argv[0]).resolve(strict=False)
        service_path = install_service(paths, executable)
        print(f"Installed {paths.service_kind} service: {service_path}")
        print(f"Logs: {paths.log_file}")
        return 0
    if action == "uninstall":
        removed = uninstall_service(paths)
        if removed:
            print(f"Removed {paths.service_kind} service: {paths.service_file}")
        else:
            print(f"No {paths.service_kind} service was installed.")
        return 0
    if action == "restart":
        restart_service(paths)
        print(f"Restarted {paths.service_kind} service: {paths.service_label}")
        print(f"Logs: {paths.log_file}")
        return 0
    raise RuntimeError(f"Unknown service action: {action}")


def install_service(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind == "launchd":
        return install_launch_agent(paths, executable)
    if paths.service_kind == "systemd":
        return install_systemd_service(paths, executable)
    raise RuntimeError(f"Unsupported service platform: {paths.service_kind}")


def uninstall_service(paths: AppPaths) -> bool:
    if paths.service_kind == "launchd":
        return uninstall_launch_agent(paths)
    if paths.service_kind == "systemd":
        return uninstall_systemd_service(paths)
    raise RuntimeError(f"Unsupported service platform: {paths.service_kind}")


def restart_service(paths: AppPaths) -> None:
    if paths.service_kind == "launchd":
        restart_launch_agent(paths)
        return
    if paths.service_kind == "systemd":
        restart_systemd_service(paths)
        return
    raise RuntimeError(f"Unsupported service platform: {paths.service_kind}")


def resolve_executable(candidate: str) -> str | None:
    candidate_path = Path(candidate).expanduser()
    if candidate_path.is_absolute():
        if candidate_path.exists() and os.access(candidate_path, os.X_OK):
            return str(candidate_path)
        return None
    return shutil.which(candidate)


def is_git_repo(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def current_branch(path: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


if __name__ == "__main__":
    raise SystemExit(main())
