"""Allow `python -m bcflow ...`, e.g. when the desktop app calls a specific venv."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
