from __future__ import annotations

from pathlib import Path


DEFAULT_CONFIG_VALUES = {
    "TELEGRAM_BOT_TOKEN": "",
    "ADMIN_IDS": "",
    "ADMIN_LABELS": "",
    "WORKSPACES_ROOT": "",
    "ACTIVE_PROJECT_PATH": "",
    "CODEX_BIN": "codex",
    "CODEX_MODEL": "gpt-5.4",
    "CODEX_SELECTED_MODELS": "gpt-5.4,gpt-5.4-mini",
    "CODEX_THINKING_LEVEL": "high",
    "CODEX_SANDBOX_MODE": "workspace-write",
    "COMMAND_TIMEOUT_SECONDS": "900",
    "SHELL_TIMEOUT_SECONDS": "120",
    "GIT_TIMEOUT_SECONDS": "120",
    "MAX_OUTPUT_CHARS": "20000",
}


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        data[key.strip()] = parse_env_value(raw_value.strip())
    return data


def write_env_file(path: Path, values: dict[str, str]) -> None:
    serialized = {**DEFAULT_CONFIG_VALUES, **values}
    lines = [
        f"TELEGRAM_BOT_TOKEN={format_env_value(serialized['TELEGRAM_BOT_TOKEN'])}",
        f"ADMIN_IDS={format_env_value(serialized['ADMIN_IDS'])}",
        f"ADMIN_LABELS={format_env_value(serialized['ADMIN_LABELS'])}",
        f"WORKSPACES_ROOT={format_env_value(serialized['WORKSPACES_ROOT'])}",
        f"ACTIVE_PROJECT_PATH={format_env_value(serialized['ACTIVE_PROJECT_PATH'])}",
        "",
        "# Optional",
        f"CODEX_BIN={format_env_value(serialized['CODEX_BIN'])}",
        f"CODEX_MODEL={format_env_value(serialized['CODEX_MODEL'])}",
        f"CODEX_SELECTED_MODELS={format_env_value(serialized['CODEX_SELECTED_MODELS'])}",
        f"CODEX_THINKING_LEVEL={format_env_value(serialized['CODEX_THINKING_LEVEL'])}",
        f"CODEX_SANDBOX_MODE={format_env_value(serialized['CODEX_SANDBOX_MODE'])}",
        f"COMMAND_TIMEOUT_SECONDS={format_env_value(serialized['COMMAND_TIMEOUT_SECONDS'])}",
        f"SHELL_TIMEOUT_SECONDS={format_env_value(serialized['SHELL_TIMEOUT_SECONDS'])}",
        f"GIT_TIMEOUT_SECONDS={format_env_value(serialized['GIT_TIMEOUT_SECONDS'])}",
        f"MAX_OUTPUT_CHARS={format_env_value(serialized['MAX_OUTPUT_CHARS'])}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_admin_ids(raw_value: str) -> list[int]:
    result: list[int] = []
    for item in raw_value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        result.append(int(candidate))
    return result


def update_admin_ids(path: Path, admin_ids: list[int]) -> None:
    values = load_env_file(path)
    values["ADMIN_IDS"] = ",".join(str(item) for item in sorted(set(admin_ids)))
    write_env_file(path, values)


def add_admin_id(path: Path, user_id: int, label: str | None = None) -> list[int]:
    current = load_admin_ids(path)
    current.add(user_id)
    values = load_env_file(path)
    values["ADMIN_IDS"] = ",".join(str(item) for item in sorted(current))
    labels = load_admin_labels(path)
    if label:
        labels[user_id] = label
    values["ADMIN_LABELS"] = serialize_admin_labels(labels)
    write_env_file(path, values)
    return sorted(current)


def remove_admin_id(path: Path, user_id: int) -> list[int]:
    current = load_admin_ids(path)
    current.discard(user_id)
    values = load_env_file(path)
    values["ADMIN_IDS"] = ",".join(str(item) for item in sorted(current))
    labels = load_admin_labels(path)
    labels.pop(user_id, None)
    values["ADMIN_LABELS"] = serialize_admin_labels(labels)
    write_env_file(path, values)
    return sorted(current)


def load_admin_ids(path: Path) -> set[int]:
    values = load_env_file(path)
    raw_value = values.get("ADMIN_IDS", "").strip()
    if not raw_value:
        return set()
    return set(parse_admin_ids(raw_value))


def load_admin_labels(path: Path) -> dict[int, str]:
    values = load_env_file(path)
    raw_value = values.get("ADMIN_LABELS", "").strip()
    if not raw_value:
        return {}
    result: dict[int, str] = {}
    for item in raw_value.split(","):
        chunk = item.strip()
        if not chunk or ":" not in chunk:
            continue
        raw_user_id, label = chunk.split(":", 1)
        raw_user_id = raw_user_id.strip()
        label = label.strip()
        if not raw_user_id.isdigit() or not label:
            continue
        result[int(raw_user_id)] = label
    return result


def save_admin_label(path: Path, user_id: int, label: str) -> dict[int, str]:
    values = load_env_file(path)
    labels = load_admin_labels(path)
    labels[user_id] = label
    values["ADMIN_LABELS"] = serialize_admin_labels(labels)
    write_env_file(path, values)
    return labels


def update_workspace_settings(
    path: Path,
    *,
    workspaces_root: Path | None = None,
    active_project_path: Path | None = None,
) -> dict[str, str]:
    values = load_env_file(path)
    if workspaces_root is not None:
        values["WORKSPACES_ROOT"] = str(workspaces_root.expanduser().resolve(strict=False))
    if active_project_path is not None:
        values["ACTIVE_PROJECT_PATH"] = str(active_project_path.expanduser().resolve(strict=False))
    write_env_file(path, values)
    return values


def update_codex_preferences(
    path: Path,
    *,
    codex_model: str | None = None,
    selected_models: list[str] | None = None,
    thinking_level: str | None = None,
    sandbox_mode: str | None = None,
) -> dict[str, str]:
    values = load_env_file(path)
    if codex_model is not None:
        values["CODEX_MODEL"] = codex_model.strip()
    if selected_models is not None:
        values["CODEX_SELECTED_MODELS"] = ",".join(item.strip() for item in selected_models if item.strip())
    if thinking_level is not None:
        values["CODEX_THINKING_LEVEL"] = thinking_level.strip()
    if sandbox_mode is not None:
        values["CODEX_SANDBOX_MODE"] = sandbox_mode.strip()
    write_env_file(path, values)
    return values


def serialize_admin_labels(labels: dict[int, str]) -> str:
    parts: list[str] = []
    for user_id in sorted(labels):
        label = labels[user_id].replace(",", "").replace(":", "").strip()
        if not label:
            continue
        parts.append(f"{user_id}:{label}")
    return ",".join(parts)


def parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        inner = value[1:-1]
        if value[0] == '"':
            return bytes(inner, "utf-8").decode("unicode_escape")
        return inner
    return value


def format_env_value(value: str) -> str:
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:,")
    if value and all(character in safe_chars for character in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
