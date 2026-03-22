from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "ex-cod-tg"
MAC_SERVICE_LABEL = "io.excodtg.bot"
MAC_HELPER_LABEL = "io.excodtg.helper"
LINUX_SERVICE_LABEL = "ex-cod-tg.service"


@dataclass(slots=True, frozen=True)
class AppPaths:
    app_name: str
    platform_name: str
    config_dir: Path
    config_file: Path
    update_notice_file: Path
    logs_dir: Path
    log_file: Path
    helper_log_file: Path | None
    service_dir: Path
    service_file: Path
    service_label: str
    helper_service_file: Path | None
    helper_service_label: str | None
    service_kind: str


def get_app_paths() -> AppPaths:
    home = Path.home()
    platform_name = sys.platform

    if platform_name == "darwin":
        config_dir = home / "Library" / "Application Support" / APP_NAME
        logs_dir = home / "Library" / "Logs" / APP_NAME
        service_dir = home / "Library" / "LaunchAgents"
        return AppPaths(
            app_name=APP_NAME,
            platform_name=platform_name,
            config_dir=config_dir,
            config_file=config_dir / "config.env",
            update_notice_file=config_dir / "update_notice.json",
            logs_dir=logs_dir,
            log_file=logs_dir / "bot.log",
            helper_log_file=logs_dir / "helper.log",
            service_dir=service_dir,
            service_file=service_dir / f"{MAC_SERVICE_LABEL}.plist",
            service_label=MAC_SERVICE_LABEL,
            helper_service_file=service_dir / f"{MAC_HELPER_LABEL}.plist",
            helper_service_label=MAC_HELPER_LABEL,
            service_kind="launchd",
        )

    if platform_name.startswith("linux"):
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
        service_dir = config_home / "systemd" / "user"
        config_dir = config_home / APP_NAME
        logs_dir = state_home / APP_NAME
        return AppPaths(
            app_name=APP_NAME,
            platform_name=platform_name,
            config_dir=config_dir,
            config_file=config_dir / "config.env",
            update_notice_file=config_dir / "update_notice.json",
            logs_dir=logs_dir,
            log_file=logs_dir / "bot.log",
            helper_log_file=None,
            service_dir=service_dir,
            service_file=service_dir / LINUX_SERVICE_LABEL,
            service_label=LINUX_SERVICE_LABEL,
            helper_service_file=None,
            helper_service_label=None,
            service_kind="systemd",
        )

    raise RuntimeError("ex-cod-tg currently supports only macOS and Linux.")
