from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import json
import logging
import shlex
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from bot.config import AppConfig


logger = logging.getLogger(__name__)
WHISPER_MODEL_NAME = "tiny"
WHISPER_PIP_PACKAGE = "faster-whisper"
SELF_REPO_URL = "git+https://github.com/jekakiev/ex-cod-tg.git@main"
GITHUB_COMMITS_API = "https://api.github.com/repos/jekakiev/ex-cod-tg/commits/"
RAW_GITHUB_BASE = "https://raw.githubusercontent.com/jekakiev/ex-cod-tg"


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
    whisper_summary: str
    active_job: str | None
    queued_jobs: int


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

    def whisper_model_name(self) -> str:
        return WHISPER_MODEL_NAME

    def current_executable_path(self) -> Path:
        return Path(sys.executable).resolve().parent / "ex-cod-tg"

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
        latest_notes = await self._read_remote_release_notes(latest_commit, latest_version) if latest_commit and latest_version else []
        latest_summary = latest_notes[0] if latest_notes else await self._read_remote_commit_summary(latest_commit) if latest_commit else None

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
        probe = await self._run_process(
            [
                sys.executable,
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
        if not probe.ok:
            return WhisperState(
                installed=False,
                model_name=self.whisper_model_name(),
                summary="Not installed",
                details=(probe.stderr or probe.stdout).strip() or None,
            )

        version = probe.stdout.strip() or None
        return WhisperState(
            installed=True,
            model_name=self.whisper_model_name(),
            summary=f"Installed ({self.whisper_model_name()})",
            package_version=version,
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
        whisper_state = await self.collect_whisper_state()

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
            whisper_summary=whisper_state.summary,
            active_job=active_job,
            queued_jobs=queued_jobs,
        )

    async def install_whisper(self) -> list[tuple[str, CommandResult]]:
        install_result = await self._run_process(
            [sys.executable, "-m", "pip", "install", "--upgrade", WHISPER_PIP_PACKAGE],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )
        if not install_result.ok:
            return [(f"pip install {WHISPER_PIP_PACKAGE}", install_result)]

        preload_script = (
            "from faster_whisper import WhisperModel\n"
            f"WhisperModel('{WHISPER_MODEL_NAME}', device='cpu', compute_type='int8')\n"
            "print('ok')\n"
        )
        preload_result = await self._run_process(
            [sys.executable, "-c", preload_script],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )
        return [
            (f"pip install {WHISPER_PIP_PACKAGE}", install_result),
            ("download model", preload_result),
        ]

    async def uninstall_whisper(self) -> list[tuple[str, CommandResult]]:
        uninstall_result = await self._run_process(
            [
                sys.executable,
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
        cleanup_result = await self._run_process(
            [sys.executable, "-c", cleanup_script],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=120,
        )
        return [
            ("pip uninstall whisper runtime", uninstall_result),
            ("remove cached whisper models", cleanup_result),
        ]

    async def install_self_update(self) -> list[tuple[str, CommandResult]]:
        pip_result = await self._run_process(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-cache-dir", SELF_REPO_URL],
            cwd=self.config.working_dir if self.config.working_dir_exists else Path.home(),
            timeout=1800,
        )
        return [("pip install --upgrade ex-cod-tg", pip_result)]

    async def trigger_service_restart(self) -> None:
        executable = self.current_executable_path()
        if not executable.exists():
            raise RuntimeError(f"CLI executable not found: {executable}")
        await asyncio.create_subprocess_exec(
            str(executable),
            "service",
            "restart",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

    async def transcribe_voice_file(self, audio_path: Path) -> VoiceTranscriptionResult:
        if not audio_path.exists():
            result = self._synthetic_error_result(
                [sys.executable, "-c", "transcribe"],
                f"Audio file does not exist: {audio_path}",
                exit_code=2,
            )
            return VoiceTranscriptionResult(result=result, text="")

        whisper_state = await self.collect_whisper_state()
        if not whisper_state.installed:
            result = self._synthetic_error_result(
                [sys.executable, "-c", "transcribe"],
                "Whisper is not installed. Open Settings -> Whisper and install it first.",
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
            [sys.executable, "-c", transcription_script, str(audio_path)],
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
        on_update: Callable[[str], Awaitable[None]] | None = None,
    ) -> CodexStreamResult:
        prompt = prompt.strip()
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
            prompt,
        ]
        command_display = shlex.join(args)
        started_at = time.perf_counter()
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
        stream_fragments: list[str] = []
        raw_stdout_lines: list[str] = []
        last_emit_at = 0.0

        async def emit(force: bool = False) -> None:
            nonlocal last_emit_at
            if on_update is None:
                return
            now = time.monotonic()
            if not force and now - last_emit_at < 0.8:
                return
            payload = "".join(stream_fragments).strip()
            await on_update(payload)
            last_emit_at = now

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
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=self.config.command_timeout_seconds,
                    )
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    raw_stdout_lines.append(decoded)
                    fragment = _extract_codex_stream_fragment(decoded)
                    if fragment:
                        stream_fragments.append(fragment)
                        await emit()
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
        stdout = "".join(raw_stdout_lines)
        stderr = "".join(stderr_lines)

        final_text = ""
        if output_file.exists():
            with suppress(OSError):
                final_text = output_file.read_text(encoding="utf-8").strip()
            with suppress(OSError):
                output_file.unlink()

        if not final_text:
            final_text = "".join(stream_fragments).strip()

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
        await emit(force=True)
        return CodexStreamResult(result=result, final_text=final_text)

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

    async def run_commit(self, message: str) -> list[tuple[str, CommandResult]]:
        staged = await self.run_git_command(["add", "-A"], timeout=60)
        if not staged.ok:
            return [("git add -A", staged)]

        committed = await self.run_git_command(
            ["commit", "-m", message],
            timeout=self.config.command_timeout_seconds,
        )
        return [("git add -A", staged), ("git commit", committed)]

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


def _extract_codex_stream_fragment(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line.startswith("{"):
        return None

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None

    event_type = str(payload.get("type", "")).lower()
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

    if "delta" not in event_type and "message" not in event_type and "response" not in event_type:
        return None

    fragments: list[str] = []
    for value in candidate_values:
        _collect_text_fragments(value, fragments)

    joined = "".join(fragments).strip()
    return joined or None


def _collect_text_fragments(value: Any, fragments: list[str]) -> None:
    if isinstance(value, str):
        cleaned = value.strip("\n")
        if cleaned:
            fragments.append(cleaned)
        return

    if isinstance(value, list):
        for item in value:
            _collect_text_fragments(item, fragments)
        return

    if not isinstance(value, dict):
        return

    for key, item in value.items():
        if key in {"id", "index", "type", "role", "status", "name", "model", "usage"}:
            continue
        if key in {"text", "delta", "content", "message", "output_text"}:
            _collect_text_fragments(item, fragments)
            continue
        if key in {"data", "item", "content_part", "part"}:
            _collect_text_fragments(item, fragments)
            continue
        if isinstance(item, (dict, list)):
            _collect_text_fragments(item, fragments)
