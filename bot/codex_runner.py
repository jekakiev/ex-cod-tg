from __future__ import annotations

import asyncio
import base64
import codecs
import importlib.metadata
import json
import logging
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from bot.config import AppConfig

from bot.version import APP_VERSION


logger = logging.getLogger(__name__)
WHISPER_MODEL_NAME = "tiny"
WHISPER_PIP_PACKAGE = "faster-whisper"
SELF_REPO_GIT_BASE = "git+https://github.com/jekakiev/ex-cod-tg.git"
GITHUB_COMMITS_API = "https://api.github.com/repos/jekakiev/ex-cod-tg/commits/"
RAW_GITHUB_BASE = "https://raw.githubusercontent.com/jekakiev/ex-cod-tg"
MODEL_ALIAS_MAP = {
    "gpt-5-codex-mini": "gpt-5.4-mini",
}
DEFAULT_SELECTED_MODELS = ("gpt-5.4", "gpt-5.4-mini")
DEFAULT_REASONING_LEVELS = ("low", "medium", "high", "xhigh")


@dataclass(slots=True)
class CommandResult:
    command: str
    cwd: Path
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.exit_code == 0


@dataclass(slots=True)
class EnvironmentStatus:
    workspaces_root: Path
    workspaces_root_exists: bool
    working_dir: Path
    working_dir_exists: bool
    codex_path: str | None
    git_repo: bool
    git_branch: str | None
    latest_commit_summary: str | None
    changed_files_count: int | None
    active_job: str | None
    queued_jobs: int


@dataclass(slots=True)
class CodexModelInfo:
    slug: str
    display_name: str
    default_reasoning_level: str
    supported_reasoning_levels: tuple[str, ...]


def normalize_model_slug(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return normalized
    return MODEL_ALIAS_MAP.get(normalized, normalized)


@dataclass(slots=True)
class CodexAuthState:
    cli_path: str | None
    cli_version: str | None
    logged_in: bool
    auth_mode: str | None
    auth_provider: str | None
    account_name: str | None
    account_email: str | None
    status_summary: str
    raw_status: str

    @property
    def account_summary(self) -> str | None:
        if self.account_name and self.account_email:
            return f"{self.account_name} <{self.account_email}>"
        if self.account_email:
            return self.account_email
        if self.account_name:
            return self.account_name
        return None


@dataclass(slots=True)
class CodexStreamResult:
    result: CommandResult
    final_text: str
    session_id: str | None = None


@dataclass(slots=True)
class CodexStreamEvent:
    kind: str
    text: str
    is_delta: bool


@dataclass(slots=True)
class CodexStreamAccumulator:
    assistant_text: str = ""
    reasoning_text: str = ""

    def apply_event(self, event: CodexStreamEvent) -> str:
        attribute = "reasoning_text" if event.kind == "reasoning" else "assistant_text"
        current = getattr(self, attribute)
        updated = _merge_stream_text(current, event.text, is_delta=event.is_delta)
        if updated != current:
            setattr(self, attribute, updated)
        return self.preview_text

    def apply_raw_line(self, raw_line: str) -> str | None:
        event = _parse_codex_stream_event(raw_line)
        if event is None:
            return None
        return self.apply_event(event)

    @property
    def preview_text(self) -> str:
        return self.preview_text_with_partial("")

    def preview_text_with_partial(self, raw_buffer: str) -> str:
        assistant = self.assistant_text.strip()
        partial_event = _parse_partial_codex_stream_event(raw_buffer)
        if partial_event is not None:
            if partial_event.kind != "reasoning":
                assistant = _merge_stream_text(assistant, partial_event.text, is_delta=False).strip()
                assistant = _stabilize_partial_stream_text(assistant)
        return assistant


@dataclass(slots=True)
class WhisperState:
    installed: bool
    model_name: str
    summary: str
    package_version: str | None = None
    details: str | None = None


@dataclass(slots=True)
class VoiceTranscriptionResult:
    result: CommandResult
    text: str
    language: str | None = None


@dataclass(slots=True)
class BotUpdateState:
    installed_commit: str | None
    latest_commit: str | None
    latest_version: str | None
    latest_summary: str | None
    latest_notes: list[str]
    update_available: bool
    check_ok: bool
    status_summary: str


@dataclass(slots=True)
class _QueueJob:
    label: str
    task_factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]


