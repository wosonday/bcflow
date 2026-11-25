"""Subprocess contract tests without importing the UI or changing its environment."""

from __future__ import annotations

import concurrent.futures
import copy
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from app.services import runner
from app.services.runner import JobManager

_FAKE_MAIN = r"""
import json
import pathlib
import subprocess
import sys
import time

kind, target = sys.argv[1:3]
config = json.loads(pathlib.Path(target).read_text()) if kind != "inspect" else {}
scenario = config.get("scenario", "normal") if kind != "inspect" else pathlib.Path(target).name
cancel = pathlib.Path(sys.argv[sys.argv.index("--cancel-file") + 1]) if kind == "run" else None

def emit(value):
    print(json.dumps(value), flush=True)

def finish(status="ok", code=0):
    emit({"event": "run_finished", "status": status, "exit_code": code,
          "variables": {"precip": "ok"}, "qa": [{"nested": [1, 2]}]})

if scenario == "tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    emit({"event": "progress", "stage": "child", "pid": child.pid})
    while True:
        time.sleep(.02)
if scenario in {"wait", "ignore"}:
    if kind == "run":
        emit({"event": "progress", "stage": "waiting", "variable": "precip"})
    while cancel is None or not cancel.exists() or scenario == "ignore":
        time.sleep(.01)
    # Ensure request_cancel cannot be confused with a completed cancellation.
    time.sleep(.15)
    finish("cancelled", 130)
    sys.exit(130)
if scenario == "no_final":
    if kind == "run":
        emit({"event": "progress", "stage": "loading"})
    sys.exit(0)
if scenario == "bad_json":
    print("this is not JSON", flush=True)
    if kind != "run":
        sys.exit(0)
if scenario == "oversized":
    print("x" * 32768, flush=True)
if scenario == "flood":
    sys.stderr.write("e" * (256 * 1024) + "\nTAIL\n")
    sys.stderr.flush()
if kind == "run":
    emit({"event": "run_started"})
    emit({"event": "progress", "stage": "worker_selection", "workers": 2})
    for number in range(600 if scenario == "flood" else 2):
        emit({"event": "qa", "check": {"number": number}})
    sys.stderr.write("stderr tail\n")
    if scenario == "failed":
        finish("failed", 1)
        sys.exit(1)
    if scenario == "error":
        finish("error", 2)
        sys.exit(2)
    if scenario == "cancel129":
        finish("cancelled", 130)
        sys.exit(129)
    if scenario == "invalid_status":
        emit({"event": "run_finished", "status": [], "exit_code": 0})
    else:
        finish()
    if scenario == "after_final":
        emit({"event": "qa", "check": {}})
    if scenario == "final_wait":
        while not cancel.exists():
            time.sleep(.01)
    sys.exit(1 if scenario == "mismatch" else 0)
if kind == "validate":
    if scenario == "duplicate_keys":
        print('{"valid": false, "valid": true}')
        sys.exit(0)
    if scenario == "non_object":
        print('[]')
        sys.exit(0)
    if scenario == "non_json_constant":
        print('{"valid": true, "n": NaN}')
        sys.exit(0)
    valid = scenario != "invalid"
    print(json.dumps({"valid": valid, "errors": []}, indent=2))
    sys.exit(1 if scenario in {"invalid", "mismatch"} else 0)
if scenario == "missing":
    emit({"error": "Cannot inspect missing input"})
    sys.exit(2)
if scenario == "empty":
    emit({})
else:
    emit({"kind": "table", "path": target})
"""


