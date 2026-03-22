from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from bot.workspaces import choose_active_project, detect_workspaces_root


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


def _parse_admin_ids(raw_value: str) -> frozenset[int]:
    if not raw_value.strip():
        return frozenset()

    admin_ids: set[int] = set()
    for item in raw_value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            admin_ids.add(int(candidate))
        except ValueError as exc:
            raise ConfigError(f"Invalid ADMIN_IDS entry: {candidate!r}") from exc
    return frozenset(admin_ids)


@dataclass(slots=True, frozen=True)
class AppConfig:
    telegram_bot_token: str
    admin_ids: frozenset[int]
    admin_labels: dict[int, str]
    workspaces_root: Path
    active_project_path: Path
    codex_bin: str
    command_timeout_seconds: int
    shell_timeout_seconds: int
    git_timeout_seconds: int
    max_output_chars: int
    config_file: Path = field(repr=False)
    project_root: Path = field(repr=False)

    @classmethod
    def from_file(cls, config_file: Path) -> "AppConfig":
        project_root = Path(__file__).resolve().parents[1]
        file_values = {
            key: str(value)
            for key, value in dotenv_values(config_file).items()
            if value is not None
        }
        merged_values = {**file_values}
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "ADMIN_IDS",
            "ADMIN_LABELS",
            "WORKSPACES_ROOT",
            "ACTIVE_PROJECT_PATH",
            "WORKING_DIR",
            "CODEX_BIN",
            "COMMAND_TIMEOUT_SECONDS",
            "SHELL_TIMEOUT_SECONDS",
            "GIT_TIMEOUT_SECONDS",
            "MAX_OUTPUT_CHARS",
        ):
            env_value = os.getenv(key)
            if env_value is not None and env_value.strip():
                merged_values[key] = env_value

        telegram_bot_token = merged_values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_bot_token:
            raise ConfigError(f"TELEGRAM_BOT_TOKEN is required in {config_file}")

        legacy_working_dir_raw = merged_values.get("WORKING_DIR", "").strip()
        workspaces_root_raw = merged_values.get("WORKSPACES_ROOT", "").strip()
        active_project_raw = merged_values.get("ACTIVE_PROJECT_PATH", "").strip()

        if workspaces_root_raw:
            workspaces_root = Path(workspaces_root_raw).expanduser().resolve(strict=False)
        elif legacy_working_dir_raw:
            legacy_working_dir = Path(legacy_working_dir_raw).expanduser().resolve(strict=False)
            workspaces_root = legacy_working_dir.parent
        else:
            workspaces_root = detect_workspaces_root(project_root)

        requested_active_project = None
        if active_project_raw:
            requested_active_project = Path(active_project_raw)
        elif legacy_working_dir_raw:
            requested_active_project = Path(legacy_working_dir_raw)
        active_project_path = choose_active_project(workspaces_root, requested_active_project)

        return cls(
            telegram_bot_token=telegram_bot_token,
            admin_ids=_parse_admin_ids(merged_values.get("ADMIN_IDS", "")),
            admin_labels=_parse_admin_labels(merged_values.get("ADMIN_LABELS", "")),
            workspaces_root=workspaces_root,
            active_project_path=active_project_path,
            codex_bin=merged_values.get("CODEX_BIN", "codex").strip() or "codex",
            command_timeout_seconds=_parse_positive_int_from_values(
                merged_values,
                "COMMAND_TIMEOUT_SECONDS",
                900,
            ),
            shell_timeout_seconds=_parse_positive_int_from_values(
                merged_values,
                "SHELL_TIMEOUT_SECONDS",
                120,
            ),
            git_timeout_seconds=_parse_positive_int_from_values(
                merged_values,
                "GIT_TIMEOUT_SECONDS",
                120,
            ),
            max_output_chars=_parse_positive_int_from_values(
                merged_values,
                "MAX_OUTPUT_CHARS",
                20000,
            ),
            config_file=config_file,
            project_root=project_root,
        )

    @property
    def working_dir_exists(self) -> bool:
        return self.active_project_path.exists() and self.active_project_path.is_dir()

    @property
    def working_dir(self) -> Path:
        return self.active_project_path

    @property
    def workspaces_root_exists(self) -> bool:
        return self.workspaces_root.exists() and self.workspaces_root.is_dir()

def _parse_positive_int_from_values(values: dict[str, str], name: str, default: int) -> int:
    raw_value = values.get(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero, got {value}")
    return value


def _parse_admin_labels(raw_value: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in raw_value.split(","):
        chunk = item.strip()
        if not chunk or ":" not in chunk:
            continue
        raw_user_id, label = chunk.split(":", 1)
        raw_user_id = raw_user_id.strip()
        label = label.strip()
        if not raw_user_id:
            continue
        try:
            result[int(raw_user_id)] = label
        except ValueError as exc:
            raise ConfigError(f"Invalid ADMIN_LABELS entry: {chunk!r}") from exc
    return result
