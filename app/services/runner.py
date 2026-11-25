"""Thread-safe, subprocess-only jobs for the local web application.

Share one manager across browser sessions (for example via the UI's resource
cache). Config dictionaries must already have their paths resolved by the
configuration service. Job artifacts are write-once inputs and bounded captures;
completed jobs are not recovered into memory after an application restart.
"""

from __future__ import annotations

import atexit
import copy
import json
import math
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psutil

_EVENT_LIMIT = 500
_LOG_LIMIT = 128 * 1024
_LINE_LIMIT = 8 * 1024 * 1024
_DISK_LIMIT = 64 * 1024 * 1024
_HISTORY_LIMIT = 50
_CANCEL_GRACE = 2.0
_KILL_GRACE = 0.5


@dataclass
class _Job:
    state: dict
    command: list[str]
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=_EVENT_LIMIT))
    logs: bytearray = field(default_factory=bytearray)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    owned: dict[int, psutil.Process] = field(default_factory=dict)
    protocol_error: str | None = None
    failure: str | None = None
    interrupted: bool = False


class JobManager:
    """Own at most one active job, with detached, deep-copied snapshots.

    ``start`` reserves the slot before launching its background supervisor.
    ``request_cancel`` only requests cooperative cancellation for runs; it never
    signals a run. ``close`` gives it a short grace period, then kills only the
    subprocess and its discovered descendants. A forced run is an error unless
    the backend confirms cancellation with a matching terminal summary.
    """

    def __init__(
        self,
        root: Path,
        *,
        python: str | Path = sys.executable,
        cwd: Path | None = None,
        command_timeout: float = 120,
    ):
        if not math.isfinite(command_timeout) or command_timeout <= 0:
            raise ValueError("command_timeout must be positive and finite")
        self.root = Path(root).resolve()
        self.cwd = Path(cwd).resolve() if cwd is not None else self.root
        self.python = str(python)
        self.command_timeout = command_timeout
        self._lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._jobs: dict[str, _Job] = {}
        self._closed = False
        atexit.register(self.close)

    def start(
        self,
        kind: str,
        *,
        config: dict | None = None,
        input_path: str | Path | None = None,
        config_digest: str = "",
    ) -> str:
        with self._lock:
            if self._closed:
                raise RuntimeError("Job manager is closed")
            if any(job.state["active"] for job in self._jobs.values()):
                raise RuntimeError("A job is already active")
            if kind not in {"run", "validate", "inspect"}:
                raise ValueError(f"Unsupported job kind: {kind}")
            target: str | None = None
            if kind == "inspect":
                if input_path is None:
                    raise ValueError("inspect requires input_path")
                path = Path(input_path)
                target = str((self.root / path).resolve())
                config_text = None
            else:
                if not isinstance(config, dict):
                    raise ValueError(f"{kind} requires a config dictionary")
                # Serialize now: caller mutations after start cannot change this input.
                config_text = json.dumps(config, ensure_ascii=True, allow_nan=False)

            job_id = uuid.uuid4().hex
            directory = self.root / "outputs" / ".web-jobs" / job_id
            directory.mkdir(parents=True, exist_ok=False)
            config_path = None
            if config_text is not None:
                config_path = directory / "config.json"
                with config_path.open("x", encoding="utf-8") as stream:
                    stream.write(config_text)
                target = str(config_path)
            assert target is not None
            command = [self.python, "-u", "-m", "bcflow", kind, target, "--json"]
            if kind == "run":
                command += ["--cancel-file", str(directory / "cancel.request")]
            job = _Job(
                state={
                    "id": job_id,
                    "kind": kind,
                    "status": "running",
                    "active": True,
                    "created_at": datetime.now(UTC).isoformat(),
                    "elapsed_seconds": 0.0,
                    "config_digest": config_digest,
                    "config_path": str(config_path) if config_path else None,
                    "returncode": None,
                    "summary": None,
                    "error": None,
                    "progress": None,
                    "job_dir": str(directory),
                },
                command=command,
            )
            self._persist(job)
            self._jobs[job_id] = job
            while len(self._jobs) > _HISTORY_LIMIT:
                del self._jobs[next(iter(self._jobs))]
            job.thread = threading.Thread(
                target=self._work, args=(job,), name=f"bcflow-{job_id}", daemon=True
            )
            try:
                job.thread.start()
            except RuntimeError as exc:
                job.failure = f"Cannot start job thread: {exc}"
                self._finish(job)
            return job_id

    def snapshot(self, job_id: str | None = None) -> dict | None:
        with self._lock:
            if job_id is None:
                job_id = next(reversed(self._jobs), None)
            job = self._jobs.get(job_id) if job_id is not None else None
            return self._snapshot(job) if job else None

    def history(self) -> list[dict]:
        with self._lock:
            return [self._snapshot(job) for job in reversed(self._jobs.values())]

    def request_cancel(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if not job.state["active"]:
                return
            if job.state["kind"] == "run":
                (Path(job.state["job_dir"]) / "cancel.request").touch(exist_ok=True)
            job.state["status"] = "cancelling"

    def close(self) -> None:
        """Cancel, kill if necessary, drain readers, and reap owned processes."""
        with self._close_lock:
            with self._lock:
                self._closed = True
                active = [job for job in self._jobs.values() if job.state["active"]]
            for job in active:
                try:
                    self.request_cancel(job.state["id"])
                except OSError:
                    pass  # Shutdown must still reap a process if its disk disappeared.
                thread = job.thread
                assert thread is not None
                thread.join(_CANCEL_GRACE)
                if thread.is_alive():
                    self._signal_tree(job, kill=True)
                    thread.join()
            atexit.unregister(self.close)

    def _snapshot(self, job: _Job) -> dict:
        result = copy.deepcopy(job.state)
        result["elapsed_seconds"] = (job.finished or time.monotonic()) - job.started
        result["events"] = copy.deepcopy(list(job.events))
        result["logs"] = bytes(job.logs).decode("utf-8", errors="replace")
        return result

    def _persist(self, job: _Job) -> None:
        # No config contents, output summaries, logs, or backend error strings:
        # those may contain sensitive application/user data.
        metadata = {
            key: job.state[key]
            for key in (
                "id",
                "kind",
                "status",
                "active",
                "created_at",
                "config_digest",
                "config_path",
                "returncode",
                "job_dir",
            )
        }
        metadata["elapsed_seconds"] = (job.finished or time.monotonic()) - job.started
        directory = Path(job.state["job_dir"])
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temporary.replace(directory / "status.json")

    def _log(self, job: _Job, data: bytes) -> None:
        with self._lock:
            job.logs.extend(data[-_LOG_LIMIT:])
            if len(job.logs) > _LOG_LIMIT:
                del job.logs[:-_LOG_LIMIT]

    def _bad_protocol(self, job: _Job, message: str) -> None:
        with self._lock:
            if job.protocol_error is None:
                job.protocol_error = message
                self._log(job, f"\n[protocol] {message}\n".encode())

    @staticmethod
    def _decode(data: bytes) -> dict:
        def reject_constant(value):
            raise ValueError(f"Non-JSON constant: {value}")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        result = json.loads(
            data.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_keys,
        )
        if not isinstance(result, dict):
            raise TypeError("Expected a JSON object")
        return result

    def _event(self, job: _Job, data: bytes) -> None:
        if not data.strip():
            return
        try:
            event = self._decode(data)
            if not isinstance(event.get("event"), str) or not event["event"]:
                raise ValueError("Missing event name")
        except TypeError, ValueError, RecursionError:
            self._bad_protocol(job, "Invalid JSON event on stdout")
            return
        with self._lock:
            if job.state["summary"] is not None:
                self._bad_protocol(job, "Event received after run_finished")
            job.events.append(event)
            if event["event"] == "progress":
                job.state["progress"] = event
            if event["event"] == "run_finished":
                job.state["summary"] = event

    def _read(self, job: _Job, pipe, destination: Path, *, stdout: bool) -> None:
        captured = 0
        pending = bytearray()
        oversized = False
        run = stdout and job.state["kind"] == "run"
        try:
            with destination.open("xb") as output:
                while chunk := pipe.read1(65536):
                    if captured < _DISK_LIMIT:
                        part = chunk[: _DISK_LIMIT - captured]
                        output.write(part)
                        captured += len(part)
                    if not stdout:
                        self._log(job, chunk)
                        continue
                    if not run:
                        if len(pending) + len(chunk) > _LINE_LIMIT:
                            oversized = True
                            pending.clear()
                            self._bad_protocol(
                                job, "JSON document exceeds protocol limit"
                            )
                        if not oversized:
                            pending.extend(chunk)
                        continue

                    # Only LF delimits JSONL; read1 may split UTF-8 and JSON tokens.
                    parts = chunk.split(b"\n")
                    for index, part in enumerate(parts):
                        if not oversized:
                            if len(pending) + len(part) > _LINE_LIMIT:
                                oversized = True
                                pending.clear()
                                self._bad_protocol(
                                    job, "JSON line exceeds protocol limit"
                                )
                            else:
                                pending.extend(part)
                        if index < len(parts) - 1:
                            if not oversized:
                                self._event(job, bytes(pending))
                            pending.clear()
                            oversized = False
                if stdout and not oversized:
                    if run:
                        self._event(job, bytes(pending))
                    else:
                        try:
                            summary = self._decode(bytes(pending))
                        except TypeError, ValueError, RecursionError:
                            self._bad_protocol(job, "Invalid JSON document on stdout")
                        else:
                            with self._lock:
                                job.state["summary"] = summary
        except (OSError, ValueError) as exc:
            with self._lock:
                job.failure = f"Cannot capture subprocess output: {exc}"
            # Keep draining even on a full disk so the child cannot deadlock.
            try:
                while pipe.read1(65536):
                    pass
            except OSError:
                pass
        finally:
            pipe.close()

    def _track_tree(self, job: _Job) -> None:
        with self._lock:
            process = job.process
            known = list(job.owned.values())
        if process is None:
            return
        if not known:
            try:
                known = [psutil.Process(process.pid)]
            except psutil.NoSuchProcess:
                return
        discovered = {p.pid: p for p in known}
        for parent in known:
            try:
                discovered.update((p.pid, p) for p in parent.children(recursive=True))
            except psutil.NoSuchProcess, psutil.AccessDenied:
                pass
        with self._lock:
            job.owned.update(discovered)

    def _signal_tree(self, job: _Job, *, kill: bool) -> None:
        self._track_tree(job)
        with self._lock:
            owned = list(job.owned.values())
            process = job.process
        # psutil Process objects check identity, protecting against PID reuse.
        for child in reversed(owned):
            try:
                child.kill() if kill else child.terminate()
            except psutil.NoSuchProcess, psutil.AccessDenied:
                pass
        if not owned and process is not None and process.poll() is None:
            try:
                process.kill() if kill else process.terminate()
            except OSError:
                pass

    def _work(self, job: _Job) -> None:
        readers = []
        try:
            process = subprocess.Popen(
                job.command,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            with self._lock:
                job.process = process
            self._track_tree(job)
            directory = Path(job.state["job_dir"])
            for pipe, name, stdout in (
                (process.stdout, "stdout.log", True),
                (process.stderr, "stderr.log", False),
            ):
                reader = threading.Thread(
                    target=self._read,
                    args=(job, pipe, directory / name),
                    kwargs={"stdout": stdout},
                    daemon=True,
                )
                reader.start()
                readers.append(reader)
            stop_at = None
            while True:
                self._track_tree(job)
                with self._lock:
                    cancelling = job.state["status"] == "cancelling"
                    timed_out = (
                        job.state["kind"] != "run"
                        and time.monotonic() - job.started > self.command_timeout
                    )
                    if timed_out:
                        job.failure = "Command timed out"
                    stop = job.state["kind"] != "run" and (cancelling or timed_out)
                    if stop and cancelling:
                        job.interrupted = True
                if stop and stop_at is None:
                    self._signal_tree(job, kill=False)
                    stop_at = time.monotonic() + _KILL_GRACE
                if stop_at is not None and time.monotonic() >= stop_at:
                    self._signal_tree(job, kill=True)
                if process.poll() is not None and not any(
                    r.is_alive() for r in readers
                ):
                    break
                time.sleep(0.02)
            process.wait()
        except (OSError, ValueError, RuntimeError, psutil.Error) as exc:
            with self._lock:
                job.failure = f"Cannot execute subprocess: {exc}"
            self._signal_tree(job, kill=True)
            if job.process is not None:
                job.process.wait()
        finally:
            for reader in readers:
                reader.join()
            with self._lock:
                if job.process is not None:
                    job.state["returncode"] = job.process.returncode
                self._finish(job)

    def _finish(self, job: _Job) -> None:
        summary = job.state["summary"]
        code = job.state["returncode"]
        kind = job.state["kind"]
        error = job.failure or job.protocol_error
        status = "error"
        if job.failure:
            pass
        elif job.interrupted and kind != "run":
            # Terminating these commands can leave a partial JSON document.
            status, error = "cancelled", None
        elif error:
            pass
        elif summary is None:
            error = "Missing terminal JSON summary"
        elif kind == "run":
            status = summary.get("status")
            expected = (
                {"ok": 0, "failed": 1, "error": 2, "cancelled": 130}.get(status)
                if isinstance(status, str)
                else None
            )
            if (
                expected is None
                or type(summary.get("exit_code")) is not int
                or summary["exit_code"] != expected
                or code != expected
            ):
                status, error = (
                    "error",
                    "Run summary and process exit code do not match",
                )
            else:
                error = (
                    summary.get("error")
                    if isinstance(summary.get("error"), str)
                    else None
                )
        elif kind == "validate":
            valid = summary.get("valid")
            if type(valid) is not bool or code != (0 if valid else 1):
                error = "Validation summary and process exit code do not match"
            else:
                status = "ok" if valid else "failed"
        elif summary.get("error"):
            error = (
                str(summary["error"])
                if code == 2
                else "Inspection error and process exit code do not match"
            )
        elif code == 0 and isinstance(summary.get("kind"), str):
            status = "ok"
        else:
            error = "Inspection summary and process exit code do not match"
        job.finished = time.monotonic()
        job.state.update(status=status, error=error, active=False)
        try:
            self._persist(job)
        except OSError as exc:
            job.state.update(status="error", error=f"Cannot persist job status: {exc}")