def wait_for(manager: JobManager, job_id: str, predicate=None, timeout=10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.snapshot(job_id)
        assert state is not None
        if predicate(state) if predicate else not state["active"]:
            return state
        time.sleep(0.01)
    pytest.fail(f"Job did not reach expected state: {manager.snapshot(job_id)}")


@pytest.fixture
def manager(tmp_path):
    # -m bcflow resolves this package from the child's explicit cwd. No PATH,
    # PYTHONPATH, interpreter wrapper, or global environment changes are needed.
    package = tmp_path / "fixture-module" / "bcflow"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(textwrap.dedent(_FAKE_MAIN), encoding="utf-8")
    instance = JobManager(
        tmp_path / "project", python=sys.executable, cwd=package.parent
    )
    try:
        yield instance
    finally:
        instance.close()


def test_api_artifacts_config_immutability_and_snapshots(manager):
    assert manager.snapshot() is None
    assert manager.snapshot("unknown") is None
    assert manager.history() == []
    config = {"scenario": "final_wait", "nested": {"values": [1, 2]}}
    original = copy.deepcopy(config)
    started = time.monotonic()
    job_id = manager.start("run", config=config, config_digest="digest")
    assert time.monotonic() - started < 1
    config["nested"]["values"].append(999)
    state = wait_for(manager, job_id, lambda s: s["summary"] is not None)
    assert state["active"] and state["status"] == "running"
    assert state["returncode"] is None
    assert state["id"] == job_id and state["kind"] == "run"
    assert state["config_digest"] == "digest"
    directory = Path(state["job_dir"])
    assert directory == manager.root / "outputs" / ".web-jobs" / job_id
    config_bytes = Path(state["config_path"]).read_bytes()
    assert json.loads(config_bytes) == original
    state["summary"]["qa"][0]["nested"].append(999)
    state["events"].clear()
    state["progress"]["stage"] = "changed"
    state["status"] = "changed"
    again = manager.snapshot()
    assert again["events"]
    assert again["progress"]["stage"] == "worker_selection"
    assert again["summary"]["qa"][0]["nested"] == [1, 2]
    manager.request_cancel(job_id)
    done = wait_for(manager, job_id)
    assert done["status"] == "ok"  # A request is not proof of cancellation.
    assert done["returncode"] == 0
    assert done["elapsed_seconds"] > 0
    assert "stderr tail" in done["logs"]
    assert Path(done["config_path"]).read_bytes() == config_bytes
    for name in ("stdout.log", "stderr.log", "status.json", "cancel.request"):
        assert (directory / name).is_file()
    metadata = json.loads((directory / "status.json").read_text())
    assert metadata["status"] == "ok" and metadata["active"] is False
    assert metadata["returncode"] == 0
    assert not {"summary", "config", "events", "logs", "error"} & metadata.keys()
    frozen_elapsed = done["elapsed_seconds"]
    assert manager.snapshot()["elapsed_seconds"] == frozen_elapsed
    manager.history()[0]["summary"].clear()
    assert manager.snapshot()["summary"]


def test_duplicate_starts_are_atomic_across_threads_and_kinds(manager):
    def start(_):
        try:
            return manager.start("run", config={"scenario": "wait"})
        except RuntimeError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(start, range(8)))
    assert len([job_id for job_id in ids if job_id]) == 1
    job_id = next(job_id for job_id in ids if job_id)
    with pytest.raises(RuntimeError, match="active"):
        manager.start("inspect", input_path="table")
    with pytest.raises(RuntimeError, match="active"):
        manager.start("validate", config={})
    manager.request_cancel(job_id)
    assert wait_for(manager, job_id)["status"] == "cancelled"
    second = manager.start("validate", config={})
    assert wait_for(manager, second)["status"] == "ok"
    assert [s["id"] for s in manager.history()] == [second, job_id]


def test_cancel_run_is_cooperative_and_confirmed(manager):
    job_id = manager.start("run", config={"scenario": "wait"})
    wait_for(manager, job_id, lambda s: s["progress"] is not None)
    before = time.monotonic()
    manager.request_cancel(job_id)
    assert time.monotonic() - before < 0.5
    state = manager.snapshot(job_id)
    assert state["active"] and state["status"] == "cancelling"
    assert (Path(state["job_dir"]) / "cancel.request").exists()
    manager.request_cancel(job_id)
    state = wait_for(manager, job_id)
    assert state["status"] == "cancelled" and state["returncode"] == 130
    assert state["error"] is None
    manager.request_cancel(job_id)  # Completed cancellation is idempotent.


