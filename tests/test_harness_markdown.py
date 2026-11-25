from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_markdown import check_links, exact_case, parse_document, slug


class MarkdownTests(unittest.TestCase):
    def codes(self, text: str) -> set[str]:
        return {issue.code for issue in parse_document("a.md", text).issues}

    def test_valid_document(self):
        self.assertEqual(self.codes("# Title\n\n## Section\n\nText.\n"), set())

    def test_trailing_whitespace(self):
        self.assertIn("MD001", self.codes("# Title\nText \n"))

    def test_conflict_markers(self):
        for marker in ("<<<<<<< HEAD", "=======", ">>>>>>> branch"):
            with self.subTest(marker=marker):
                self.assertIn("MD002", self.codes(f"# Title\n{marker}\n"))

    def test_heading_jump(self):
        self.assertIn("MD003", self.codes("# Title\n### Skipped\n"))

    def test_exactly_one_h1(self):
        for text in ("## Missing\n", "# One\n# Two\n"):
            self.assertIn("MD005", self.codes(text))

    def test_final_newline(self):
        self.assertIn("MD006", self.codes("# Title"))

    def test_unclosed_fence_reports_opening_line(self):
        doc = parse_document("a.md", "# Title\n\n```python\nx = 1\n")
        self.assertIn((3, "MD004"), {(issue.line, issue.code) for issue in doc.issues})

    def test_longer_tilde_fence_with_nested_backticks(self):
        self.assertEqual(self.codes("# Title\n~~~text\n```not a heading\n~~~~\n"), set())

    def test_fence_suffix_does_not_close_block(self):
        self.assertIn("MD004", self.codes("# Title\n```\n```python\n"))

    def test_fenced_heading_is_not_anchor(self):
        doc = parse_document("a.md", "# Title\n```\n# Example\n```\n")
        self.assertNotIn("example", doc.anchors)

    def test_bom_and_nul(self):
        codes = self.codes("\ufeff# Title\n\x00\n")
        self.assertTrue({"ENC001", "ENC002"} <= codes)

    def test_table_wrong_width(self):
        self.assertIn("MD007", self.codes("# T\n\n| A | B |\n|---|---|\n| one | two | three |\n"))

    def test_table_escaped_pipe(self):
        self.assertEqual(self.codes("# T\n\n| A | B |\n|---|---|\n| a\\|b | c |\n"), set())

    def test_table_separator_wrong_width(self):
        self.assertIn("MD007", self.codes("# T\n\n| A | B |\n|---|---|---|\n"))

    def test_unicode_numeric_heading_slug(self):
        self.assertEqual(slug("3.1.1. Điều phối kinh phí AI"), "311-điều-phối-kinh-phí-ai")

    def test_duplicate_heading_anchors(self):
        doc = parse_document("a.md", "# T\n## Same\n## Same\n## Same-1\n")
        self.assertTrue({"same", "same-1", "same-1-1"} <= doc.anchors)

    def test_html_anchor(self):
        self.assertIn("custom", parse_document("a.md", '# T\n<a id="custom"></a>\n').anchors)

    def test_fenced_and_inline_code_links_ignored(self):
        doc = parse_document("a.md", "# T\n`[x](missing.md)`\n```\n[x](other.md)\n```\n")
        self.assertEqual(doc.links, [])

    def test_unclosed_link(self):
        self.assertIn("LINK001", self.codes("# T\n[x](broken\n"))

    def test_link_title_and_balanced_parentheses(self):
        doc = parse_document("a.md", '# T\n[x](other(one).md "Title")\n')
        self.assertEqual(doc.links, [(2, "other(one).md")])

    def test_angle_bracket_destination_with_spaces(self):
        doc = parse_document("a.md", '# T\n[x](<other file.md> "Title")\n')
        self.assertEqual(doc.links, [(2, "other file.md")])

    def test_invalid_unencoded_space(self):
        self.assertIn("LINK002", self.codes("# T\n[x](other file.md)\n"))

    def test_missing_reference(self):
        self.assertIn("LINK004", self.codes("# T\n[x][missing]\n"))

    def test_reference_labels_are_normalized(self):
        text = '# T\n[x][Some  Label]\n[Some Label]: other.md "Title"\n'
        doc = parse_document("a.md", text)
        self.assertEqual(doc.issues, [])
        self.assertEqual(doc.links, [(3, "other.md")])

    def test_collapsed_reference(self):
        self.assertNotIn("LINK004", self.codes("# T\n[label][]\n[label]: other.md\n"))

    def test_duplicate_reference(self):
        self.assertIn("LINK003", self.codes("# T\n[a]: x.md\n[A]: y.md\n"))


class LocalLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="harness-links-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.write("a.md", "# Title\n")
        self.write("sub/Đúng (one).md", "# Đúng\n## Section\n")
        self.write("image.png", "placeholder")

    def write(self, name: str, text: str):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def codes(self, destination: str) -> set[str]:
        doc = parse_document("a.md", f"# Title\n[x]({destination})\n")
        linked = parse_document("sub/Đúng (one).md", "# Đúng\n## Section\n")
        check_links(self.root, doc, {doc.path: doc, linked.path: linked})
        return {issue.code for issue in doc.issues}

    def test_valid_local_unicode_encoded_link_and_anchor(self):
        self.assertEqual(self.codes(quote("sub/Đúng (one).md") + "#section"), set())

    def test_same_file_anchor(self):
        self.assertEqual(self.codes("#title"), set())

    def test_missing_anchor(self):
        self.assertIn("LINK010", self.codes("#missing"))

    def test_missing_local_target(self):
        self.assertIn("LINK007", self.codes("missing.md"))

    def test_directory_is_not_a_file(self):
        self.assertIn("LINK007", self.codes("sub/"))

    def test_encoded_traversal_rejected(self):
        self.assertIn("LINK006", self.codes("%2e%2e/outside.md"))

    def test_windows_separator_rejected(self):
        self.assertIn("LINK005", self.codes("sub%5cfile.md"))

    def test_absolute_path_rejected(self):
        self.assertIn("LINK005", self.codes("/absolute.md"))

    def test_windows_drive_path_rejected(self):
        self.assertIn("LINK005", self.codes("C:/private/file.md"))

    def test_case_is_checked_portably(self):
        self.assertFalse(exact_case(self.root, "A.md"))
        self.assertTrue(exact_case(self.root, "a.md"))

    def test_external_links_are_offline(self):
        self.assertEqual(self.codes("https://invalid.example/not-requested"), set())

    def test_unsafe_schemes(self):
        for destination in ("javascript:alert(1)", "data:text/plain,hello", "file:///private"):
            with self.subTest(destination=destination):
                self.assertIn("LINK011", self.codes(destination))

    def test_images_are_local_link_targets(self):
        doc = parse_document("a.md", "# T\n![Image](missing.png)\n")
        check_links(self.root, doc, {"a.md": doc})
        self.assertIn("LINK007", {issue.code for issue in doc.issues})

    def test_non_markdown_asset_needs_no_heading_parse(self):
        self.assertEqual(self.codes("image.png"), set())

    def test_unloaded_markdown_is_not_silently_accepted(self):
        self.write("unloaded.md", "# T\n")
        self.assertIn("LINK009", self.codes("unloaded.md#t"))


if __name__ == "__main__":
    unittest.main()
