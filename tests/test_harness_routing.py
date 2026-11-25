import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_markdown import parse_document
from harness_routing import (
    BACKGROUND,
    CONTRACTS,
    FLASH,
    PRO,
    SAFETY,
    check_contracts,
    check_model_ids,
    contract,
)
from lint_harness import REQUIRED, lint


def block(name: str, rows: dict[str, str]) -> str:
    contents = "\n".join(
        f"| `{key}` | `{value}` | Scope |" for key, value in rows.items()
    )
    return (
        f"<!-- harness:{name}:start -->\n"
        f"| Role | Model | Scope |\n|---|---|---|\n{contents}\n"
        f"<!-- harness:{name}:end -->\n"
    )


class ModelTests(unittest.TestCase):
    def codes(self, text: str, path: str = "docs/a.md") -> set[str]:
        doc = parse_document(path, "# T\n" + text + "\n")
        check_model_ids(doc)
        return {issue.code for issue in doc.issues}

    def test_confirmed_models(self):
        self.assertEqual(
            self.codes("\n".join([FLASH, PRO, *BACKGROUND.values()])), set()
        )

    def test_approved_variant(self):
        self.assertEqual(self.codes(FLASH + "#high"), set())

    def test_wrong_variant(self):
        self.assertIn("MODEL003", self.codes(PRO + "#low"))

    def test_retired_free_id(self):
        self.assertIn("MODEL001", self.codes("opencode/deepseek-v4.1-flash-free"))

    def test_personal_api_is_not_go(self):
        self.assertIn("MODEL002", self.codes("opencode/deepseek-v4-pro"))

    def test_unknown_nvidia_alias_or_model(self):
        for value in (
            "nvidia-kimi/moonshotai/kimi-typo",
            "nvidia-other/nvidia/nemotron",
        ):
            self.assertIn("MODEL002", self.codes(value))

    def test_model_ids_are_case_sensitive(self):
        for value in (FLASH.upper(), BACKGROUND["background-kimi"].upper()):
            self.assertIn("MODEL002", self.codes(value))

    def test_explanatory_namespace_wildcard(self):
        self.assertEqual(
            self.codes("Not a silent fallback: `opencode/deepseek-*`."), set()
        )

    def test_namespace_wildcard_is_not_runnable(self):
        for selection in (
            "opencode run --model opencode/deepseek-*",
            "model: opencode/deepseek-*",
        ):
            self.assertIn("MODEL004", self.codes(selection))

    def test_archived_retired_id_allowed(self):
        self.assertEqual(
            self.codes(
                "Retired: opencode/deepseek-v4.1-flash-free",
                "docs/superpowers/specs/a.md",
            ),
            set(),
        )

    def test_credentials_rejected_even_in_archive_without_echoing_value(self):
        fake = "nvapi-" + "x" * 30
        doc = parse_document("docs/superpowers/specs/a.md", f"# T\n{fake}\n")
        check_model_ids(doc)
        self.assertIn("SEC001", {issue.code for issue in doc.issues})
        self.assertNotIn(fake, str(doc.issues))


