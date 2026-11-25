"""Root-app UI contracts, with static inputs and no backend processes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit", minversion="1.67")
from streamlit.testing.v1 import AppTest

from app import runtime
from app.services.config import config_digest
from bcflow.discovery import list_methods

pytestmark = pytest.mark.usefixtures("web_component_discovery_cache")

_HOME = Path(__file__).resolve().parents[1] / "app" / "Home.py"
_CONFIG_TEXT = """# A static UI fixture; no input datasets are needed.
variables: [tmax]
stations:
  metadata: stations.csv
  data_dir: weather
model:
  hist: model/{var}_hist.nc
  sim: model/{var}_sim.nc
correction:
  tmax:
    method: empirical_quantile_mapping
periods:
  calibration: [2000-01-01, 2001-12-31]
output:
  path: outputs
"""


class FakeManager:
    """In-memory manager boundary; completion only happens when a test says so."""

    def __init__(self, root: Path):
        self.root = self.cwd = root
        self.jobs = {}
        self.starts = []
        self.cancel_requests = []

    def add_job(self, kind="run", status="running", **values):
        job_id = f"job-{len(self.jobs) + 1}"
        active = status in {"running", "cancelling"}
        job = {
            "id": job_id,
            "kind": kind,
            "status": status,
            "active": active,
            "created_at": "2026-11-08T12:00:00+00:00",
            "elapsed_seconds": 1.5,
            "config_digest": "",
            "config_path": None,
            "returncode": None,
            "summary": None,
            "error": None,
            "progress": None,
            "job_dir": str(self.root / "outputs" / ".web-jobs" / job_id),
            "events": [],
            "logs": "",
        }
        job.update(values)
        self.jobs[job_id] = job
        return job

    def history(self):
        return [copy.deepcopy(job) for job in reversed(self.jobs.values())]

    def snapshot(self, job_id=None):
        if job_id is None:
            job_id = next(reversed(self.jobs), None)
        return copy.deepcopy(self.jobs.get(job_id))

    def start(self, kind, *, config=None, config_digest="", input_path=None):
        # Keep the actual argument reference so later UI mutation is observable.
        self.starts.append(
            {
                "kind": kind,
                "config": config,
                "config_digest": config_digest,
                "input_path": input_path,
            }
        )
        if any(job["active"] for job in self.jobs.values()):
            raise RuntimeError("A job is already active")
        return self.add_job(kind, config_digest=config_digest)["id"]

    def request_cancel(self, job_id):
        self.cancel_requests.append(job_id)
        self.jobs[job_id]["status"] = "cancelling"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake = FakeManager(tmp_path)
    monkeypatch.setattr(runtime, "get_manager", lambda: fake)
    return fake


@pytest.fixture
def config_path(tmp_path):
    folder = tmp_path / "configs"
    folder.mkdir()
    path = folder / "toy.yaml"
    path.write_text(_CONFIG_TEXT, encoding="utf-8")
    return path


def _app():
    # Cold Streamlit imports on Windows-mounted/CI filesystems can exceed 15s.
    app = AppTest.from_file(str(_HOME), default_timeout=45).run()
    _no_exceptions(app)
    return app


def _no_exceptions(app):
    assert not app.exception, [item.message for item in app.exception]


def _open(app, path):
    app.text_input(key="config_path").set_value(str(path)).run()
    app.button(key="load_config").click().run()
    _no_exceptions(app)
    return app


def _resolved(app):
    return json.loads(app.json[0].value)


def _expected_config(path):
    base = path.parent
    return {
        "variables": ["tmax"],
        "stations": {
            "metadata": str(base / "stations.csv"),
            "data_dir": str(base / "weather"),
        },
        "model": {
            "hist": str(base / "model/{var}_hist.nc"),
            "sim": str(base / "model/{var}_sim.nc"),
        },
        "correction": {"tmax": {"method": "empirical_quantile_mapping"}},
        "periods": {"calibration": ["2000-01-01", "2001-12-31"]},
        "output": {"path": str(base / "outputs")},
    }


def _validated(manager, app, **values):
    fields = {
        "status": "ok",
        "config_digest": config_digest(_resolved(app)),
        "returncode": 0,
        "summary": {"valid": True, "errors": [], "warnings": []},
    }
    fields.update(values)
    return manager.add_job("validate", **fields)


def test_homepage_and_empty_navigation_pages_have_no_exceptions(manager):
    app = _app()
    assert any(item.value == "Configure a correction" for item in app.title)
    assert app.session_state["document"] is None
    assert app.session_state["editor_buffer"] == ""
    assert app.session_state["tracked_job_id"] is None
    assert app.text_input(key="config_path").value == "configs/example_toy.yaml"
    for page, message in (
        ("monitor", "No jobs yet"),
        ("results", "No saved runs"),
        ("configure", "Choose a configuration file"),
    ):
        app.switch_page(f"app_pages/{page}.py").run()
        _no_exceptions(app)
        assert any(message in item.value for item in app.info)
    assert not manager.starts


def test_open_absolute_config_preserves_text_and_base_directory(manager, config_path):
    app = _open(_app(), config_path)
    document = app.session_state["document"]
    assert document["path"] == str(config_path)
    assert document["base_dir"] == str(config_path.parent)
    assert document["text"] == _CONFIG_TEXT
    assert app.session_state["editor_buffer"] == _CONFIG_TEXT
    assert app.text_area(key="config_editor").value == _CONFIG_TEXT
    assert _resolved(app) == _expected_config(config_path)
    assert any(str(config_path.parent) in item.value for item in app.caption)
    assert not app.button(key="validate_config").disabled
    assert app.button(key="run_config").disabled
    assert not manager.starts


@pytest.mark.parametrize("draft", ["variables: [unterminated", "[]", "null"])
def test_invalid_draft_disables_validation_and_run(manager, config_path, draft):
    app = _open(_app(), config_path)
    _validated(manager, app)
    app.run()
    assert not app.button(key="run_config").disabled
    app.text_area(key="config_editor").set_value(draft).run()
    _no_exceptions(app)
    assert app.error
    assert app.session_state["editor_buffer"] == draft
    assert app.button(key="validate_config").disabled
    assert app.button(key="run_config").disabled
    assert not manager.starts


def test_validate_then_run_submits_unchanged_config_once(manager, config_path):
    original = config_path.read_bytes()
    app = _open(_app(), config_path)
    expected = _expected_config(config_path)
    digest = config_digest(expected)
    app.button(key="validate_config").click().run()
    _no_exceptions(app)
    assert manager.starts == [
        {
            "kind": "validate",
            "config": expected,
            "config_digest": digest,
            "input_path": None,
        }
    ]
    validation_id = app.session_state["tracked_job_id"]
    assert manager.jobs[validation_id]["kind"] == "validate"
    assert any(item.value == "Run & monitor" for item in app.title)
    manager.jobs[validation_id].update(
        active=False,
        status="ok",
        returncode=0,
        summary={"valid": True, "errors": [], "warnings": []},
    )
    app.switch_page("app_pages/configure.py").run()
    _no_exceptions(app)
    assert not app.button(key="run_config").disabled
    app.button(key="run_config").click().run()
    _no_exceptions(app)
    assert len(manager.starts) == 2
    submission = manager.starts[1]
    assert submission == {
        "kind": "run",
        "config": expected,
        "config_digest": digest,
        "input_path": None,
    }
    run_id = app.session_state["tracked_job_id"]
    assert run_id != validation_id
    assert manager.jobs[run_id]["kind"] == "run"
    app.run()
    app.run()
    _no_exceptions(app)
    app.switch_page("app_pages/configure.py").run()
    _no_exceptions(app)
    assert app.button(key="validate_config").disabled
    assert app.button(key="run_config").disabled
    app.text_area(key="config_editor").set_value(
        _CONFIG_TEXT.replace("path: outputs", "path: different-outputs")
    ).run()
    _no_exceptions(app)
    assert _resolved(app)["output"]["path"] == str(
        config_path.parent / "different-outputs"
    )
    assert submission["config"] == expected
    assert submission["config_digest"] == digest
    assert len(manager.starts) == 2
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    "fields",
    [
        {"config_digest": "a-different-draft"},
        {"status": "failed", "returncode": 1},
        {"summary": {"valid": False, "errors": []}},
        {"active": True, "status": "running", "returncode": None},
    ],
    ids=["wrong-digest", "failed-job", "invalid-summary", "still-active"],
)
def test_run_requires_completed_successful_matching_validation(
    manager, config_path, fields
):
    app = _open(_app(), config_path)
    _validated(manager, app, **fields)
    app.run()
    _no_exceptions(app)
    assert app.button(key="run_config").disabled
    assert not manager.starts


def test_editing_draft_invalidates_previous_validation(manager, config_path):
    app = _open(_app(), config_path)
    _validated(manager, app)
    app.run()
    assert not app.button(key="run_config").disabled
    edited = _CONFIG_TEXT.replace("path: outputs", "path: revised")
    app.text_area(key="config_editor").set_value(edited).run()
    _no_exceptions(app)
    assert app.button(key="run_config").disabled
    assert not app.button(key="validate_config").disabled
    app.text_area(key="config_editor").set_value(_CONFIG_TEXT).run()
    _no_exceptions(app)
    assert not app.button(key="run_config").disabled
    assert not manager.starts


def test_editor_draft_survives_switching_pages(manager, config_path):
    app = _open(_app(), config_path)
    draft = _CONFIG_TEXT.replace("path: outputs", "path: unsaved")
    app.text_area(key="config_editor").set_value(draft).run()
    for page in ("monitor", "results", "configure"):
        app.switch_page(f"app_pages/{page}.py").run()
        _no_exceptions(app)
        assert app.session_state["editor_buffer"] == draft
        assert app.session_state["document"]["base_dir"] == str(config_path.parent)
    assert app.text_area(key="config_editor").value == draft
    assert _resolved(app)["output"]["path"] == str(config_path.parent / "unsaved")
    assert config_path.read_text(encoding="utf-8") == _CONFIG_TEXT
    assert not manager.starts


def test_open_another_file_requires_explicit_discard(manager, config_path, tmp_path):
    other_folder = tmp_path / "another-folder"
    other_folder.mkdir()
    other = other_folder / "other.yaml"
    other_text = _CONFIG_TEXT.replace("path: outputs", "path: other-outputs")
    other.write_text(other_text, encoding="utf-8")
    app = _open(_app(), config_path)
    draft = _CONFIG_TEXT + "# Keep this unsaved draft\n"
    app.text_area(key="config_editor").set_value(draft).run()
    app.text_input(key="config_path").set_value(str(other)).run()
    _no_exceptions(app)
    assert app.button(key="load_config").disabled
    assert not app.checkbox(key="discard_draft").value
    assert app.session_state["document"]["path"] == str(config_path)
    assert app.text_area(key="config_editor").value == draft
    app.checkbox(key="discard_draft").check().run()
    assert not app.button(key="load_config").disabled
    app.button(key="load_config").click().run()
    _no_exceptions(app)
    assert app.session_state["document"]["path"] == str(other)
    assert app.session_state["document"]["base_dir"] == str(other.parent)
    assert app.text_area(key="config_editor").value == other_text
    assert _resolved(app)["output"]["path"] == str(other.parent / "other-outputs")
    assert config_path.read_text(encoding="utf-8") == _CONFIG_TEXT
    assert not manager.starts


def test_save_copy_keeps_original_and_refuses_overwriting_source(
    manager, config_path, tmp_path
):
    original = config_path.read_bytes()
    app = _open(_app(), config_path)
    draft = _CONFIG_TEXT.replace("path: outputs", "path: draft-outputs")
    app.text_area(key="config_editor").set_value(draft).run()
    target = tmp_path / "copies" / "saved.json"
    app.text_input(key="copy_path").set_value(str(target)).run()
    app.button(key="save_copy").click().run()
    _no_exceptions(app)
    expected = _expected_config(config_path)
    expected["output"]["path"] = str(config_path.parent / "draft-outputs")
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert any("Saved" in item.value for item in app.success)
    assert config_path.read_bytes() == original
    assert app.session_state["document"]["path"] == str(config_path)
    assert app.session_state["document"]["base_dir"] == str(config_path.parent)
    assert app.session_state["editor_buffer"] == draft
    app.text_input(key="copy_path").set_value(str(config_path)).run()
    app.checkbox(key="overwrite_copy").check().run()
    app.button(key="save_copy").click().run()
    _no_exceptions(app)
    assert any("source config is protected" in item.value for item in app.error)
    assert config_path.read_bytes() == original
    assert not manager.starts


def test_monitor_cancellation_is_only_a_request(manager):
    job = manager.add_job()
    app = _app().switch_page("app_pages/monitor.py").run()
    _no_exceptions(app)
    app.button(key=f"cancel_{job['id']}").click().run()
    _no_exceptions(app)
    assert manager.cancel_requests == [job["id"]]
    assert job["status"] == "cancelling"
    assert job["active"] is True
    assert job["returncode"] is None
    assert job["summary"] is None
    assert app.button(key=f"cancel_{job['id']}").disabled
    assert any("Cancellation requested" in item.value for item in app.warning)
    assert not app.success
    assert not any("Job cancelled." in item.value for item in app.warning)
    app.run()
    _no_exceptions(app)
    assert manager.cancel_requests == [job["id"]]
    assert job["active"] is True
    assert not manager.starts


@pytest.mark.parametrize(
    "status, returncode, alert, message",
    [
        ("ok", 0, "success", "Job completed successfully"),
        ("cancelled", 130, "warning", "Job cancelled"),
        ("failed", 1, "error", "Static backend failure"),
        ("error", 2, "error", "Static backend failure"),
    ],
)
def test_monitor_displays_terminal_summary_statuses_and_logs(
    manager, status, returncode, alert, message
):
    variables = {"tmax": "ok"}
    if status != "ok":
        variables.update(precip="failed", wind="cancelled", solar="skipped")
    logs = "A diagnostic line\nUnicode: café — <not-html>\n"
    manager.add_job(
        status=status,
        returncode=returncode,
        error="Static backend failure" if status in {"failed", "error"} else None,
        logs=logs,
        events=[{"event": "run_finished", "status": status, "exit_code": returncode}],
        summary={"variables": variables, "run_dir": str(manager.root / "saved-run")},
    )
    app = _app().switch_page("app_pages/monitor.py").run()
    _no_exceptions(app)
    assert any(message in item.value for item in getattr(app, alert))
    table = next(item.value for item in app.dataframe if "Variable" in item.value)
    assert table.to_dict("records") == [
        {"Variable": name, "Status": value} for name, value in variables.items()
    ]
    assert any(item.value == logs.rstrip() for item in app.code)
    assert any(item.value == str(manager.root / "saved-run") for item in app.code)
    assert {item.label: item.value for item in app.metric}["Exit code"] == str(
        returncode
    )
    assert not any(item.key.startswith("cancel_") for item in app.button if item.key)
    if status != "ok":
        assert not app.success
    assert not manager.cancel_requests
    assert not manager.starts


def test_monitor_renders_structured_progress_and_refreshes_snapshot(manager):
    job = manager.add_job(
        logs="Worker diagnostic output",
        progress={
            "variable": "tmax",
            "stage": "adjusting_blocks",
            "workers": 2,
            "cpu_limit": 4,
            "memory_limit": "1 GiB",
            "estimated_task_bytes": 1024**3,
        },
        events=[
            {
                "event": "progress",
                "variable": "tmax",
                "completed_cells": 5,
                "total_cells": 10,
                "block_index": 1,
                "total_blocks": 2,
            }
        ],
    )
    app = _app().switch_page("app_pages/monitor.py").run()
    _no_exceptions(app)
    assert any("tmax: adjusting blocks" in item.value for item in app.caption)
    assert any("Workers: 2" in item.value for item in app.caption)
    assert app.get("progress")[0].proto.value == 50
    assert json.loads(app.json[0].value) == job["events"]
    assert any(item.value == "Worker diagnostic output" for item in app.code)
    job.update(logs="New diagnostic output", progress=None, events=[])
    app.run()
    _no_exceptions(app)
    assert any(item.value == "New diagnostic output" for item in app.code)
    assert not app.get("progress")
    assert not manager.starts


def test_monitor_warns_when_final_response_is_missing(manager):
    manager.add_job(status="error", returncode=2, logs="No final JSON response")
    app = _app().switch_page("app_pages/monitor.py").run()
    _no_exceptions(app)
    assert any("usable final response" in item.value for item in app.warning)
    assert any(item.value == "No final JSON response" for item in app.code)
    assert not app.success


def test_monitor_idle_calls_history_once_and_relative_load_uses_manager_cwd(
    manager, config_path, monkeypatch
):
    calls = 0
    original_history = manager.history

    def counted_history():
        nonlocal calls
        calls += 1
        return original_history()

    monkeypatch.setattr(manager, "history", counted_history)
    _app().switch_page("app_pages/monitor.py").run()
    assert calls == 1

    # Relative config path resolves against manager.cwd even if cwd differs.
    other_cwd = config_path.parent.parent / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)
    app = _open(_app(), Path("configs") / config_path.name)
    assert app.session_state["document"]["path"] == str(config_path)


def test_methods_table_frame_surfaces_family_variables_projection_and_extras():
    from app.ui.components import methods_table_frame
    from bcflow.discovery import list_methods

    frame = methods_table_frame(list_methods())

    assert list(frame.columns) == [
        "Method",
        "Family",
        "Variables",
        "Projection",
        "Extra",
        "Summary",
    ]
    assert len(frame) == 11
    rows = {row["Method"]: row for row in frame.to_dict("records")}
    assert rows["empirical_quantile_mapping"]["Family"] == "train + adjust"
    assert rows["empirical_quantile_mapping"]["Variables"] == "1 (univariate)"
    assert rows["empirical_quantile_mapping"]["Projection"] == "Yes"
    assert rows["empirical_quantile_mapping"]["Extra"] == "Built-in"
    assert rows["npdf_transform"]["Family"] == "adjust only"
    assert rows["extreme_values"]["Family"] == "post-step only (post:)"
    assert rows["mbcn"]["Variables"] == "2+ (targets:)"
    assert rows["otc"]["Projection"] == "In-sample only"
    assert rows["otc"]["Extra"] == "Requires POT"
    assert rows["dotc"]["Extra"] == "Requires POT"
    assert all(row["Summary"] for row in rows.values())


def test_configure_methods_expander_renders_badges_with_alt(manager, config_path):
    app = _open(_app(), config_path)
    columns = {"Method", "Family", "Variables", "Projection", "Extra", "Summary"}
    elements = [
        element for element in app.dataframe if set(element.value.columns) == columns
    ]
    assert len(elements) == 1
    table = elements[0].value

    assert len(table) == 11
    assert table["Method"].tolist() == [method["name"] for method in list_methods()]
    assert "post-step only (post:)" in table["Family"].tolist()
    assert "2+ (targets:)" in table["Variables"].tolist()
    assert "In-sample only" in table["Projection"].tolist()
    assert "Requires POT" in table["Extra"].tolist()
    assert all(element.proto.alt for element in elements)
    caption = " ".join(item.value for item in app.caption)
    assert "correction.<var>.post:" in caption
    assert "targets:" in caption
    assert "pip install POT" in caption
