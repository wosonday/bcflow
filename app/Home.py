"""Entry point: render the fast application shell before any backend work."""

# Match the reference Streamlit project's conventional entrypoint filename.
# ruff: noqa: N999

import sys
from pathlib import Path

# Support both `python -m app.launch` and direct `streamlit run app/Home.py`.
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

import streamlit as st

from app.runtime import init_session
from bcflow._version import bcflow_version

st.set_page_config(
    page_title="bcflow", page_icon=":material/water_drop:", layout="wide"
)
init_session()

with st.sidebar:
    st.title("bcflow")
    st.caption("Climate bias correction")
    st.divider()
    st.caption("Local workspace")
    st.code(str(Path.cwd()), language=None, wrap_lines=True)
    st.caption(f"Backend {bcflow_version()}")
    st.info(
        "Personal local app: browser tabs share one active job. Paths refer to files on this machine, not browser uploads."
    )

page = st.navigation(
    [
        st.Page(
            "app_pages/configure.py",
            title="Configure",
            icon=":material/tune:",
            default=True,
        ),
        st.Page(
            "app_pages/monitor.py", title="Run & monitor", icon=":material/play_circle:"
        ),
        st.Page("app_pages/results.py", title="Results", icon=":material/analytics:"),
    ]
)
page.run()