@pytest.mark.parametrize("kind", ["validate", "inspect"])
def test_non_run_cancel_terminates_instead_of_waiting_for_timeout(manager, kind):
    job_id = manager.start(kind, config={"scenario": "wait"}, input_path="wait")
    manager.request_cancel(job_id)
    state = wait_for(manager, job_id)
    assert state["status"] == "cancelled"
    assert not state["active"] and state["returncode"] is not None
    assert not (Path(state["job_dir"]) / "cancel.request").exists()


@pytest.mark.parametrize(
    "kind,scenario,expected,code",
    [
        ("run", "normal", "ok", 0),
        ("run", "failed", "failed", 1),
        ("run", "error", "error", 2),
        ("run", "no_final", "error", 0),
        ("run", "mismatch", "error", 1),
        ("run", "cancel129", "error", 129),
        ("run", "invalid_status", "error", 0),
        ("run", "bad_json", "error", 0),
        ("run", "after_final", "error", 0),
        ("validate", "normal", "ok", 0),
        ("validate", "invalid", "failed", 1),
        ("validate", "mismatch", "error", 1),
        ("validate", "no_final", "error", 0),
        ("validate", "bad_json", "error", 0),
        ("validate", "duplicate_keys", "error", 0),
        ("validate", "non_object", "error", 0),
        ("validate", "non_json_constant", "error", 0),
        ("inspect", "table", "ok", 0),
        ("inspect", "missing", "error", 2),
        ("inspect", "bad_json", "error", 0),
        ("inspect", "empty", "error", 0),
    ],
)
def test_protocol_and_exit_code_must_agree(manager, kind, scenario, expected, code):
    job_id = manager.start(kind, config={"scenario": scenario}, input_path=scenario)
    state = wait_for(manager, job_id)
    assert state["status"] == expected, state
    assert state["returncode"] == code
    assert not state["active"]
    if expected == "error" and scenario != "error":
        assert state["error"]
    if kind == "inspect" and scenario == "table":
        assert state["summary"]["path"] == str(manager.root / "table")


def test_memory_and_disk_captures_are_bounded_but_readers_keep_draining(
    manager, monkeypatch
):
    monkeypatch.setattr(runner, "_DISK_LIMIT", 2048)
    job_id = manager.start("run", config={"scenario": "flood"})
    state = wait_for(manager, job_id)
    assert state["status"] == "ok"
    assert len(state["events"]) == 500
    assert state["events"][-1]["event"] == "run_finished"
    # The only progress event has fallen out of the event ring.
    assert not any(e["event"] == "progress" for e in state["events"])
    assert state["progress"]["workers"] == 2
    assert len(state["logs"].encode()) <= 128 * 1024
    assert "TAIL" in state["logs"] and "stderr tail" in state["logs"]
    directory = Path(state["job_dir"])
    assert (directory / "stdout.log").stat().st_size == 2048
    assert (directory / "stderr.log").stat().st_size == 2048
    assert state["summary"]["status"] == "ok"


@pytest.mark.parametrize("kind", ["run", "validate", "inspect"])
def test_oversized_protocol_is_not_success_even_with_a_later_valid_summary(
    manager, monkeypatch, kind
):
    monkeypatch.setattr(runner, "_LINE_LIMIT", 1024)
    state = wait_for(
        manager,
        manager.start(kind, config={"scenario": "oversized"}, input_path="oversized"),
    )
    assert state["status"] == "error"
    assert "exceeds protocol limit" in state["error"]
    assert "[protocol]" in state["logs"]


@pytest.mark.parametrize("kind", ["validate", "inspect"])
def test_command_timeout_is_enforced(manager, kind):
    manager.command_timeout = 0.15
    state = wait_for(
        manager, manager.start(kind, config={"scenario": "wait"}, input_path="wait")
    )
    assert state["status"] == "error"
    assert "timed out" in state["error"]
    assert state["returncode"] is not None


