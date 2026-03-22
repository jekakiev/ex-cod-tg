from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class WorkspaceProject:
    name: str
    path: Path


def detect_workspaces_root(current_path: Path | None = None) -> Path:
    home = Path.home()
    base_path = (current_path or Path.cwd()).expanduser().resolve(strict=False)

    candidates: list[Path] = []
    seen: set[Path] = set()

    def add_candidate(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.expanduser().resolve(strict=False)
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    add_candidate(_candidate_workspace_parent(base_path))
    add_candidate(base_path)
    add_candidate(home / "workspace")
    add_candidate(home / "Workspace")
    add_candidate(home / "Developer")
    add_candidate(home / "Code")
    add_candidate(home / "Projects")
    add_candidate(home / "Desktop" / "workspace")
    add_candidate(home / "Desktop" / "Workspace")
    add_candidate(home / "Desktop" / "Developer")
    add_candidate(home)

    scored: list[tuple[int, int, Path]] = []
    for candidate in candidates:
        if not candidate.exists() or not candidate.is_dir():
            continue
        repo_count = _count_git_repos(candidate)
        direct_match = 1 if base_path == candidate or base_path.is_relative_to(candidate) else 0
        scored.append((repo_count, direct_match, candidate))

    if scored:
        scored.sort(key=lambda item: (item[0], item[1], _path_priority(item[2])), reverse=True)
        return scored[0][2]

    return base_path.parent if base_path.parent.exists() else base_path


def scan_workspace_projects(root: Path) -> list[WorkspaceProject]:
    resolved_root = root.expanduser().resolve(strict=False)
    if not resolved_root.exists() or not resolved_root.is_dir():
        return []

    projects: dict[Path, WorkspaceProject] = {}

    for child in resolved_root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        projects[child.resolve(strict=False)] = WorkspaceProject(name=child.name, path=child.resolve(strict=False))

    return sorted(projects.values(), key=lambda item: (item.name.lower(), str(item.path)))


def choose_active_project(root: Path, requested_path: Path | None) -> Path:
    resolved_root = root.expanduser().resolve(strict=False)
    requested = requested_path.expanduser().resolve(strict=False) if requested_path else None
    projects = scan_workspace_projects(resolved_root)

    if requested is not None:
        for project in projects:
            if project.path == requested:
                return project.path
        if requested.exists() and requested.is_dir():
            return requested

    if projects:
        return projects[0].path

    return resolved_root


def project_name(path: Path) -> str:
    resolved = path.expanduser().resolve(strict=False)
    return resolved.name or str(resolved)


def _candidate_workspace_parent(path: Path) -> Path | None:
    resolved = path.expanduser().resolve(strict=False)
    if _looks_like_git_repo(resolved):
        return resolved.parent
    return resolved.parent if resolved.parent != resolved else None


def _path_priority(path: Path) -> int:
    text = str(path).lower()
    priorities = [
        "/desktop/workspace",
        "/workspace",
        "/developer",
        "/code",
        "/projects",
    ]
    for index, marker in enumerate(priorities):
        if marker in text:
            return len(priorities) - index
    return 0


def _looks_like_git_repo(path: Path) -> bool:
    git_dir = path / ".git"
    return git_dir.exists()


def _count_git_repos(root: Path) -> int:
    resolved_root = root.expanduser().resolve(strict=False)
    if not resolved_root.exists() or not resolved_root.is_dir():
        return 0

    count = 1 if _looks_like_git_repo(resolved_root) else 0
    for child in resolved_root.iterdir():
        if child.is_dir() and not child.name.startswith(".") and _looks_like_git_repo(child):
            count += 1
    return count
