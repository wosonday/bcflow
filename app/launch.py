"""Launch the optional local frontend without changing config path semantics."""

from __future__ import annotations

import sys
from pathlib import Path


def streamlit_argv(args: list[str]) -> list[str]:
    """Use an absolute script path, keeping the user's workspace as cwd."""
    return [
        "streamlit",
        "run",
        str(Path(__file__).with_name("Home.py")),
        "--server.address=127.0.0.1",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        *args,
    ]


def main() -> int:
    try:
        from streamlit.web import cli
    except ImportError:
        print("Install the web extra first: uv sync --extra web", file=sys.stderr)
        return 2
    sys.argv = streamlit_argv(sys.argv[1:])
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
