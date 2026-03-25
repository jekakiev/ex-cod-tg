from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


NO_GIT_BRANCH_KEY = "__workspace__"


@dataclass(slots=True)
class ConversationSummary:
    request: str = ""
    done: str = ""
    next: str = ""


@dataclass(slots=True)
class BranchConversationState:
    repo_path: str
    branch_name: str
    session_id: str | None = None
    last_seen_head: str | None = None
    codex_sandbox_mode: str | None = None
    summary: ConversationSummary | None = None
    updated_at: str | None = None


class BranchConversationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._states = self._load()

    def get(self, repo_path: Path, branch_name: str | None) -> BranchConversationState | None:
        return self._states.get(self._key(repo_path, branch_name))

    def set(
        self,
        *,
        repo_path: Path,
        branch_name: str | None,
        session_id: str | None,
        last_seen_head: str | None,
        codex_sandbox_mode: str | None,
        summary: ConversationSummary | None,
    ) -> BranchConversationState:
        state = BranchConversationState(
            repo_path=str(repo_path.resolve(strict=False)),
            branch_name=normalize_branch_key(branch_name),
            session_id=session_id or None,
            last_seen_head=last_seen_head or None,
            codex_sandbox_mode=codex_sandbox_mode or None,
            summary=summary,
            updated_at=_utc_now_iso(),
        )
        self._states[self._key(repo_path, branch_name)] = state
        self._persist()
        return state

    def clear(self, repo_path: Path, branch_name: str | None) -> bool:
        key = self._key(repo_path, branch_name)
        removed = self._states.pop(key, None)
        if removed is None:
            return False
        self._persist()
        return True

    def _load(self) -> dict[str, BranchConversationState]:
        if not self.path.exists():
            return {}

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        raw_states = payload.get("states") if isinstance(payload, dict) else None
        if not isinstance(raw_states, list):
            return {}

        states: dict[str, BranchConversationState] = {}
        for item in raw_states:
            if not isinstance(item, dict):
                continue
            repo_path = str(item.get("repo_path") or "").strip()
            branch_name = normalize_branch_key(item.get("branch_name"))
            if not repo_path:
                continue
            raw_summary = item.get("summary")
            summary = None
            if isinstance(raw_summary, dict):
                summary = ConversationSummary(
                    request=str(raw_summary.get("request") or "").strip(),
                    done=str(raw_summary.get("done") or "").strip(),
                    next=str(raw_summary.get("next") or "").strip(),
                )
            state = BranchConversationState(
                repo_path=repo_path,
                branch_name=branch_name,
                session_id=str(item.get("session_id") or "").strip() or None,
                last_seen_head=str(item.get("last_seen_head") or "").strip() or None,
                codex_sandbox_mode=str(item.get("codex_sandbox_mode") or "").strip() or None,
                summary=summary,
                updated_at=str(item.get("updated_at") or "").strip() or None,
            )
            states[self._key(Path(repo_path), branch_name)] = state
        return states

    def _persist(self) -> None:
        payload = {
            "version": 1,
            "states": [asdict(state) for state in sorted(self._states.values(), key=lambda item: (item.repo_path, item.branch_name))],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp_path.replace(self.path)

    @staticmethod
    def _key(repo_path: Path, branch_name: str | None) -> str:
        return f"{repo_path.resolve(strict=False)}::{normalize_branch_key(branch_name)}"


def normalize_branch_key(branch_name: str | None) -> str:
    cleaned = str(branch_name or "").strip()
    return cleaned or NO_GIT_BRANCH_KEY


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
