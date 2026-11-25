"""Launcher contracts that also run without the optional Streamlit extra."""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import launch

_HOME = Path(__file__).resolve().parents[1] / "app" / "Home.py"


@pytest.mark.parametrize("args", [[], ["--server.port=8765", "--", "a config.yaml"]])
def test_streamlit_argv_uses_absolute_root_entrypoint_and_local_defaults(
    tmp_path, monkeypatch, args
):
    monkeypatch.chdir(tmp_path)
    original_args = list(args)

    def forbid_chdir(*_args, **_kwargs):
        pytest.fail("The launcher must preserve the caller's working directory")

    monkeypatch.setattr(os, "chdir", forbid_chdir)
    argv = launch.streamlit_argv(args)
    assert argv == [
        "streamlit",
        "run",
        str(_HOME),
        "--server.address=127.0.0.1",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        *original_args,
    ]
    assert Path(argv[2]).is_absolute()
    assert Path(argv[2]).is_file()
    assert Path.cwd() == tmp_path
    assert args == original_args


def test_main_delegates_to_streamlit_without_changing_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["bcflow-web", "--server.port=8765"])
    calls = []

    def cli_main():
        calls.append((list(sys.argv), Path.cwd()))
        return 7

    web = ModuleType("streamlit.web")
    web.__dict__["cli"] = SimpleNamespace(main=cli_main)
    monkeypatch.setitem(sys.modules, "streamlit.web", web)

    def forbid_chdir(*_args, **_kwargs):
        pytest.fail("Starting the frontend must not change the caller's workspace")

    monkeypatch.setattr(os, "chdir", forbid_chdir)
    assert launch.main() == 7
    assert calls == [
        (
            [
                "streamlit",
                "run",
                str(_HOME),
                "--server.address=127.0.0.1",
                "--server.headless=true",
                "--browser.gatherUsageStats=false",
                "--server.port=8765",
            ],
            tmp_path,
        )
    ]
    assert Path.cwd() == tmp_path


def test_missing_optional_streamlit_has_actionable_message(monkeypatch, capsys):
    original_import = builtins.__import__
    original_argv = ["bcflow-web", "--server.port=8765"]
    monkeypatch.setattr(sys, "argv", original_argv)

    def without_streamlit(name, *args, **kwargs):
        if name == "streamlit" or name.startswith("streamlit."):
            raise ModuleNotFoundError("No module named 'streamlit'", name="streamlit")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_streamlit)
    assert launch.main() == 2
    captured = capsys.readouterr()
    assert "uv sync --extra web" in captured.err
    assert not captured.out
    assert sys.argv is original_argv
