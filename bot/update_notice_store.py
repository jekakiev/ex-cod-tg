from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class UpdateNotice:
    chat_id: int
    user_id: int
    old_commit: str | None
    new_commit: str | None
    version: str | None
    notes: list[str]


def load_update_notice(path: Path) -> UpdateNotice | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return UpdateNotice(
            chat_id=int(payload["chat_id"]),
            user_id=int(payload["user_id"]),
            old_commit=_string_or_none(payload.get("old_commit")),
            new_commit=_string_or_none(payload.get("new_commit")),
            version=_string_or_none(payload.get("version")),
            notes=_notes_list(payload.get("notes")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_update_notice(path: Path, notice: UpdateNotice) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "chat_id": notice.chat_id,
        "user_id": notice.user_id,
        "old_commit": notice.old_commit,
        "new_commit": notice.new_commit,
        "version": notice.version,
        "notes": notice.notes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_update_notice(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _notes_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _string_or_none(item)
        if text:
            result.append(text)
    return result
