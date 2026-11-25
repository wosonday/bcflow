"""Configuration view: no scientific work runs in the UI thread."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from app import runtime
from app.runtime import SessionKeys
from app.services._io import resolve_local_path
from app.services.config import list_example_configs, load_document, save_copy
from app.ui.components import render_json_download, render_methods_table, render_validation_messages
from bcflow.discovery import list_methods


def _remember_editor() -> None:
    st.session_state[SessionKeys.EDITOR_BUFFER] = st.session_state[SessionKeys.CONFIG_EDITOR]


def _choose_example() -> None:
    st.session_state[SessionKeys.CONFIG_PATH] = st.session_state[SessionKeys.EXAMPLE_CONFIG]


def _start_and_monitor(manager, kind: str, **kwargs) -> None:
    try:
        st.session_state[SessionKeys.TRACKED_JOB_ID] = manager.start(kind, **kwargs)
        st.switch_page("app_pages/monitor.py")
    except (ValueError, OSError, RuntimeError) as exc:
        st.error(str(exc))


@st.cache_data(ttl=5, show_spinner=False)
def _list_example_configs(cwd: str) -> list[str]:
    return list_example_configs(Path(cwd) / "configs")


def render() -> None:
    st.title("Configure a correction")
    st.caption(
        "Open a YAML/JSON file, edit a draft, then validate it before running. Input data stays on disk."
    )
    runtime.init_session()
    manager = runtime.get_manager()
    examples = _list_example_configs(str(Path.cwd()))
    if examples:
        st.selectbox(
            "Example configuration", examples, key=SessionKeys.EXAMPLE_CONFIG, on_change=_choose_example
        )
    st.text_input(
        "Configuration path",
        key=SessionKeys.CONFIG_PATH,
        help="A path on the Streamlit server's filesystem.",
    )
    document = st.session_state[SessionKeys.DOCUMENT]
    dirty = document is not None and st.session_state[SessionKeys.EDITOR_BUFFER] != document["text"]
    discard = (
        st.checkbox("Discard my unsaved draft when opening another file", key="discard_draft")
        if dirty
        else False
    )
    if st.button("Open config", key="load_config", icon=":material/folder_open:", disabled=dirty and not discard):
        try:
            path = resolve_local_path(st.session_state[SessionKeys.CONFIG_PATH], manager.cwd)
            document = load_document(path)
            st.session_state[SessionKeys.DOCUMENT] = document
            st.session_state[SessionKeys.EDITOR_BUFFER] = document["text"]
            st.session_state[SessionKeys.CONFIG_EDITOR] = document["text"]
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    if document is None:
        st.info("Choose a configuration file and select Open config to begin.")
        return

    st.caption(f"Paths resolve relative to: {document['base_dir']}")
    st.session_state.setdefault(SessionKeys.CONFIG_EDITOR, st.session_state[SessionKeys.EDITOR_BUFFER])
    st.text_area("Configuration draft", key=SessionKeys.CONFIG_EDITOR, height=340, on_change=_remember_editor)
    config, digest, error = runtime.current_config_with_digest()
    if error:
        st.error(error, title="Cannot parse this draft")
    busy = runtime.active_job() is not None
    validation = runtime.matching_validation(digest) if digest else None
    validated = bool(
        validation and validation["status"] == "ok" and (validation.get("summary") or {}).get("valid")
    )
    with st.container(horizontal=True):
        if st.button("Validate config", key="validate_config", icon=":material/fact_check:", disabled=config is None or busy):
            _start_and_monitor(manager, "validate", config=config, config_digest=digest)
        if st.button(
            "Run correction", key="run_config", type="primary", icon=":material/play_arrow:", disabled=not validated or busy
        ):
            _start_and_monitor(manager, "run", config=config, config_digest=digest)
    if busy:
        st.info("A backend job is active. Open Run & monitor to view or cancel it.")
    elif validated:
        st.success("This exact draft passed backend validation. Run correction is enabled.")
    else:
        st.caption(
            "Run is enabled only after this exact draft passes validation. Editing invalidates the previous validation."
        )
    if validation and validation.get("summary"):
        render_validation_messages(validation["summary"])

    with st.expander("Resolved paths and save a copy"):
        st.caption(
            "Original files are never saved automatically. Copies use absolute paths and normalized YAML/JSON; YAML comments are not retained."
        )
        if config is not None:
            st.json(config, expanded=False)
            render_json_download(
                "Download resolved JSON", config, "bcflow-config.json", "download_config", indent=2, width="content"
            )
            copy_path = st.text_input("Copy destination", value="outputs/web-config.json", key="copy_path")
            overwrite = st.checkbox("Replace an existing copy", key="overwrite_copy")
            if st.button("Save copy", key="save_copy"):
                try:
                    target = resolve_local_path(copy_path, manager.cwd, resolve=True)
                    saved = save_copy(
                        config, target, overwrite=overwrite, source_path=document["path"], create_parents=True
                    )
                    st.success(f"Saved {saved}")
                except (ValueError, OSError) as exc:
                    st.error(str(exc))

    with st.expander("Inspect an input dataset"):
        input_path = st.text_input(
            "Dataset path", key="inspect_path", help="Relative paths use the opened config's directory."
        )
        if st.button("Inspect dataset", key="inspect_input", disabled=not input_path or busy):
            path = resolve_local_path(input_path, document["base_dir"], resolve=True)
            _start_and_monitor(manager, "inspect", input_path=path)
    with st.expander("Available correction methods"):
        render_methods_table(list_methods())