class AsyncCommandQueue:
    def __init__(self, name: str = "telegram-command-queue") -> None:
        self._queue: asyncio.Queue[_QueueJob] = asyncio.Queue()
        self._active_label: str | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._name = name
        self._logger = logging.getLogger(f"{__name__}.queue")

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name=self._name)

    async def shutdown(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def submit(self, label: str, task_factory: Callable[[], Awaitable[Any]]) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await self._queue.put(_QueueJob(label=label, task_factory=task_factory, future=future))
        return await future

    @property
    def active_label(self) -> str | None:
        return self._active_label

    @property
    def waiting_count(self) -> int:
        return self._queue.qsize()

    def jobs_ahead(self) -> int:
        return self._queue.qsize() + (1 if self._active_label else 0)

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            self._active_label = job.label
            self._logger.info("Started queued job: %s", job.label)
            try:
                result = await job.task_factory()
            except Exception as exc:
                self._logger.exception("Queued job failed: %s", job.label)
                if not job.future.done():
                    job.future.set_exception(exc)
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self._logger.info("Finished queued job: %s", job.label)
                self._active_label = None
                self._queue.task_done()


class CodexRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._logger = logging.getLogger(__name__)

    def codex_path(self) -> str | None:
        return shutil.which(self.config.codex_bin)

    def codex_auth_file(self) -> Path:
        return Path.home() / ".codex" / "auth.json"

    def codex_models_cache_file(self) -> Path:
        return Path.home() / ".codex" / "models_cache.json"

    def whisper_model_name(self) -> str:
        return WHISPER_MODEL_NAME

    def current_executable_path(self) -> Path:
        platform_install_path: Path
        if sys.platform == "darwin":
            platform_install_path = Path.home() / "Library" / "Application Support" / "ex-cod-tg" / "app" / "venv" / "bin" / "ex-cod-tg"
        else:
            data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            platform_install_path = data_home / "ex-cod-tg" / "app" / "venv" / "bin" / "ex-cod-tg"

        candidates: list[Path] = []
        argv0 = Path(sys.argv[0]).expanduser()
        if argv0.name == "ex-cod-tg":
            candidates.append(argv0.resolve(strict=False))

        candidates.append(Path(sys.executable).resolve().parent / "ex-cod-tg")

        which_path = shutil.which("ex-cod-tg")
        if which_path:
            candidates.append(Path(which_path).resolve(strict=False))

        candidates.append((Path.home() / ".local" / "bin" / "ex-cod-tg").resolve(strict=False))
        candidates.append(platform_install_path.resolve(strict=False))

        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate

        return platform_install_path.resolve(strict=False)

    async def collect_codex_auth_state(self) -> CodexAuthState:
        cli_path = self.codex_path()
        cli_version = None
        if cli_path is not None:
            version_result = await self._run_process(
                [self.config.codex_bin, "--version"],
                cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
                timeout=15,
                log_command=False,
            )
            if version_result.ok:
                cli_version = version_result.stdout.strip() or None

        if cli_path is None:
            return CodexAuthState(
                cli_path=None,
                cli_version=None,
                logged_in=False,
                auth_mode=None,
                auth_provider=None,
                account_name=None,
                account_email=None,
                status_summary="CLI not found",
                raw_status="Codex CLI was not found on PATH.",
            )

        status_result = await self._run_process(
            [self.config.codex_bin, "login", "status"],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=15,
            log_command=False,
        )
        raw_status = (status_result.stdout or status_result.stderr).strip()
        logged_in = status_result.ok and "logged in" in raw_status.lower()

        auth_mode, account_name, account_email = self._read_auth_file_metadata()
        provider = self._detect_auth_provider(raw_status, auth_mode)
        status_summary = self._summarize_auth_status(
            raw_status=raw_status,
            logged_in=logged_in,
            auth_provider=provider,
        )

        return CodexAuthState(
            cli_path=cli_path,
            cli_version=cli_version,
            logged_in=logged_in,
            auth_mode=auth_mode,
            auth_provider=provider,
            account_name=account_name,
            account_email=account_email,
            status_summary=status_summary,
            raw_status=raw_status,
        )

    async def collect_bot_update_state(self) -> BotUpdateState:
        installed_commit = self._read_installed_commit()
        latest_commit = await self._read_latest_remote_commit()
        latest_version = await self._read_remote_version(latest_commit) if latest_commit else None
        remote_commit_summary = await self._read_remote_commit_summary(latest_commit) if latest_commit else None
        latest_notes = await self._read_remote_release_notes(latest_commit, latest_version) if latest_commit and latest_version else []
        if latest_version and latest_version == APP_VERSION and remote_commit_summary:
            latest_notes = [remote_commit_summary]
        latest_summary = latest_notes[0] if latest_notes else remote_commit_summary

        if latest_commit is None:
            return BotUpdateState(
                installed_commit=installed_commit,
                latest_commit=None,
                latest_version=None,
                latest_summary=None,
                latest_notes=[],
                update_available=False,
                check_ok=False,
                status_summary="Update check unavailable",
            )

        update_available = bool(installed_commit and installed_commit != latest_commit)
        if installed_commit is None:
            status_summary = "Update check unavailable"
        elif update_available:
            status_summary = f"Update available ({latest_version or latest_commit[:7]})"
        else:
            status_summary = f"Up to date ({latest_version})" if latest_version else "Up to date"

        return BotUpdateState(
            installed_commit=installed_commit,
            latest_commit=latest_commit,
            latest_version=latest_version,
            latest_summary=latest_summary,
            latest_notes=latest_notes,
            update_available=update_available,
            check_ok=installed_commit is not None,
            status_summary=status_summary,
        )

    async def collect_whisper_state(self) -> WhisperState:
        runtime_executable, version, runtime_label = await self._resolve_whisper_runtime()
        if runtime_executable is None:
            return WhisperState(
                installed=False,
                model_name=self.whisper_model_name(),
                summary="Not installed",
                details="Whisper was not found in the bot environment or in system python3.",
            )

        details = None
        if runtime_executable != sys.executable:
            details = f"Using external runtime: {runtime_label or runtime_executable}"
        return WhisperState(
            installed=True,
            model_name=self.whisper_model_name(),
            summary=f"Installed ({self.whisper_model_name()})",
            package_version=version,
            details=details,
        )

    async def collect_environment_status(
        self,
        *,
        active_job: str | None,
        queued_jobs: int,
    ) -> EnvironmentStatus:
        git_repo = False
        git_branch: str | None = None
        latest_commit_summary: str | None = None
        changed_files_count: int | None = None

        if self.config.working_dir_exists:
            probe = await self.run_git_command(
                ["rev-parse", "--is-inside-work-tree"],
                timeout=15,
                log_command=False,
            )
            if probe.ok and probe.stdout.strip() == "true":
                git_repo = True
                branch_result = await self.run_git_command(
                    ["rev-parse", "--abbrev-ref", "HEAD"],
                    timeout=15,
                    log_command=False,
                )
                if branch_result.ok:
                    git_branch = branch_result.stdout.strip() or None
                commit_result = await self.run_git_command(
                    ["log", "-1", "--pretty=%s"],
                    timeout=15,
                    log_command=False,
                )
                if commit_result.ok:
                    latest_commit_summary = commit_result.stdout.strip() or None
                diff_result = await self.run_git_command(
                    ["status", "--porcelain"],
                    timeout=15,
                    log_command=False,
                )
                if diff_result.ok:
                    changed_files_count = len([line for line in diff_result.stdout.splitlines() if line.strip()])

        return EnvironmentStatus(
            workspaces_root=self.config.workspaces_root,
            workspaces_root_exists=self.config.workspaces_root_exists,
            working_dir=self.config.working_dir,
            working_dir_exists=self.config.working_dir_exists,
            codex_path=self.codex_path(),
            git_repo=git_repo,
            git_branch=git_branch,
            latest_commit_summary=latest_commit_summary,
            changed_files_count=changed_files_count,
            active_job=active_job,
            queued_jobs=queued_jobs,
        )

    def collect_model_catalog(self) -> list[CodexModelInfo]:
        cache_file = self.codex_models_cache_file()
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._fallback_model_catalog()

        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return self._fallback_model_catalog()

        catalog: list[CodexModelInfo] = []
        seen: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if item.get("visibility") not in (None, "list"):
                continue
            slug = normalize_model_slug(str(item.get("slug") or "").strip())
            if not slug or slug in seen:
                continue
            display_name = str(item.get("display_name") or slug).strip() or slug
            default_reasoning_level = str(item.get("default_reasoning_level") or "medium").strip() or "medium"
            raw_levels = item.get("supported_reasoning_levels")
            supported_levels: list[str] = []
            if isinstance(raw_levels, list):
                for raw_level in raw_levels:
                    if not isinstance(raw_level, dict):
                        continue
                    effort = str(raw_level.get("effort") or "").strip()
                    if effort:
                        supported_levels.append(effort)
            normalized_levels = tuple(level for level in supported_levels if level) or DEFAULT_REASONING_LEVELS
            catalog.append(
                CodexModelInfo(
                    slug=slug,
                    display_name=display_name,
                    default_reasoning_level=default_reasoning_level,
                    supported_reasoning_levels=normalized_levels,
                )
            )
            seen.add(slug)

        return catalog or self._fallback_model_catalog()

    async def install_whisper_runtime(self) -> CommandResult:
        return await self._run_process(
            [sys.executable, "-m", "pip", "install", "--upgrade", WHISPER_PIP_PACKAGE],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )

    async def preload_whisper_model(self, executable: str | None = None) -> CommandResult:
        runtime_executable = executable or sys.executable
        preload_script = (
            "from faster_whisper import WhisperModel\n"
            f"WhisperModel('{WHISPER_MODEL_NAME}', device='cpu', compute_type='int8')\n"
            "print('ok')\n"
        )
        return await self._run_process(
            [runtime_executable, "-c", preload_script],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )

    async def install_whisper(self) -> list[tuple[str, CommandResult]]:
        install_result = await self.install_whisper_runtime()
        if not install_result.ok:
            return [(f"pip install {WHISPER_PIP_PACKAGE}", install_result)]
        preload_result = await self.preload_whisper_model()
        return [
            (f"pip install {WHISPER_PIP_PACKAGE}", install_result),
            ("download model", preload_result),
        ]

    async def uninstall_whisper_runtime(self) -> CommandResult:
        runtime_executable, _, _ = await self._resolve_whisper_runtime()
        executable = runtime_executable or sys.executable
        return await self._run_process(
            [
                executable,
                "-m",
                "pip",
                "uninstall",
                "-y",
                "faster-whisper",
                "ctranslate2",
                "av",
                "onnxruntime",
                "onnxruntime-silicon",
                "tokenizers",
                "huggingface-hub",
                "hf-xet",
            ],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=600,
        )

    async def cleanup_whisper_models(self) -> CommandResult:
        cleanup_script = (
            "from pathlib import Path\n"
            "import shutil\n"
            "home = Path.home()\n"
            "cache_root = home / '.cache' / 'huggingface' / 'hub'\n"
            "removed = []\n"
            "if cache_root.exists():\n"
            "    for path in cache_root.glob('models--Systran--faster-whisper-*'):\n"
            "        shutil.rmtree(path, ignore_errors=True)\n"
            "        removed.append(path.name)\n"
            "print('\\n'.join(removed))\n"
        )
        return await self._run_process(
            [sys.executable, "-c", cleanup_script],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=120,
        )

    async def uninstall_whisper(self) -> list[tuple[str, CommandResult]]:
        uninstall_result = await self.uninstall_whisper_runtime()
        cleanup_result = await self.cleanup_whisper_models()
        return [
            ("pip uninstall whisper runtime", uninstall_result),
            ("remove cached whisper models", cleanup_result),
        ]

    async def download_self_update_installer(self, ref: str | None = None) -> tuple[CommandResult, Path | None]:
        target_ref = (ref or "main").strip() or "main"
        return await asyncio.to_thread(self._download_update_installer, target_ref)

    async def run_self_update_installer(self, installer_path: Path, ref: str | None = None) -> CommandResult:
        target_ref = (ref or "main").strip() or "main"
        repo_url = f"{SELF_REPO_GIT_BASE}@{target_ref}"
        install_result = await self._run_process(
            [
                "/usr/bin/env",
                "EX_COD_TG_SKIP_SERVICE_INSTALL=1",
                f"EX_COD_TG_REPO_URL={repo_url}",
                "/bin/bash",
                str(installer_path),
            ],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )
        with suppress(OSError):
            installer_path.unlink()
        return install_result

    async def install_self_update(self, ref: str | None = None) -> list[tuple[str, CommandResult]]:
        target_ref = (ref or "main").strip() or "main"
        script_result, installer_path = await self.download_self_update_installer(target_ref)
        if not script_result.ok or installer_path is None:
            return [(f"download install.sh ({target_ref})", script_result)]
        install_result = await self.run_self_update_installer(installer_path, target_ref)
        return [
            (f"download install.sh ({target_ref})", script_result),
            ("run install.sh", install_result),
        ]

    async def trigger_service_reinstall(self) -> None:
        executable = self.current_executable_path()
        if not executable.exists():
            raise RuntimeError(f"CLI executable not found: {executable}")
        if sys.platform == "darwin":
            from bot.app_paths import get_app_paths
            from bot.launchd import schedule_service_install_launch_agent

            await asyncio.to_thread(schedule_service_install_launch_agent, get_app_paths(), executable)
            return
        await asyncio.create_subprocess_exec(
            str(executable),
            "service",
            "install",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    def _download_update_installer(self, ref: str) -> tuple[CommandResult, Path | None]:
        url = f"{RAW_GITHUB_BASE}/{ref}/install.sh"
        started_at = time.perf_counter()
        fd, temp_name = tempfile.mkstemp(prefix="ex-cod-tg-install-", suffix=".sh")
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = response.read()
            temp_path.write_bytes(payload)
            temp_path.chmod(0o755)
            return (
                CommandResult(
                    command=f"download {url}",
                    cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
                    exit_code=0,
                    stdout=str(temp_path),
                    stderr="",
                    duration_seconds=time.perf_counter() - started_at,
                ),
                temp_path,
            )
        except Exception as exc:
            with suppress(OSError):
                temp_path.unlink()
            return (
                CommandResult(
                    command=f"download {url}",
                    cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
                    exit_code=1,
                    stdout="",
                    stderr=str(exc),
                    duration_seconds=time.perf_counter() - started_at,
                    error=str(exc),
                ),
                None,
            )

    async def transcribe_voice_file(self, audio_path: Path) -> VoiceTranscriptionResult:
        if not audio_path.exists():
            result = self._synthetic_error_result(
                [sys.executable, "-c", "transcribe"],
                f"Audio file does not exist: {audio_path}",
                exit_code=2,
            )
            return VoiceTranscriptionResult(result=result, text="")

        runtime_executable, _, _ = await self._resolve_whisper_runtime()
        if runtime_executable is None:
            result = self._synthetic_error_result(
                [sys.executable, "-c", "transcribe"],
                "Whisper is not installed. Open Settings and install it first.",
                exit_code=127,
            )
            return VoiceTranscriptionResult(result=result, text="")

        transcription_script = (
            "import json\n"
            "import sys\n"
            "from faster_whisper import WhisperModel\n"
            "audio_path = sys.argv[1]\n"
            f"model = WhisperModel('{WHISPER_MODEL_NAME}', device='cpu', compute_type='int8')\n"
            "segments, info = model.transcribe(audio_path, beam_size=1, vad_filter=True)\n"
            "text = ' '.join(segment.text.strip() for segment in segments if segment.text.strip()).strip()\n"
            "payload = {'text': text, 'language': getattr(info, 'language', None)}\n"
            "print(json.dumps(payload, ensure_ascii=False))\n"
        )
        result = await self._run_process(
            [runtime_executable, "-c", transcription_script, str(audio_path)],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=900,
        )
        if not result.ok:
            return VoiceTranscriptionResult(result=result, text="")

        try:
            payload = json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return VoiceTranscriptionResult(result=result, text=result.stdout.strip())

        return VoiceTranscriptionResult(
            result=result,
            text=str(payload.get("text") or "").strip(),
            language=str(payload["language"]) if payload.get("language") else None,
        )

    async def _resolve_whisper_runtime(self) -> tuple[str | None, str | None, str | None]:
        candidates: list[tuple[str, str]] = [(sys.executable, "bot environment")]
        system_python = shutil.which("python3")
        if system_python and Path(system_python).resolve(strict=False) != Path(sys.executable).resolve(strict=False):
            candidates.append((system_python, "system python3"))

        seen: set[str] = set()
        for executable, label in candidates:
            if executable in seen:
                continue
            seen.add(executable)
            probe = await self._run_process(
                [
                    executable,
                    "-c",
                    (
                        "import importlib.metadata as metadata; "
                        f"print(metadata.version('{WHISPER_PIP_PACKAGE}'))"
                    ),
                ],
                cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
                timeout=30,
                log_command=False,
            )
            if probe.ok:
                return executable, probe.stdout.strip() or None, label

        return None, None, None

    async def run_codex_prompt(self, prompt: str) -> CommandResult:
        prompt = prompt.strip()
        if not prompt:
            return self._synthetic_error_result(
                [self.config.codex_bin],
                "Prompt is empty.",
                exit_code=2,
            )

        if not self.config.working_dir_exists:
            return self._synthetic_error_result(
                [self.config.codex_bin, prompt],
                f"ACTIVE_PROJECT_PATH does not exist: {self.config.working_dir}",
                exit_code=2,
            )

        if self.codex_path() is None:
            return self._synthetic_error_result(
                [self.config.codex_bin, prompt],
                (
                    f"Codex CLI was not found on PATH. "
                    f"Install it and ensure `{self.config.codex_bin} --version` works."
                ),
                exit_code=127,
            )

        return await self._run_process(
            [self.config.codex_bin, "-m", self.config.codex_model, "-c", f'model_reasoning_effort="{self.config.codex_thinking_level}"', prompt],
            cwd=self.config.working_dir,
            timeout=self.config.command_timeout_seconds,
        )

    async def run_codex_streaming_prompt(
        self,
        prompt: str,
        *,
        image_paths: list[Path] | tuple[Path, ...] | None = None,
        resume_session_id: str | None = None,
        on_update: Callable[[str], Awaitable[None]] | None = None,
    ) -> CodexStreamResult:
        prompt = prompt.strip()
        normalized_image_paths = [Path(path) for path in (image_paths or [])]
        if not prompt:
            result = self._synthetic_error_result(
                [self.config.codex_bin, "exec"],
                "Prompt is empty.",
                exit_code=2,
            )
            return CodexStreamResult(result=result, final_text="")

        if not self.config.working_dir_exists:
            result = self._synthetic_error_result(
                [self.config.codex_bin, "exec", prompt],
                f"ACTIVE_PROJECT_PATH does not exist: {self.config.working_dir}",
                exit_code=2,
            )
            return CodexStreamResult(result=result, final_text="")

        if self.codex_path() is None:
            result = self._synthetic_error_result(
                [self.config.codex_bin, "exec", prompt],
                (
                    f"Codex CLI was not found on PATH. "
                    f"Install it and ensure `{self.config.codex_bin} --version` works."
                ),
                exit_code=127,
            )
            return CodexStreamResult(result=result, final_text="")

        missing_image = next((path for path in normalized_image_paths if not path.exists() or not path.is_file()), None)
        if missing_image is not None:
            result = self._synthetic_error_result(
                [self.config.codex_bin, "exec", "-i", str(missing_image), prompt],
                f"Image file does not exist: {missing_image}",
                exit_code=2,
            )
            return CodexStreamResult(result=result, final_text="")

        if resume_session_id:
            return await self._run_codex_resume_streaming_prompt(
                prompt,
                resume_session_id=resume_session_id,
                on_update=on_update,
            )

        output_file = Path(tempfile.gettempdir()) / f"ex-cod-tg-codex-last-{int(time.time() * 1000)}.txt"
        args = [
            self.config.codex_bin,
            "exec",
            "-m",
            self.config.codex_model,
            "-c",
            f'model_reasoning_effort="{self.config.codex_thinking_level}"',
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--full-auto",
            "-C",
            str(self.config.working_dir),
            "-o",
            str(output_file),
        ]
        for image_path in normalized_image_paths:
            args.extend(["-i", str(image_path)])
        args.append(prompt)
        command_display = shlex.join(args)
        started_at = time.perf_counter()
        started_at_epoch = time.time()
        self._logger.info("Running streaming Codex command in %s: %s", self.config.working_dir, command_display)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.config.working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            result = self._synthetic_error_result(
                args,
                f"Executable not found: {self.config.codex_bin}",
                exit_code=127,
            )
            return CodexStreamResult(result=result, final_text="")

        stderr_lines: list[str] = []
        stream_state = CodexStreamAccumulator()
        raw_stdout_parts: list[str] = []
        stdout_buffer = ""
        stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        session_id: str | None = None
        last_emit_at = 0.0
        last_payload = ""

        async def emit(payload: str, force: bool = False) -> None:
            nonlocal last_emit_at
            nonlocal last_payload
            if on_update is None:
                return
            payload = payload.strip()
            if not payload:
                return
            if payload == last_payload:
                return
            now = time.monotonic()
            if not force and now - last_emit_at < 0.03:
                return
            await on_update(payload)
            last_emit_at = now
            last_payload = payload

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(read_stderr())

        timed_out = False
        try:
            if process.stdout is not None:
                while True:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(128),
                        timeout=self.config.command_timeout_seconds,
                    )
                    if not chunk:
                        break
                    decoded_chunk = stdout_decoder.decode(chunk, final=False)
                    raw_stdout_parts.append(decoded_chunk)
                    stdout_buffer += decoded_chunk

                    lines = stdout_buffer.splitlines()
                    if stdout_buffer.endswith(("\n", "\r")):
                        complete_lines = lines
                        stdout_buffer = ""
                    else:
                        complete_lines = lines[:-1]
                        stdout_buffer = lines[-1] if lines else stdout_buffer

                    for raw_line in complete_lines:
                        if session_id is None:
                            session_id = _extract_session_id_from_stream_line(raw_line)
                        event = _parse_codex_stream_event(raw_line)
                        if event is None:
                            continue
                        preview = stream_state.apply_event(event)
                        if preview:
                            await emit(preview, force=event.kind == "assistant")

                    partial_preview = stream_state.preview_text_with_partial(stdout_buffer)
                    if partial_preview:
                        await emit(partial_preview)

                trailing_chunk = stdout_decoder.decode(b"", final=True)
                if trailing_chunk:
                    raw_stdout_parts.append(trailing_chunk)
                    stdout_buffer += trailing_chunk
                if stdout_buffer:
                    preview = stream_state.apply_raw_line(stdout_buffer)
                    if preview:
                        await emit(preview, force=True)
            await asyncio.wait_for(process.wait(), timeout=self.config.command_timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
        finally:
            with suppress(asyncio.CancelledError):
                await stderr_task

        duration_seconds = time.perf_counter() - started_at
        exit_code = process.returncode if not timed_out else -1
        stdout = "".join(raw_stdout_parts)
        stderr = "".join(stderr_lines)

        final_text = ""
        if output_file.exists():
            with suppress(OSError):
                final_text = output_file.read_text(encoding="utf-8").strip()
            with suppress(OSError):
                output_file.unlink()

        if not final_text:
            final_text = stream_state.assistant_text.strip() or stream_state.preview_text

        if session_id is None and not resume_session_id:
            session_id = await asyncio.to_thread(self._recover_recent_session_id, started_at_epoch)
        if session_id is None and resume_session_id and exit_code == 0 and not timed_out:
            session_id = resume_session_id

        if timed_out:
            stderr = (
                f"Command timed out after {self.config.command_timeout_seconds} seconds.\n\n{stderr}".strip()
                if stderr
                else f"Command timed out after {self.config.command_timeout_seconds} seconds."
            )
        elif exit_code != 0:
            self._logger.warning(
                "Streaming command failed with exit code %s in %s: %s",
                exit_code,
                self.config.working_dir,
                command_display,
            )

        result = CommandResult(
            command=command_display,
            cwd=self.config.working_dir,
            exit_code=exit_code,
            stdout=final_text or stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
        )
        await emit(stream_state.preview_text, force=True)
        return CodexStreamResult(result=result, final_text=final_text, session_id=session_id)

    async def _run_codex_resume_streaming_prompt(
        self,
        prompt: str,
        *,
        resume_session_id: str,
        on_update: Callable[[str], Awaitable[None]] | None,
    ) -> CodexStreamResult:
        args = [
            self.config.codex_bin,
            "exec",
            "resume",
            "-c",
            f'model="{self.config.codex_model}"',
            "-c",
            f'model_reasoning_effort="{self.config.codex_thinking_level}"',
            resume_session_id,
            prompt,
        ]
        command_display = shlex.join(args)
        started_at = time.perf_counter()
        self._logger.info("Running resumed Codex command in %s: %s", self.config.working_dir, command_display)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(self.config.working_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            result = self._synthetic_error_result(
                args,
                f"Executable not found: {self.config.codex_bin}",
                exit_code=127,
            )
            return CodexStreamResult(result=result, final_text="", session_id=resume_session_id)

        stdout_parts: list[str] = []
        stderr_lines: list[str] = []
        stdout_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        last_emit_at = 0.0
        last_payload = ""
        session_id = resume_session_id

        async def emit(payload: str, force: bool = False) -> None:
            nonlocal last_emit_at
            nonlocal last_payload
            if on_update is None:
                return
            payload = payload.strip()
            if not payload:
                return
            if payload == last_payload:
                return
            now = time.monotonic()
            if not force and now - last_emit_at < 0.03:
                return
            await on_update(payload)
            last_emit_at = now
            last_payload = payload

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line.decode("utf-8", errors="replace"))

        stderr_task = asyncio.create_task(read_stderr())
        timed_out = False
        try:
            if process.stdout is not None:
                while True:
                    chunk = await asyncio.wait_for(
                        process.stdout.read(128),
                        timeout=self.config.command_timeout_seconds,
                    )
                    if not chunk:
                        break
                    decoded = stdout_decoder.decode(chunk, final=False)
                    stdout_parts.append(decoded)
                    current_stdout = "".join(stdout_parts)
                    maybe_session_id = _extract_resume_session_id(current_stdout)
                    if maybe_session_id:
                        session_id = maybe_session_id
                    preview = _extract_resume_assistant_text(current_stdout)
                    if preview:
                        await emit(preview)
                trailing = stdout_decoder.decode(b"", final=True)
                if trailing:
                    stdout_parts.append(trailing)
            await asyncio.wait_for(process.wait(), timeout=self.config.command_timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
        finally:
            with suppress(asyncio.CancelledError):
                await stderr_task

        duration_seconds = time.perf_counter() - started_at
        exit_code = process.returncode if not timed_out else -1
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_lines)
        final_text = _extract_resume_assistant_text(stdout).strip()

        if timed_out:
            stderr = (
                f"Command timed out after {self.config.command_timeout_seconds} seconds.\n\n{stderr}".strip()
                if stderr
                else f"Command timed out after {self.config.command_timeout_seconds} seconds."
            )
        elif exit_code != 0:
            self._logger.warning(
                "Resumed command failed with exit code %s in %s: %s",
                exit_code,
                self.config.working_dir,
                command_display,
            )

        result = CommandResult(
            command=command_display,
            cwd=self.config.working_dir,
            exit_code=exit_code,
            stdout=final_text or stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
        )
        await emit(final_text, force=True)
        return CodexStreamResult(result=result, final_text=final_text, session_id=session_id)

    async def run_shell_command(self, args: list[str]) -> CommandResult:
        if not self.config.working_dir_exists:
            return self._synthetic_error_result(
                args,
                f"ACTIVE_PROJECT_PATH does not exist: {self.config.working_dir}",
                exit_code=2,
            )

        return await self._run_process(
            args,
            cwd=self.config.working_dir,
            timeout=self.config.shell_timeout_seconds,
        )

    async def run_git_command(
        self,
        args: list[str],
        *,
        timeout: int | None = None,
        log_command: bool = True,
    ) -> CommandResult:
        if not self.config.working_dir_exists:
            return self._synthetic_error_result(
                ["git", *args],
                f"ACTIVE_PROJECT_PATH does not exist: {self.config.working_dir}",
                exit_code=2,
            )

        return await self._run_process(
            ["git", *args],
            cwd=self.config.working_dir,
            timeout=timeout or self.config.git_timeout_seconds,
            log_command=log_command,
        )

    async def run_codex_logout(self) -> CommandResult:
        return await self._run_process(
            [self.config.codex_bin, "logout"],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=60,
        )

    async def _run_process(
        self,
        args: list[str],
        *,
        cwd: Path,
        timeout: int,
        log_command: bool = True,
    ) -> CommandResult:
        started_at = time.perf_counter()
        command_display = shlex.join(args)

        if log_command:
            self._logger.info("Running command in %s: %s", cwd, command_display)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return self._synthetic_error_result(
                args,
                f"Executable not found: {args[0]}",
                exit_code=127,
            )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            raise

        duration_seconds = time.perf_counter() - started_at
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = process.returncode if not timed_out else -1

        if timed_out:
            stderr = (
                f"Command timed out after {timeout} seconds.\n\n{stderr}".strip()
                if stderr
                else f"Command timed out after {timeout} seconds."
            )
            self._logger.warning("Command timed out in %s: %s", cwd, command_display)
        elif exit_code != 0:
            self._logger.warning(
                "Command failed with exit code %s in %s: %s",
                exit_code,
                cwd,
                command_display,
            )

        return CommandResult(
            command=command_display,
            cwd=cwd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            timed_out=timed_out,
        )

    def _synthetic_error_result(
        self,
        args: list[str],
        message: str,
        *,
        exit_code: int,
    ) -> CommandResult:
        return CommandResult(
            command=shlex.join(args),
            cwd=self.config.working_dir,
            exit_code=exit_code,
            stdout="",
            stderr=message,
            duration_seconds=0.0,
            error=message,
        )

    def _fallback_model_catalog(self) -> list[CodexModelInfo]:
        return [
            CodexModelInfo(
                slug=slug,
                display_name=slug,
                default_reasoning_level="medium",
                supported_reasoning_levels=DEFAULT_REASONING_LEVELS,
            )
            for slug in DEFAULT_SELECTED_MODELS
        ]

    def _read_installed_commit(self) -> str | None:
        try:
            distribution = importlib.metadata.distribution("ex-cod-tg")
        except importlib.metadata.PackageNotFoundError:
            return None

        direct_url_text = distribution.read_text("direct_url.json")
        if not direct_url_text:
            return None
        try:
            payload = json.loads(direct_url_text)
        except json.JSONDecodeError:
            return None
        vcs_info = payload.get("vcs_info") if isinstance(payload, dict) else None
        commit_id = vcs_info.get("commit_id") if isinstance(vcs_info, dict) else None
        return str(commit_id).strip() if commit_id else None

    async def _read_latest_remote_commit(self) -> str | None:
        result = await self._run_process(
            ["git", "ls-remote", "https://github.com/jekakiev/ex-cod-tg.git", "HEAD"],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=30,
            log_command=False,
        )
        if not result.ok:
            return None
        first_field = result.stdout.strip().split(maxsplit=1)[0] if result.stdout.strip() else ""
        return first_field or None

    async def _read_remote_commit_summary(self, commit_sha: str | None) -> str | None:
        if not commit_sha:
            return None
        return await asyncio.to_thread(self._fetch_remote_commit_summary, commit_sha)

    async def _read_remote_version(self, commit_sha: str | None) -> str | None:
        if not commit_sha:
            return None
        return await asyncio.to_thread(self._fetch_remote_version, commit_sha)

    async def _read_remote_release_notes(self, commit_sha: str | None, version: str | None) -> list[str]:
        if not commit_sha or not version:
            return []
        return await asyncio.to_thread(self._fetch_remote_release_notes, commit_sha, version)

    def _fetch_remote_commit_summary(self, commit_sha: str) -> str | None:
        request = urllib.request.Request(
            GITHUB_COMMITS_API + commit_sha,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "ex-cod-tg"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return None

        commit = payload.get("commit") if isinstance(payload, dict) else None
        message = commit.get("message") if isinstance(commit, dict) else None
        if not isinstance(message, str):
            return None
        return message.strip().splitlines()[0].strip() or None

    def _fetch_remote_version(self, commit_sha: str) -> str | None:
        text = self._fetch_raw_text(commit_sha, "bot/version.py")
        if not text:
            return None
        for line in text.splitlines():
            if line.strip().startswith("APP_VERSION"):
                _, _, value = line.partition("=")
                cleaned = value.strip().strip('"').strip("'")
                return cleaned or None
        return None

    def _fetch_remote_release_notes(self, commit_sha: str, version: str) -> list[str]:
        text = self._fetch_raw_text(commit_sha, "CHANGELOG.md")
        if not text:
            return []
        target_header = f"## {version}"
        lines = text.splitlines()
        capture = False
        notes: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == target_header:
                capture = True
                continue
            if capture and stripped.startswith("## "):
                break
            if capture and stripped.startswith("- "):
                notes.append(stripped[2:].strip())
        return notes

    def _fetch_raw_text(self, commit_sha: str, relative_path: str) -> str | None:
        request = urllib.request.Request(
            f"{RAW_GITHUB_BASE}/{commit_sha}/{relative_path}",
            headers={"User-Agent": "ex-cod-tg"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.read().decode("utf-8")
        except (OSError, urllib.error.URLError, UnicodeDecodeError):
            return None

    def _read_auth_file_metadata(self) -> tuple[str | None, str | None, str | None]:
        auth_file = self.codex_auth_file()
        if not auth_file.exists():
            return None, None, None

        try:
            payload = json.loads(auth_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None, None

        auth_mode = payload.get("auth_mode")
        id_token = ((payload.get("tokens") or {}).get("id_token")) if isinstance(payload, dict) else None
        jwt_payload = self._decode_jwt_payload(id_token) if isinstance(id_token, str) else {}
        account_name = jwt_payload.get("name")
        account_email = jwt_payload.get("email")
        return auth_mode, account_name, account_email

    def _decode_jwt_payload(self, token: str) -> dict[str, Any]:
        try:
            segments = token.split(".")
            if len(segments) < 2:
                return {}
            payload = segments[1]
            payload += "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
            value = json.loads(decoded.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def _detect_auth_provider(self, raw_status: str, auth_mode: str | None) -> str | None:
        lowered = raw_status.lower()
        if "chatgpt" in lowered:
            return "ChatGPT"
        if "api key" in lowered:
            return "API key"
        if auth_mode == "chatgpt":
            return "ChatGPT"
        if auth_mode == "apikey":
            return "API key"
        return None

    def _recover_recent_session_id(self, started_at_epoch: float) -> str | None:
        session_index = Path.home() / ".codex" / "session_index.jsonl"
        try:
            lines = session_index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None

        threshold = started_at_epoch - 2.0
        for raw_line in reversed(lines):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            session_id = str(payload.get("id") or "").strip()
            updated_at = str(payload.get("updated_at") or "").strip()
            if not session_id or not updated_at:
                continue
            updated_epoch = _parse_codex_iso_timestamp(updated_at)
            if updated_epoch is None:
                continue
            if updated_epoch >= threshold:
                return session_id
        return None

    def _summarize_auth_status(
        self,
        *,
        raw_status: str,
        logged_in: bool,
        auth_provider: str | None,
    ) -> str:
        if logged_in:
            if auth_provider:
                return f"Logged in via {auth_provider}"
            return "Logged in"
        if raw_status:
            return raw_status
        return "Not logged in"


def _parse_codex_stream_event(raw_line: str) -> CodexStreamEvent | None:
    line = raw_line.strip()
    if not line.startswith("{"):
        return None

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_type = str(payload.get("type", "")).lower()
    normalized_event_type = event_type.replace(".", "_")
    inferred_kind = _infer_stream_kind(payload)
    candidate_values: list[Any] = []

    if "delta" in payload:
        candidate_values.append(payload["delta"])
    if "content" in payload:
        candidate_values.append(payload["content"])
    if "message" in payload:
        candidate_values.append(payload["message"])
    if "item" in payload:
        candidate_values.append(payload["item"])
    if "data" in payload:
        candidate_values.append(payload["data"])
    if "text" in payload:
        candidate_values.append(payload["text"])
    if "part" in payload:
        candidate_values.append(payload["part"])
    if "summary" in payload:
        candidate_values.append(payload["summary"])
    if "reasoning" in payload:
        candidate_values.append(payload["reasoning"])
    if "response" in payload:
        candidate_values.append(payload["response"])
    if "output" in payload:
        candidate_values.append(payload["output"])

    if inferred_kind is None and not any(
        marker in normalized_event_type
        for marker in (
            "delta",
            "message",
            "response",
            "output_text",
            "output_item",
            "reasoning",
            "summary",
            "thinking",
            "completed",
        )
    ):
        return None

    fragments: list[str] = []
    for value in candidate_values:
        _collect_text_fragments(value, fragments)

    joined = "".join(fragments)
    if _is_low_value_stream_text(joined):
        return None

    if inferred_kind is not None:
        kind = inferred_kind
    else:
        kind = "reasoning" if any(marker in normalized_event_type for marker in ("reasoning", "summary", "thinking")) else "assistant"
    return CodexStreamEvent(
        kind=kind,
        text=joined,
        is_delta="delta" in normalized_event_type,
    )


def _parse_partial_codex_stream_event(raw_buffer: str) -> CodexStreamEvent | None:
    buffer = raw_buffer.replace("\x00", "").strip()
    if not buffer.startswith("{"):
        return None

    normalized = buffer.lower()
    if '"type":"agent_message"' in normalized:
        kind = "assistant"
    elif any(marker in normalized for marker in ('"type":"reasoning"', '"type":"summary"', '"type":"thinking"')):
        kind = "reasoning"
    else:
        return None

    marker = '"text":"'
    marker_index = buffer.find(marker)
    if marker_index < 0:
        return None

    fragment = _extract_partial_json_string_fragment(buffer[marker_index + len(marker) :])
    if not fragment:
        return None

    decoded = _decode_partial_json_string_fragment(fragment)
    if _is_low_value_stream_text(decoded):
        return None

    return CodexStreamEvent(kind=kind, text=decoded, is_delta=False)


def _extract_partial_json_string_fragment(value: str) -> str:
    chars: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\":
            chars.append(char)
            escaped = True
            continue
        if char == '"':
            break
        chars.append(char)
    return "".join(chars)


def _decode_partial_json_string_fragment(fragment: str) -> str:
    candidate = fragment
    while candidate:
        try:
            return str(json.loads(f'"{candidate}"'))
        except json.JSONDecodeError:
            shortened = _trim_incomplete_json_escape(candidate)
            if shortened != candidate:
                candidate = shortened
                continue
            candidate = candidate[:-1]
    return ""


def _trim_incomplete_json_escape(value: str) -> str:
    if value.endswith("\\"):
        return value[:-1]
    unicode_match = re.search(r"\\u[0-9a-fA-F]{0,3}$", value)
    if unicode_match:
        return value[: unicode_match.start()]
    return value


def _stabilize_partial_stream_text(value: str) -> str:
    normalized = value.replace("\x00", "").strip()
    if not normalized:
        return ""
    if normalized[-1].isspace() or normalized[-1] in ".!,?:;)]}\"'":
        return normalized
    cutoff = max(normalized.rfind(" "), normalized.rfind("\n"), normalized.rfind("\t"))
    if cutoff >= 24:
        return normalized[:cutoff].rstrip()
    return normalized


def _collect_text_fragments(value: Any, fragments: list[str]) -> None:
    if isinstance(value, str):
        cleaned = value.replace("\x00", "")
        if cleaned.strip():
            fragments.append(cleaned)
        return

    if isinstance(value, list):
        for item in value:
            _collect_text_fragments(item, fragments)
        return

    if not isinstance(value, dict):
        return

    for key, item in value.items():
        normalized_key = str(key).lower()
        if normalized_key in {"id", "index", "type", "role", "status", "name", "model", "usage"}:
            continue
        if normalized_key in {"text", "delta", "content", "message", "output_text", "summary", "reasoning"}:
            _collect_text_fragments(item, fragments)
            continue
        if normalized_key in {"data", "item", "content_part", "part", "response", "output"}:
            _collect_text_fragments(item, fragments)
            continue
        if any(marker in normalized_key for marker in ("text", "delta", "content", "message", "summary", "reasoning")):
            _collect_text_fragments(item, fragments)
            continue
        if isinstance(item, (dict, list)):
            _collect_text_fragments(item, fragments)


def _infer_stream_kind(value: Any) -> str | None:
    if isinstance(value, dict):
        type_value = str(value.get("type", "")).lower().replace(".", "_")
        if any(marker in type_value for marker in ("reasoning", "summary", "thinking")):
            return "reasoning"
        if any(marker in type_value for marker in ("agent_message", "message", "output_text")):
            return "assistant"

        for key in ("item", "data", "response", "output", "content", "message", "part"):
            if key in value:
                inferred = _infer_stream_kind(value[key])
                if inferred is not None:
                    return inferred

        for item in value.values():
            if isinstance(item, (dict, list)):
                inferred = _infer_stream_kind(item)
                if inferred is not None:
                    return inferred
        return None

    if isinstance(value, list):
        for item in value:
            inferred = _infer_stream_kind(item)
            if inferred is not None:
                return inferred

    return None


def _merge_stream_text(current: str, incoming: str, *, is_delta: bool) -> str:
    if not incoming.strip():
        return current
    if not current:
        return incoming
    if is_delta:
        return f"{current}{incoming}"
    if incoming == current:
        return current
    if incoming.startswith(current):
        return incoming
    if current.endswith(incoming):
        return current
    if len(incoming) > len(current):
        return incoming
    return current


def _is_low_value_stream_text(value: str) -> bool:
    normalized = value.replace("\x00", "").strip()
    if not normalized:
        return True
    return normalized.lower() in {
        "thinking",
        "thinking…",
        "thinking...",
        "working",
        "working…",
        "working...",
    }


def _extract_session_id_from_stream_line(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    event_type = str(payload.get("type") or "").strip().lower()
    if event_type != "thread.started":
        return None
    thread_id = str(payload.get("thread_id") or "").strip()
    return thread_id or None


def _parse_codex_iso_timestamp(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _extract_resume_session_id(stdout_text: str) -> str | None:
    match = re.search(r"session id:\s*([0-9a-fA-F-]{8,})", stdout_text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _extract_resume_assistant_text(stdout_text: str) -> str:
    if not stdout_text:
        return ""
    normalized = stdout_text.replace("\r\n", "\n")
    marker = "\ncodex\n"
    marker_index = normalized.find(marker)
    if marker_index < 0:
        return ""
    assistant_section = normalized[marker_index + len(marker) :]
    footer_index = assistant_section.find("\ntokens used\n")
    if footer_index >= 0:
        assistant_section = assistant_section[:footer_index]
    cleaned = assistant_section.strip()
    if not cleaned:
        return ""

    lines = [line.rstrip() for line in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) >= 2 and lines[-1].strip() == lines[-2].strip():
        lines.pop()
    return "\n".join(lines).strip()
