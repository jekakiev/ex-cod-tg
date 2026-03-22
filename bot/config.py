from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from bot.codex_runner import DEFAULT_SELECTED_MODELS, normalize_model_slug
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
    codex_model: str
    codex_selected_models: tuple[str, ...]
    codex_thinking_level: str
    command_timeout_seconds: int
    shell_timeout_seconds: int
    git_timeout_seconds: int
    max_output_chars: int
    telegram_max_images_per_request: int
    telegram_image_max_bytes: int
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
            "CODEX_MODEL",
            "CODEX_SELECTED_MODELS",
            "CODEX_THINKING_LEVEL",
            "COMMAND_TIMEOUT_SECONDS",
            "SHELL_TIMEOUT_SECONDS",
            "GIT_TIMEOUT_SECONDS",
            "MAX_OUTPUT_CHARS",
            "TELEGRAM_MAX_IMAGES_PER_REQUEST",
            "TELEGRAM_IMAGE_MAX_BYTES",
        ):
            env_value = os.getenv(key)
            if env_value is not None and env_value.strip():
                merged_values[key] = env_value

        default_codex_model, default_thinking_level = _load_default_codex_preferences()
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
            codex_model=normalize_model_slug(
                merged_values.get("CODEX_MODEL", default_codex_model).strip() or default_codex_model
            ),
            codex_selected_models=_parse_selected_models(
                merged_values.get("CODEX_SELECTED_MODELS", ""),
                fallback=(normalize_model_slug(default_codex_model), *DEFAULT_SELECTED_MODELS[1:]),
            ),
            codex_thinking_level=merged_values.get("CODEX_THINKING_LEVEL", default_thinking_level).strip() or default_thinking_level,
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
            telegram_max_images_per_request=_parse_positive_int_from_values(
                merged_values,
                "TELEGRAM_MAX_IMAGES_PER_REQUEST",
                10,
            ),
            telegram_image_max_bytes=_parse_positive_int_from_values(
                merged_values,
                "TELEGRAM_IMAGE_MAX_BYTES",
                20 * 1024 * 1024,
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


def _load_default_codex_preferences() -> tuple[str, str]:
    config_path = Path.home() / ".codex" / "config.toml"
    default_model = "gpt-5.4"
    default_thinking = "high"
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return default_model, default_thinking

    model = payload.get("model")
    thinking = payload.get("model_reasoning_effort")
    if isinstance(model, str) and model.strip():
        default_model = model.strip()
    if isinstance(thinking, str) and thinking.strip():
        default_thinking = thinking.strip()
    return default_model, default_thinking


def _parse_selected_models(raw_value: str, *, fallback: tuple[str, ...]) -> tuple[str, ...]:
    models = tuple(normalize_model_slug(item) for item in raw_value.split(",") if item.strip())
    return models or fallback
