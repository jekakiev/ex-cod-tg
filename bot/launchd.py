from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from bot.app_paths import AppPaths


def install_launch_agent(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")

    paths.service_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    plist = {
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
    }

    with paths.service_file.open("wb") as handle:
        plistlib.dump(plist, handle)

    _run_launchctl(["bootout", f"gui/{os.getuid()}", str(paths.service_file)], check=False)
    _run_launchctl(["bootstrap", f"gui/{os.getuid()}", str(paths.service_file)], check=True)
    _run_launchctl(["kickstart", "-k", f"gui/{os.getuid()}/{paths.service_label}"], check=False)
    return paths.service_file


def uninstall_launch_agent(paths: AppPaths) -> bool:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")

    existed = paths.service_file.exists()
    _run_launchctl(["bootout", f"gui/{os.getuid()}", str(paths.service_file)], check=False)
    if paths.service_file.exists():
        paths.service_file.unlink()
    return existed


def restart_launch_agent(paths: AppPaths) -> None:
    if paths.service_kind != "launchd":
        raise RuntimeError("launchd service management is only available on macOS.")
    if not paths.service_file.exists():
        raise RuntimeError("launchd service is not installed.")
    _run_launchctl(["kickstart", "-k", f"gui/{os.getuid()}/{paths.service_label}"], check=True)


def _run_launchctl(arguments: list[str], *, check: bool) -> None:
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
