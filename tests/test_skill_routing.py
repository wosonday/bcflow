"""Regression checks for the user-approved workflow routing, not installed skills."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_markdown import parse_document
from harness_routing import CONTRACTS, check_contracts


APPROVED = {
    "discovery": "brainstorming", "system-design": "system-design",
    "adr": "architecture", "planning": "writing-plans",
    "test-strategy": "testing-strategy", "feature": "test-driven-development",
    "bugfix": "systematic-debugging", "review-fix": "receiving-code-review",
    "review-request": "requesting-code-review", "review": "code-review",
    "audit": "script/CI", "docs": "documentation",
    "verification": "verification-before-completion",
    "skill-authoring": "writing-skills", "mechanical": "none",
}


def table(name, rows):
    body = "\n".join(f"| `{key}` | `{value}` | Scope |" for key, value in rows.items())
    return f"<!-- harness:{name}:start -->\n| Role | Workflow | Scope |\n|---|---|---|\n{body}\n<!-- harness:{name}:end -->\n"


class SkillRoutingTests(unittest.TestCase):
    def codes(self, replacement):
        documents = {}
        for filename, blocks in CONTRACTS.items():
            if filename == "SKILL-ROUTING.md":
                continue
            path = "docs/" + filename
            documents[path] = parse_document(path, "# Policy\n" + "\n".join(table(name, rows) for name, rows in blocks.items()))
        if replacement is not None:
            path = "docs/SKILL-ROUTING.md"
            documents[path] = parse_document(path, "# Skills\n" + replacement)
        issues = check_contracts(documents)
        return {issue.code for issue in issues} | {issue.code for doc in documents.values() for issue in doc.issues}

    def test_complete_approved_matrix_passes(self):
        self.assertEqual(self.codes(table("skill-routing", APPROVED)), set())

    def test_deleting_entire_skill_matrix_fails(self):
        self.assertIn("POLICY006", self.codes(None))

    def test_missing_debugging_or_verification_route_fails(self):
        for missing in ("bugfix", "verification"):
            with self.subTest(missing=missing):
                self.assertIn("POLICY005", self.codes(table("skill-routing", {k: v for k, v in APPROVED.items() if k != missing})))

    def test_ponytail_cannot_replace_debugging_or_review(self):
        for phase in ("bugfix", "review"):
            with self.subTest(phase=phase):
                self.assertIn("POLICY004", self.codes(table("skill-routing", {**APPROVED, phase: "ponytail"})))

    def test_review_feedback_cannot_use_review_request_workflow(self):
        self.assertIn("POLICY004", self.codes(table("skill-routing", {**APPROVED, "review-fix": "requesting-code-review"})))

    def test_fenced_skill_matrix_is_not_a_live_contract(self):
        self.assertIn("POLICY001", self.codes("````\n" + table("skill-routing", APPROVED) + "````\n"))


if __name__ == "__main__":
    unittest.main()
