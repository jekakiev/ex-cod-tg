from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path

from bot.app_paths import AppPaths

UPDATE_LABEL = "io.excodtg.updater"


def install_launch_agent(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")

    paths.service_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    _write_launch_agent(
        plist_path=paths.service_file,
        plist={
            "Label": paths.service_label,
            "ProgramArguments": [str(executable), "run"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "WorkingDirectory": str(Path.home()),
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUNBUFFERED": "1",
            },
            "StandardOutPath": str(paths.log_file),
            "StandardErrorPath": str(paths.log_file),
        },
    )
    set_launch_agent_enabled(paths.service_label, enabled=True)
    bootstrap_launch_agent(paths.service_file)
    kickstart_launch_agent(paths.service_label, check=False)
    return paths.service_file


def install_helper_launch_agent(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")
    if paths.helper_service_file is None or paths.helper_service_label is None:
        raise RuntimeError("Helper service is not configured for this platform.")

    paths.service_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    helper_log = paths.helper_log_file or paths.log_file
    _write_launch_agent(
        plist_path=paths.helper_service_file,
        plist={
            "Label": paths.helper_service_label,
            "ProgramArguments": [str(executable), "tray"],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": ["Aqua"],
            "WorkingDirectory": str(Path.home()),
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUNBUFFERED": "1",
            },
            "StandardOutPath": str(helper_log),
            "StandardErrorPath": str(helper_log),
        },
    )
    set_launch_agent_enabled(paths.helper_service_label, enabled=True)
    bootstrap_launch_agent(paths.helper_service_file)
    kickstart_launch_agent(paths.helper_service_label, check=False)
    return paths.helper_service_file


def uninstall_launch_agent(paths: AppPaths) -> bool:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")

    existed = paths.service_file.exists()
    _run_launchctl(["bootout", _gui_domain_target(), str(paths.service_file)], check=False)
    if paths.service_file.exists():
        paths.service_file.unlink()
    return existed


def uninstall_helper_launch_agent(paths: AppPaths) -> bool:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")
    if paths.helper_service_file is None:
        return False

    existed = paths.helper_service_file.exists()
    _run_launchctl(["bootout", _gui_domain_target(), str(paths.helper_service_file)], check=False)
    if paths.helper_service_file.exists():
        paths.helper_service_file.unlink()
    return existed


def restart_launch_agent(paths: AppPaths) -> None:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")
    if not paths.service_file.exists():
        raise RuntimeError("launchd service is not installed.")
    kickstart_launch_agent(paths.service_label, check=True)


def restart_helper_launch_agent(paths: AppPaths) -> None:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")
    if paths.helper_service_file is None or paths.helper_service_label is None:
        raise RuntimeError("Helper service is not configured for this platform.")
    if not paths.helper_service_file.exists():
        raise RuntimeError("helper launchd service is not installed.")
    kickstart_launch_agent(paths.helper_service_label, check=True)


def schedule_service_install_launch_agent(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")

    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    updater_plist = paths.config_dir / "service-install-updater.plist"
    helper_log = paths.helper_log_file or paths.log_file
    _write_launch_agent(
        plist_path=updater_plist,
        plist={
            "Label": UPDATE_LABEL,
            "ProgramArguments": [str(executable), "service", "install"],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Background",
            "WorkingDirectory": str(Path.home()),
            "EnvironmentVariables": {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUNBUFFERED": "1",
            },
            "StandardOutPath": str(helper_log),
            "StandardErrorPath": str(helper_log),
        },
    )
    set_launch_agent_enabled(UPDATE_LABEL, enabled=True)
    bootstrap_launch_agent(updater_plist)
    kickstart_launch_agent(UPDATE_LABEL, check=False)
    return updater_plist


def is_launch_agent_loaded(label: str) -> bool:
    result = _run_launchctl(["print", f"{_gui_domain_target()}/{label}"], check=False)
    return result.returncode == 0


def bootstrap_launch_agent(plist_path: Path) -> None:
    _run_launchctl(["bootout", _gui_domain_target(), str(plist_path)], check=False)
    _run_launchctl(["bootstrap", _gui_domain_target(), str(plist_path)], check=True)


def bootout_launch_agent(plist_path: Path) -> None:
    _run_launchctl(["bootout", _gui_domain_target(), str(plist_path)], check=False)


def kickstart_launch_agent(label: str, *, check: bool) -> None:
    _run_launchctl(["kickstart", "-k", f"{_gui_domain_target()}/{label}"], check=check)


def set_launch_agent_enabled(label: str, *, enabled: bool) -> None:
    action = "enable" if enabled else "disable"
    _run_launchctl([action, f"{_gui_domain_target()}/{label}"], check=False)


def is_launch_agent_enabled(label: str) -> bool:
    result = _run_launchctl(["print-disabled", _gui_domain_target()], check=False)
    if result.returncode != 0:
        return True
    pattern = re.compile(rf'"?{re.escape(label)}"?\s*=>\s*(true|false)', re.IGNORECASE)
    match = pattern.search(result.stdout)
    if not match:
        match = pattern.search(result.stderr)
    if not match:
        return True
    return match.group(1).lower() != "true"


def _write_launch_agent(*, plist_path: Path, plist: dict[object, object]) -> None:
    with plist_path.open("wb") as handle:
        plistlib.dump(plist, handle)


def _gui_domain_target() -> str:
    return f"gui/{os.getuid()}"


def _run_launchctl(arguments: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["launchctl", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("launchctl was not found. This command only works on macOS.") from exc

    if check and result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown launchctl error"
        raise RuntimeError(stderr)
    return result
