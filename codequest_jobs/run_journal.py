"""Local execution journal for the canonical daily JobReady run.

One JSON file per attempted run under codequest_jobs/output/runs/. The file is
created before any meaningful work and rewritten atomically at every stage
transition. It holds sanitized metadata only: no DSNs, passwords, tokens, or
raw exception dumps. The daily report stays the business summary; this file
records what was attempted, what committed, and where a run stopped.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

JOURNAL_VERSION = 1
REPO = Path(__file__).resolve().parent.parent

STAGES = (
    "preflight",
    "scrape",
    "catalog_plan",
    "catalog_write",
    "validation_select",
    "validation_probe",
    "validation_write",
    "application_sync",
    "report",
)
STAGE_STATES = ("not_started", "running", "completed", "failed", "skipped")
TERMINAL_STATES = ("running", "completed", "failed", "abandoned")
DONE_STATES = ("completed", "skipped")

MESSAGE_LIMIT = 300
STRING_LIMIT = 500
REDACTED = "[redacted]"

_SECRET_ENV_NAMES = (
    "JOB_READY_DATABASE_URL",
    "JOB_READY_APP_DATABASE_URL",
    "CODEQUEST_DATABASE_URL",
    "DATABASE_URL",
    "INDEED_API_KEY",
)
_SECRET_ENV_PATTERN = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|DATABASE_URL|DSN|CREDENTIAL)", re.I)
_REDACTIONS = (
    (re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S+", re.I), "[redacted-dsn]"),
    (re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@'\"]*:[^\s/@'\"]*@\S*", re.I), "[redacted-url]"),
    (re.compile(r"\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?\S+", re.I), "authorization=" + REDACTED),
    (re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{6,}", re.I), "bearer " + REDACTED),
    (re.compile(r"\b(?:password|passwd|pwd)\s*[=:]\s*\S+", re.I), "[redacted-password]"),
    (
        re.compile(r"\b(api[_-]?key|apikey|access[_-]?key|secret|token|access_token|auth_token)\s*[=:]\s*[^\s&]+", re.I),
        r"\1=" + REDACTED,
    ),
)
_FORBIDDEN = ("postgresql://", "postgres://", "password=")

_KNOWN_FAILURES = (
    ("another jobready daily run is active", "RUN_LOCK_HELD"),
    ("catalog target is unreachable", "CATALOG_UNREACHABLE"),
    ("job_ready_database_url is required", "CATALOG_TARGET_MISSING"),
    ("membership sync refused", "MEMBERSHIP_REFUSED"),
    ("report refused", "REPORT_REFUSED"),
)
_CODE_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _secret_values() -> list[str]:
    values: set[str] = set()
    for name, value in os.environ.items():
        if name.upper() not in _SECRET_ENV_NAMES and not _SECRET_ENV_PATTERN.search(name):
            continue
        value = (value or "").strip()
        if len(value) >= 6:
            values.add(value)
        if "://" in value:
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password and len(password) >= 4:
                values.add(password)
    return sorted(values, key=len, reverse=True)


def sanitize_text(value: Any, limit: int = STRING_LIMIT) -> str:
    text = str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    for secret in _secret_values():
        text = text.replace(secret, REDACTED)
    lowered = text.lower()
    if any(token in lowered for token in _FORBIDDEN):
        return REDACTED
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def sanitize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {sanitize_text(key, 100): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return sanitize_text(value)


def failure_code(exc: BaseException, default: str) -> str:
    """Structured code for a failure. Prefers a leading CODE token in SystemExit text."""
    if isinstance(exc, SystemExit):
        if isinstance(exc.code, int) or exc.code is None:
            return f"EXIT_{exc.code or 0}"
        text = str(exc.code).strip()
        first = text.split(" ", 1)[0] if text else ""
        if _CODE_TOKEN.match(first):
            return first
        lowered = text.lower()
        for prefix, code in _KNOWN_FAILURES:
            if lowered.startswith(prefix):
                return code
        return default
    if isinstance(exc, KeyboardInterrupt):
        return "INTERRUPTED"
    return default


def failure_message(exc: BaseException) -> str:
    if isinstance(exc, SystemExit):
        text = "" if exc.code is None else str(exc.code)
    else:
        detail = str(exc)
        text = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return sanitize_text(text, MESSAGE_LIMIT)


def current_git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    return commit


def new_run_id(started: datetime, commit: str | None) -> str:
    """Sortable by start time: 20261012T021500Z_<short commit>_<random>."""
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = commit[:7] if commit else "nogit"
    return f"{stamp}_{short}_{secrets.token_hex(3)}"


def atomic_write_text(path: Path, text: str) -> None:
    """Write a temp file in the same directory, fsync it, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)