def test_run_does_not_use_command_timeout(manager):
    manager.command_timeout = 0.01
    job_id = manager.start("run", config={"scenario": "wait"})
    wait_for(manager, job_id, lambda s: s["progress"] is not None)
    time.sleep(0.05)
    assert manager.snapshot(job_id)["active"]
    manager.request_cancel(job_id)
    assert wait_for(manager, job_id)["status"] == "cancelled"


def test_close_kills_only_owned_process_tree_and_reaps(manager, monkeypatch):
    monkeypatch.setattr(runner, "_CANCEL_GRACE", 0.1)
    job_id = manager.start("run", config={"scenario": "tree"})
    state = wait_for(manager, job_id, lambda s: s["progress"] is not None)
    child = psutil.Process(state["progress"]["pid"])
    process = manager._jobs[job_id].process
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        manager.close()
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
    assert process.poll() is not None
    state = manager.snapshot(job_id)
    assert state["status"] == "error" and not state["active"]
    try:
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass
    assert psutil.Process().is_running()
    manager.close()
    with pytest.raises(RuntimeError, match="closed"):
        manager.start("validate", config={})


def test_close_allows_cooperative_completion(manager):
    job_id = manager.start("run", config={"scenario": "wait"})
    wait_for(manager, job_id, lambda s: s["progress"] is not None)
    manager.close()
    assert manager.snapshot(job_id)["status"] == "cancelled"


def test_bounded_history_keeps_disk_artifacts(manager, monkeypatch):
    monkeypatch.setattr(runner, "_HISTORY_LIMIT", 3)
    ids = []
    directories = []
    for _ in range(5):
        job_id = manager.start("validate", config={})
        ids.append(job_id)
        directories.append(Path(wait_for(manager, job_id)["job_dir"]))
    assert [s["id"] for s in manager.history()] == ids[:-4:-1]
    assert manager.snapshot(ids[0]) is None
    assert all((directory / "status.json").exists() for directory in directories)


def test_spawn_failure_is_an_error_and_releases_slot(manager):
    manager.python = str(manager.root / "no-such-python")
    state = wait_for(manager, manager.start("validate", config={}))
    assert state["status"] == "error" and state["returncode"] is None
    assert "Cannot execute subprocess" in state["error"]
    manager.python = sys.executable
    assert wait_for(manager, manager.start("validate", config={}))["status"] == "ok"


def test_bad_arguments_do_not_reserve_a_job(manager):
    with pytest.raises(ValueError):
        manager.start("other", config={})
    with pytest.raises(ValueError):
        manager.start("run")
    with pytest.raises(ValueError):
        manager.start("inspect")
    with pytest.raises(ValueError):
        manager.start("validate", config={"n": float("nan")})
    with pytest.raises(KeyError):
        manager.request_cancel("unknown")
    assert manager.history() == []
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            JobManager(manager.root, command_timeout=timeout)


def test_default_interpreter_and_cwd(tmp_path):
    manager = JobManager(tmp_path)
    try:
        assert manager.python == sys.executable
        assert manager.cwd == tmp_path.resolve()
    finally:
        manager.close()


def test_actual_backend_toy_run(toy_config, tmp_path):
    """Real -m bcflow, xsdba, JSONL, netCDF outputs, and immutable JSON config."""
    config = copy.deepcopy(toy_config)
    config["variables"] = ["precip"]
    config["correction"] = {"precip": config["correction"]["precip"]}
    config["output"] = {"path": str(tmp_path / "corrected")}
    # cwd is explicit and does not rely on the test process's working directory.
    project = Path(__file__).resolve().parents[1]
    manager = JobManager(tmp_path / "web", cwd=project)
    try:
        job_id = manager.start("run", config=config, config_digest="toy")
        state = wait_for(manager, job_id, timeout=85)
        assert state["status"] == "ok", state
        assert state["returncode"] == 0
        assert state["summary"]["event"] == "run_finished"
        assert state["summary"]["variables"] == {"precip": "ok"}
        assert Path(state["summary"]["manifest"]).is_file()
        assert any(event["event"] == "qa" for event in state["events"])
        assert state["progress"] is not None
        assert json.loads(Path(state["config_path"]).read_text()) == config
    finally:
        manager.close()
