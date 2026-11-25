"""Configuration view: no scientific work runs in the UI thread."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from app import runtime
from app.services.config import config_digest, load_document, save_copy
from bcflow.discovery import list_methods


def _remember_editor() -> None:
    st.session_state["editor_buffer"] = st.session_state["config_editor"]


def _choose_example() -> None:
    st.session_state["config_path"] = st.session_state["example_config"]


def render() -> None:
    st.title("Configure a correction")
    st.caption(
        "Open a YAML/JSON file, edit a draft, then validate it before running. Input data stays on disk."
    )
    runtime.init_session()
    examples = sorted(
        str(path)
        for path in Path("configs").glob("*")
        if path.suffix in {".yaml", ".yml", ".json"}
    )
    if examples:
        st.selectbox(
            "Example configuration",
            examples,
            key="example_config",
            on_change=_choose_example,
        )
    st.text_input(
        "Configuration path",
        key="config_path",
        help="A path on the Streamlit server's filesystem.",
    )
    document = st.session_state["document"]
    dirty = (
        document is not None and st.session_state["editor_buffer"] != document["text"]
    )
    discard = (
        st.checkbox(
            "Discard my unsaved draft when opening another file", key="discard_draft"
        )
        if dirty
        else False
    )
    if st.button(
        "Open config",
        key="load_config",
        icon=":material/folder_open:",
        disabled=dirty and not discard,
    ):
        try:
            document = load_document(st.session_state["config_path"])
            st.session_state["document"] = document
            st.session_state["editor_buffer"] = document["text"]
            st.session_state["config_editor"] = document["text"]
        except (ValueError, OSError) as exc:
            st.error(str(exc))
    if document is None:
        st.info("Choose a configuration file and select Open config to begin.")
        return

    st.caption(f"Paths resolve relative to: {document['base_dir']}")
    st.session_state.setdefault("config_editor", st.session_state["editor_buffer"])
    st.text_area(
        "Configuration draft",
        key="config_editor",
        height=340,
        on_change=_remember_editor,
    )
    st.session_state["editor_buffer"] = st.session_state["config_editor"]
    config, error = runtime.current_config()
    if error:
        st.error(error, title="Cannot parse this draft")
    manager = runtime.get_manager()
    busy = runtime.active_job() is not None
    digest = config_digest(config) if config is not None else ""
    validation = runtime.matching_validation(digest) if digest else None
    validated = bool(
        validation
        and validation["status"] == "ok"
        and (validation.get("summary") or {}).get("valid")
    )
    with st.container(horizontal=True):
        if st.button(
            "Validate config",
            key="validate_config",
            icon=":material/fact_check:",
            disabled=config is None or busy,
        ):
            try:
                st.session_state["tracked_job_id"] = manager.start(
                    "validate", config=config, config_digest=digest
                )
                st.switch_page("app_pages/monitor.py")
            except (ValueError, OSError, RuntimeError) as exc:
                st.error(str(exc))
        if st.button(
            "Run correction",
            key="run_config",
            type="primary",
            icon=":material/play_arrow:",
            disabled=not validated or busy,
        ):
            try:
                st.session_state["tracked_job_id"] = manager.start(
                    "run", config=config, config_digest=digest
                )
                st.switch_page("app_pages/monitor.py")
            except (ValueError, OSError, RuntimeError) as exc:
                st.error(str(exc))
    if busy:
        st.info("A backend job is active. Open Run & monitor to view or cancel it.")
    elif validated:
        st.success(
            "This exact draft passed backend validation. Run correction is enabled."
        )
    else:
        st.caption(
            "Run is enabled only after this exact draft passes validation. Editing invalidates the previous validation."
        )
    if validation and validation.get("summary"):
        summary = validation["summary"]
        for item in summary.get("errors", []):
            st.error(
                f"{'.'.join(map(str, item.get('loc', [])))}: {item.get('message', '')}"
            )
        for item in summary.get("warnings", []):
            st.warning(item.get("message", str(item)))

    with st.expander("Resolved paths and save a copy"):
        st.caption(
            "Original files are never saved automatically. Copies use absolute paths and normalized YAML/JSON; YAML comments are not retained."
        )
        if config is not None:
            st.json(config, expanded=False)
            st.download_button(
                "Download resolved JSON",
                json.dumps(config, indent=2),
                file_name="bcflow-config.json",
                mime="application/json",
                key="download_config",
            )
            copy_path = st.text_input(
                "Copy destination", value="outputs/web-config.json", key="copy_path"
            )
            overwrite = st.checkbox("Replace an existing copy", key="overwrite_copy")
            if st.button("Save copy", key="save_copy"):
                try:
                    target = Path(copy_path).expanduser().resolve()
                    if target == Path(document["path"]).resolve():
                        raise ValueError(
                            "Choose a different destination; the source config is protected."
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    saved = save_copy(config, target, overwrite=overwrite)
                    st.success(f"Saved {saved}")
                except (ValueError, OSError) as exc:
                    st.error(str(exc))

    with st.expander("Inspect an input dataset"):
        input_path = st.text_input(
            "Dataset path",
            key="inspect_path",
            help="Relative paths use the opened config's directory.",
        )
        if st.button(
            "Inspect dataset", key="inspect_input", disabled=not input_path or busy
        ):
            try:
                path = Path(input_path).expanduser()
                if not path.is_absolute():
                    path = Path(document["base_dir"]) / path
                st.session_state["tracked_job_id"] = manager.start(
                    "inspect", input_path=path.resolve()
                )
                st.switch_page("app_pages/monitor.py")
            except (ValueError, OSError, RuntimeError) as exc:
                st.error(str(exc))
    with st.expander("Available correction methods"):
        st.dataframe(
            list_methods(), hide_index=True, alt="Available bias-correction algorithms"
        )