def _blank_stage() -> dict[str, Any]:
    return {
        "status": "not_started",
        "started_at": None,
        "finished_at": None,
        "elapsed_sec": None,
        "committed": None,
        "counters": {},
        "skip_reason": None,
        "failure_code": None,
        "failure_message": None,
    }


@dataclass
class StageHandle:
    """Set committed and counters inside a journal.stage() block."""

    committed: bool | None = None
    counters: dict[str, Any] = field(default_factory=dict)


class RunJournal:
    def __init__(self, directory: Path | None, *, run: dict[str, Any], git_commit: str | None) -> None:
        started = _now()
        self.directory = Path(directory) if directory is not None else None
        self.run_id = new_run_id(started, git_commit)
        self.path = self.directory / f"{self.run_id}.json" if self.directory is not None else None
        self._clock: dict[str, float] = {}
        self._run_clock = time.perf_counter()
        self.data: dict[str, Any] = {
            "journal_version": JOURNAL_VERSION,
            "run_id": self.run_id,
            "git_commit": git_commit,
            **run,
            "pid": os.getpid(),
            "started_at": _iso(started),
            "finished_at": None,
            "elapsed_sec": None,
            "terminal_state": "running",
            "failed_stage": None,
            "failure_code": None,
            "failure_message": None,
            "report_path": None,
            "abandoned_previous": [],
            "stages": {name: _blank_stage() for name in STAGES},
        }

    @classmethod
    def start(cls, directory: Path, *, run: dict[str, Any], git_commit: str | None = None) -> "RunJournal":
        """Create and persist the journal before any meaningful work."""
        journal = cls(directory, run=run, git_commit=git_commit if git_commit is not None else current_git_commit())
        journal.write()
        return journal

    @classmethod
    def disabled(cls) -> "RunJournal":
        """In-memory journal for direct run_daily callers. Never touches disk."""
        return cls(None, run={}, git_commit=None)

    def write(self) -> None:
        if self.path is None:
            return
        text = json.dumps(sanitize(self.data), indent=2)
        atomic_write_text(self.path, text)

    def status(self, stage: str) -> str:
        return self.data["stages"][stage]["status"]

    def running_stage(self) -> str | None:
        for name in STAGES:
            if self.status(name) == "running":
                return name
        return None

    def begin(self, stage: str) -> None:
        record = self.data["stages"][stage]
        record.update(status="running", started_at=_iso(_now()), finished_at=None, elapsed_sec=None)
        self._clock[stage] = time.perf_counter()
        self.write()

    def _close(self, stage: str, status: str) -> dict[str, Any]:
        record = self.data["stages"][stage]
        moment = _iso(_now())
        if record["started_at"] is None:
            record["started_at"] = moment
        record["status"] = status
        record["finished_at"] = moment
        started = self._clock.pop(stage, None)
        record["elapsed_sec"] = round(time.perf_counter() - started, 3) if started is not None else 0.0
        return record

    def complete(self, stage: str, *, committed: bool | None = None, **counters: Any) -> None:
        record = self._close(stage, "completed")
        record["committed"] = committed
        record["counters"].update(counters)
        self.write()

    def skip(self, stage: str, reason: str) -> None:
        record = self._close(stage, "skipped")
        record["elapsed_sec"] = 0.0
        record["committed"] = None
        record["skip_reason"] = reason
        self.write()

    def fail(
        self,
        stage: str,
        exc: BaseException,
        *,
        committed: bool | None,
        code: str | None = None,
        counters: dict[str, Any] | None = None,
    ) -> None:
        record = self._close(stage, "failed")
        record["committed"] = committed
        record["failure_code"] = code or failure_code(exc, f"{stage.upper()}_FAILED")
        record["failure_message"] = failure_message(exc)
        if counters:
            record["counters"].update(counters)
        self.write()

    @contextmanager
    def stage(self, name: str, *, committed_on_failure: bool | None = None) -> Iterator[StageHandle]:
        """Mark running, then completed or failed. The failure is always re-raised."""
        handle = StageHandle()
        self.begin(name)
        try:
            yield handle
        except BaseException as exc:
            self.fail(name, exc, committed=committed_on_failure, counters=handle.counters)
            raise
        self.complete(name, committed=handle.committed, **handle.counters)

    def _finish(self, state: str) -> None:
        self.data["terminal_state"] = state
        self.data["finished_at"] = _iso(_now())
        self.data["elapsed_sec"] = round(time.perf_counter() - self._run_clock, 3)

    def finish_completed(self, *, report_path: str | None) -> None:
        """Completed only when every stage completed or was skipped."""
        unfinished = [name for name in STAGES if self.status(name) not in DONE_STATES]
        if unfinished:
            exc = SystemExit("RUN_INCOMPLETE stages not finished: " + ", ".join(unfinished))
            self.finish_failed(exc)
            raise exc
        self.data["report_path"] = report_path
        self._finish("completed")
        self.write()

    def finish_failed(self, exc: BaseException) -> None:
        if self.data["terminal_state"] != "running":
            return
        failed_stage = next((name for name in STAGES if self.status(name) == "failed"), None)
        running = self.running_stage()
        if running is not None:
            self.fail(running, exc, committed=None)
            failed_stage = failed_stage or running
        record = self.data["stages"].get(failed_stage) if failed_stage else None
        self.data["failed_stage"] = failed_stage
        self.data["failure_code"] = (record or {}).get("failure_code") or failure_code(exc, "RUN_FAILED")
        self.data["failure_message"] = (record or {}).get("failure_message") or failure_message(exc)
        self._finish("failed")
        self.write()

    def mark_abandoned_previous(self) -> list[str]:
        """Call only while holding the daily run lock.

        Another journal still in "running" then belongs to a process that died
        without finishing. A stage that was running is marked failed with
        committed=null: whether its transaction committed is unknown.
        """
        if self.directory is None or not self.directory.is_dir():
            return []
        abandoned: list[str] = []
        for path in sorted(self.directory.glob("*.json")):
            if path == self.path:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("terminal_state") != "running":
                continue
            moment = _iso(_now())
            data["terminal_state"] = "abandoned"
            data["abandoned_detected_at"] = moment
            data["abandoned_detected_by"] = self.run_id
            for name, record in (data.get("stages") or {}).items():
                if isinstance(record, dict) and record.get("status") == "running":
                    record.update(
                        status="failed",
                        finished_at=None,
                        committed=None,
                        failure_code="RUN_ABANDONED",
                        failure_message="process ended while this stage was running",
                    )
                    data["failed_stage"] = data.get("failed_stage") or name
            data["failure_code"] = data.get("failure_code") or "RUN_ABANDONED"
            atomic_write_text(path, json.dumps(sanitize(data), indent=2))
            abandoned.append(str(data.get("run_id") or path.stem))
        self.data["abandoned_previous"] = abandoned
        self.write()
        return abandoned