class ContractTests(unittest.TestCase):
    def parse(self, text: str, expected=None):
        doc = parse_document("docs/a.md", "# T\n" + text)
        rows = contract(doc, "safety", SAFETY if expected is None else expected)
        return rows, {issue.code for issue in doc.issues}

    def test_valid_safety_contract(self):
        rows, codes = self.parse(block("safety", SAFETY))
        self.assertEqual(rows, SAFETY)
        self.assertEqual(codes, set())

    def test_missing_rule(self):
        rows = {key: value for key, value in SAFETY.items() if key != "writes"}
        self.assertIn("POLICY005", self.parse(block("safety", rows))[1])

    def test_wrong_and_extra_rule(self):
        rows = {**SAFETY, "writes": "all", "invented": "yes"}
        self.assertIn("POLICY004", self.parse(block("safety", rows))[1])

    def test_duplicate_rule(self):
        text = block("safety", SAFETY).replace(
            "<!-- harness:safety:end -->",
            "| `writes` | `none` | duplicate |\n<!-- harness:safety:end -->",
        )
        self.assertIn("POLICY003", self.parse(text)[1])

    def test_duplicate_blocks(self):
        self.assertIn("POLICY001", self.parse(block("safety", SAFETY) * 2)[1])

    def test_markers_in_example_do_not_count(self):
        self.assertIn(
            "POLICY001", self.parse("````\n" + block("safety", SAFETY) + "````\n")[1]
        )

    def test_reversed_markers(self):
        self.assertIn(
            "POLICY001",
            self.parse("<!-- harness:safety:end -->\n<!-- harness:safety:start -->\n")[
                1
            ],
        )

    def test_missing_header_is_not_accepted(self):
        text = block("safety", SAFETY).replace(
            "| Role | Model | Scope |\n|---|---|---|\n", ""
        )
        self.assertIn("POLICY008", self.parse(text)[1])

    def test_malformed_table(self):
        text = block("safety", SAFETY).replace(
            "| `writes` | `none` | Scope |", "| `writes` |"
        )
        self.assertIn("POLICY002", self.parse(text)[1])

    def test_missing_policy_files_fails_closed(self):
        self.assertIn("POLICY006", {issue.code for issue in check_contracts({})})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="harness-lint-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for path in REQUIRED:
            self.write(path, "# Document\n")
        for filename, blocks in CONTRACTS.items():
            text = "# Policy\n\n" + "\n".join(
                block(name, rows) for name, rows in blocks.items()
            )
            self.write("docs/" + filename, text)

    def write(self, relative: str, text: str):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def codes(self):
        return {issue.code for issue in lint(self.root).issues}

    def test_complete_fixture_passes(self):
        self.assertEqual(lint(self.root).issues, [])

    def test_empty_root_does_not_pass(self):
        with TemporaryDirectory(prefix="harness-empty-") as root:
            self.assertIn("IO005", {issue.code for issue in lint(Path(root)).issues})

    def test_nonexistent_root(self):
        self.assertIn(
            "IO004", {issue.code for issue in lint(self.root / "missing").issues}
        )

    def test_deleted_required_doc(self):
        (self.root / "AGENTS-TUTORIAL.md").unlink()
        self.assertIn("IO005", self.codes())

    def test_invalid_utf8(self):
        (self.root / "docs/README.md").write_bytes(b"\xff")
        self.assertTrue({"IO003", "IO005"} <= self.codes())

    def test_traversal_error_is_not_silently_ignored(self):
        def failed_walk(root, *, onerror, followlinks):
            onerror(PermissionError(13, "denied", str(root)))
            return iter(())

        with patch("lint_harness.os.walk", side_effect=failed_walk):
            self.assertIn("IO001", self.codes())

    def test_secret_in_bad_link_is_redacted_from_all_diagnostics(self):
        fake = "nvapi-" + "x" * 30
        self.write("docs/secret.md", f"# T\n[x]({fake}.md)\n")
        report = lint(self.root)
        self.assertIn("SEC001", {issue.code for issue in report.issues})
        self.assertNotIn(fake, str(report.issues))

    def test_safety_contract_drift(self):
        path = self.root / "docs/NVIDIA-BACKGROUND.md"
        text = path.read_text(encoding="utf-8").replace(
            "`background-only`", "`interactive`"
        )
        path.write_text(text, encoding="utf-8")
        self.assertIn("POLICY004", self.codes())

    def test_missing_policy_document(self):
        (self.root / "docs/NVIDIA-BACKGROUND.md").unlink()
        self.assertIn("POLICY006", self.codes())

    def test_nvidia_cannot_be_routed_as_worker(self):
        path = self.root / "docs/OPENCODE-WORKERS-REVIEWER.md"
        text = path.read_text(encoding="utf-8").replace(
            f"| `worker-fast` | `{FLASH}`",
            f"| `worker-fast` | `{BACKGROUND['background-kimi']}`",
        )
        path.write_text(text, encoding="utf-8")
        self.assertIn("POLICY004", self.codes())

    def test_wrong_write_permission_fails(self):
        path = self.root / "docs/NVIDIA-BACKGROUND.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("`none`", "`all`"),
            encoding="utf-8",
        )
        self.assertIn("POLICY004", self.codes())

    def test_ignored_worktree_content(self):
        self.write(".wt/other/broken.md", "not markdown")
        self.assertEqual(self.codes(), set())

    def test_opencode_agent_prompts_are_not_harness_docs(self):
        self.write(
            ".opencode/agent/worker.md", "---\nmodel: x\n---\nNo heading here.\n"
        )
        self.assertEqual(self.codes(), set())

    def test_ua_graph_directory_is_ignored(self):
        self.write(".ua/notes.md", "No H1 heading; local Understand-Anything data.\n")
        self.assertEqual(self.codes(), set())

    def test_external_url_is_not_requested(self):
        self.write("docs/a.md", "# T\n[x](https://invalid.example/no-network)\n")
        self.assertEqual(self.codes(), set())

    def test_cli_json_and_exit_codes(self):
        script = Path(__file__).resolve().parents[1] / "scripts/lint_harness.py"
        args = [
            sys.executable,
            "-B",
            str(script),
            "--root",
            str(self.root),
            "--format",
            "json",
        ]
        good = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(json.loads(good.stdout)["issues"], [])
        self.write("docs/broken.md", "# Broken\n[x](missing.md)\n")
        bad = subprocess.run(args, capture_output=True, text=True, check=False)
        self.assertEqual(bad.returncode, 1, bad.stderr)
        self.assertIn(
            "LINK007", {issue["code"] for issue in json.loads(bad.stdout)["issues"]}
        )


if __name__ == "__main__":
    unittest.main()
