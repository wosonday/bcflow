"""Fail-closed, dependency-free lint for the agent-tutorial document collection."""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from harness_markdown import Document, Issue, check_links, parse_document
from harness_routing import SECRET, check_contracts, check_model_ids

IGNORED = {
    ".git",
    ".wt",
    ".agents",
    ".opencode",
    ".ua",
    ".venv",
    "node_modules",
    "__pycache__",
}
REQUIRED = {
    "AGENTS-TUTORIAL.md",
    "BOT-CEO.md",
    "HERDR-CORE-BOT.md",
    # docs/ is English-only; the root rule documents stay Vietnamese.
    "docs/README.md",
    "docs/HARNESS-LINT.md",
}


@dataclass
class Report:
    documents: int
    issues: list[Issue]


def load_documents(root: Path) -> tuple[dict[str, Document], list[Issue]]:
    documents: dict[str, Document] = {}
    issues: list[Issue] = []

    def on_error(error: OSError) -> None:
        issues.append(
            Issue(
                str(error.filename), 1, "IO001", "Cannot traverse document directory."
            )
        )

    for directory, folders, files in os.walk(root, onerror=on_error, followlinks=False):
        for folder in folders:
            path = Path(directory) / folder
            if folder not in IGNORED and path.is_symlink():
                issues.append(
                    Issue(
                        path.relative_to(root).as_posix(),
                        1,
                        "IO002",
                        "Symlinked directory is not linted.",
                    )
                )
        folders[:] = sorted(
            folder
            for folder in folders
            if folder not in IGNORED and not (Path(directory) / folder).is_symlink()
        )
        for filename in sorted(files):
            path = Path(directory) / filename
            if path.suffix.lower() != ".md":
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                issues.append(
                    Issue(relative, 1, "IO002", "Symlinked Markdown is not linted.")
                )
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError, UnicodeError:
                issues.append(
                    Issue(relative, 1, "IO003", "Cannot read document as UTF-8.")
                )
                continue
            documents[relative] = parse_document(relative, text)
    return documents, issues


def lint(root: Path) -> Report:
    root = root.resolve()
    if not root.is_dir():
        return Report(0, [Issue(".", 1, "IO004", "Document root is not a directory.")])
    documents, issues = load_documents(root)
    for missing in sorted(REQUIRED - documents.keys()):
        issues.append(
            Issue(missing, 1, "IO005", "Required harness document missing/unreadable.")
        )
    for doc in documents.values():
        check_model_ids(doc)
        try:
            check_links(root, doc, documents)
        except OSError, ValueError:
            doc.error(1, "IO006", "Cannot fully validate local links.")
    issues.extend(check_contracts(documents))
    for doc in documents.values():
        issues.extend(doc.issues)
    redacted = [
        Issue(
            SECRET.sub("<redacted>", issue.path),
            issue.line,
            issue.code,
            SECRET.sub("<redacted>", issue.message),
        )
        for issue in issues
    ]
    return Report(len(documents), sorted(redacted))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    report = lint(args.root)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=True, indent=2))
    else:
        for issue in report.issues:
            print(f"{issue.path}:{issue.line}: {issue.code} {issue.message}")
        print(
            f"Harness lint: {report.documents} documents, {len(report.issues)} errors."
        )
    return 1 if report.issues else 0


if __name__ == "__main__":
    sys.exit(main())
