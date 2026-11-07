"""The terminal renderer preserves the pipeline's event/cancellation contract."""

from __future__ import annotations

import json

import pytest

from bcflow import cli


@pytest.fixture
def bars(monkeypatch):
    created = []

    class FakeBar:
        def __init__(self, **kwargs):
            self.options = kwargs
            self.n = 0
            self.updates = []
            self.labels = []
            self.closed = False
            created.append(self)

        def update(self, amount):
            self.updates.append(amount)
            self.n += amount

        def set_postfix_str(self, label, refresh=False):
            self.labels.append(label)

        def refresh(self):
            pass

        def close(self):
            self.closed = True

    original_init = cli._TerminalProgress.__init__

    def interactive_init(self):
        original_init(self)
        self.interactive = True

    monkeypatch.setattr(cli, "tqdm", FakeBar)
    monkeypatch.setattr(cli._TerminalProgress, "__init__", interactive_init)
    return created


def _block(stage, completed=0, variable="tmax", index=1):
    return {
        "event": "progress",
        "variable": variable,
        "stage": stage,
        "block_index": index,
        "total_blocks": 2,
        "completed_cells": completed,
        "total_cells": 150,
        "block_cells": 100 if index == 1 else 50,
    }


def test_bar_advances_only_for_completed_cells_and_reuses_across_blocks(bars):
    progress = cli._TerminalProgress()
    progress(_block("block_started"))
    progress({**_block("regridding"), "source": "hist"})
    progress(_block("training"))
    assert bars[0].n == 0
    assert bars[0].labels[-2] == "block 1/2 | regridding (hist)"
    progress(_block("block_finished", 100))
    progress(_block("block_started", 100, index=2))
    progress(_block("block_finished", 150, index=2))
    assert len(bars) == 1
    assert bars[0].n == 150
    assert sum(bars[0].updates) == 150
    assert bars[0].options["total"] == 150
    assert bars[0].options["unit"] == "cell"
    progress.close()
    assert bars[0].closed


def test_scan_bar_closes_before_cell_bar_and_combining(bars):
    progress = cli._TerminalProgress()
    progress(
        {
            "event": "progress",
            "variable": "tmax",
            "stage": "scanning_reference",
            "completed": 0,
            "total": 730,
        }
    )
    progress(
        {
            "event": "progress",
            "variable": "tmax",
            "stage": "scanning_reference",
            "completed": 730,
            "total": 730,
        }
    )
    assert bars[0].n == 730
    assert bars[0].options["unit"] == "step"
    progress(_block("block_started"))
    assert bars[0].closed
    assert len(bars) == 2
    assert bars[1].n == 0
    progress({"event": "progress", "variable": "tmax", "stage": "combining"})
    assert bars[1].closed
    assert progress.bar is None


@pytest.mark.parametrize("kind", ["variable_finished", "run_cancelled"])
def test_cancel_or_finish_closes_partial_bar_without_filling_it(bars, kind):
    progress = cli._TerminalProgress()
    progress(_block("block_finished", 100))
    progress(
        {
            "event": kind,
            "variable": "tmax",
            "status": "cancelled",
            "message": "Saving completed variables.",
        }
    )
    assert bars[0].closed
    assert bars[0].n == 100


def test_noninteractive_output_keeps_plain_lines(monkeypatch, capsys):
    def must_not_create_bar(**kwargs):
        pytest.fail("Non-interactive output must not create a bar")

    monkeypatch.setattr(cli, "tqdm", must_not_create_bar)
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: False)
    progress = cli._TerminalProgress()
    progress(_block("block_finished", 100))
    progress.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "100/150 cells" in captured.err


@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize("failure", [None, RuntimeError, cli._ForcedCancellation])
def test_cli_closes_bars_on_all_exit_paths(
    bars, monkeypatch, capsys, tmp_path, json_mode, failure
):
    def run_config_file(path, *, control):
        control.emit(_block("block_finished", 100))
        if failure is not None:
            raise failure()
        return {
            "status": "ok",
            "exit_code": 0,
            "qa": [],
            "variables": {},
            "run_dir": None,
            "manifest": None,
            "error": None,
            "counts": {"pass": 0, "warn": 0, "fail": 0},
        }

    monkeypatch.setattr(cli.service, "run_config_file", run_config_file)
    args = ["run", str(tmp_path / "config.yaml")] + (["--json"] if json_mode else [])
    if failure is RuntimeError:
        with pytest.raises(RuntimeError):
            cli.main(args)
    else:
        assert cli.main(args) == (130 if failure else 0)
    captured = capsys.readouterr()
    if json_mode:
        assert bars == []
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert events[0]["event"] == "progress"
        if failure is not RuntimeError:
            assert events[-1]["event"] == "run_finished"
    else:
        assert len(bars) == 1
        assert bars[0].closed
        assert bars[0].n == 100


def test_real_tqdm_renders_to_stderr_and_closes(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stderr, "isatty", lambda: True)
    progress = cli._TerminalProgress()
    progress(_block("block_started"))
    progress(_block("block_finished", 100))
    progress.close()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "tmax" in captured.err
    assert "100/150" in captured.err
    assert "block 1/2" in captured.err
