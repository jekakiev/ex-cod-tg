from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bot.app_paths import AppPaths


def install_systemd_service(paths: AppPaths, executable: Path) -> Path:
    if paths.service_kind != "systemd":
        raise RuntimeError("systemd service management is only available on Linux.")

    paths.service_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    unit_contents = f"""[Unit]
Description=ex-cod-tg Telegram bridge
After=network-online.target

[Service]
Type=simple
WorkingDirectory={Path.home()}
ExecStart={executable} run
Restart=always
RestartSec=3
Environment=PATH={os.environ.get("PATH", "")}
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:{paths.log_file}
StandardError=append:{paths.log_file}

[Install]
WantedBy=default.target
"""

    paths.service_file.write_text(unit_contents, encoding="utf-8")
    _run_systemctl(["daemon-reload"])
    _run_systemctl(["enable", "--now", paths.service_label])
    return paths.service_file


def uninstall_systemd_service(paths: AppPaths) -> bool:
    if paths.service_kind != "systemd":
        raise RuntimeError("systemd service management is only available on Linux.")

    existed = paths.service_file.exists()
    _run_systemctl(["disable", "--now", paths.service_label], check=False)
    if paths.service_file.exists():
        paths.service_file.unlink()
    _run_systemctl(["daemon-reload"], check=False)
    return existed


def restart_systemd_service(paths: AppPaths) -> None:
    if paths.service_kind != "systemd":
        raise RuntimeError("systemd service management is only available on Linux.")
    if not paths.service_file.exists():
        raise RuntimeError("systemd service is not installed.")
    _run_systemctl(["restart", paths.service_label])


def _run_systemctl(arguments: list[str], *, check: bool = True) -> None:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("systemctl was not found. This command only works on Linux with systemd.") from exc

    if check and result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown systemctl error"
        raise RuntimeError(stderr)
