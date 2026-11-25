"""Small, offline Markdown checks for this harness, not a CommonMark parser."""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True, order=True)
class Issue:
    path: str
    line: int
    code: str
    message: str


@dataclass
class Document:
    path: str
    text: str
    visible: list[tuple[int, str]] = field(default_factory=list)
    anchors: set[str] = field(default_factory=set)
    links: list[tuple[int, str]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    def error(self, line: int, code: str, message: str) -> None:
        self.issues.append(Issue(self.path, line, code, message))


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$")
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
LINK_START = re.compile(r"!?\[[^\]\n]*\]\(")
REFERENCE = re.compile(r"!?\[([^\]\n]+)\]\[([^\]\n]*)\]")
DEFINITION = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)(?:\s+.*)?$")
DESTINATION = re.compile(r"""^(<[^>]+>|\S+?)(?:\s+["'].*["'])?$""")
HTML_ANCHOR = re.compile(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', re.IGNORECASE)
SEPARATOR = re.compile(r"^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$")


def slug(heading: str) -> str:
    heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
    heading = re.sub(r"<[^>]*>", "", heading).replace("`", "").lower().strip()
    return "".join(
        char
        for char in heading
        if char in "_- " or unicodedata.category(char)[0] in "LMN"
    ).replace(" ", "-")


def label(value: str) -> str:
    return " ".join(value.split()).casefold()


def cells(line: str) -> list[str]:
    return re.split(r"(?<!\\)\|", line.strip().strip("|"))


def visible_lines(doc: Document) -> None:
    opened: tuple[str, int, int] | None = None
    previous_level = 0
    h1_count = 0
    for number, line in enumerate(doc.text.splitlines(), 1):
        if line.rstrip() != line:
            doc.error(number, "MD001", "Trailing whitespace.")
        if re.match(r"^(?:<{7}|={7}|>{7})(?:\s|$)", line):
            doc.error(number, "MD002", "Unresolved merge conflict marker.")
        match = FENCE.match(line)
        if match:
            marker, suffix = match.groups()
            if opened is None:
                opened = (marker[0], len(marker), number)
            elif (
                marker[0] == opened[0]
                and len(marker) >= opened[1]
                and not suffix.strip()
            ):
                opened = None
            continue
        if opened is not None:
            continue
        doc.visible.append((number, line))
        doc.anchors.update(HTML_ANCHOR.findall(line))
        match = HEADING.match(line)
        if not match:
            continue
        level = len(match[1])
        if level > previous_level + 1:
            doc.error(number, "MD003", "Heading level skips a parent.")
        h1_count += level == 1
        previous_level = level
        base = slug(match[2])
        anchor, index = base, 0
        while anchor in doc.anchors:
            index += 1
            anchor = f"{base}-{index}"
        doc.anchors.add(anchor)
    if opened is not None:
        doc.error(opened[2], "MD004", "Unclosed fenced code block.")
    if h1_count != 1:
        doc.error(1, "MD005", "Expected exactly one H1 heading.")
    if not doc.text.endswith("\n"):
        doc.error(max(1, len(doc.text.splitlines())), "MD006", "Missing final newline.")


def inline_links(doc: Document, number: int, line: str) -> None:
    for match in LINK_START.finditer(line):
        start, depth, escaped = match.end(), 1, False
        end = start
        while end < len(line) and depth:
            char = line[end]
            if not escaped:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
            escaped = char == "\\" and not escaped
            end += 1
        if depth:
            doc.error(number, "LINK001", "Unclosed inline link destination.")
            continue
        raw = line[start : end - 1].strip()
        destination = DESTINATION.fullmatch(raw)
        if destination is None:
            doc.error(
                number,
                "LINK002",
                "Invalid link destination; encode spaces or use <...>.",
            )
            continue
        target = destination[1]
        doc.links.append((number, target[1:-1] if target.startswith("<") else target))


def collect_links(doc: Document) -> None:
    definitions: dict[str, str] = {}
    references: list[tuple[int, str]] = []
    for number, original in doc.visible:
        line = INLINE_CODE.sub("", original)
        if line.lstrip().startswith("<!--"):
            continue
        definition = DEFINITION.match(line)
        if definition:
            name, target = label(definition[1]), definition[2].strip("<>")
            if name in definitions:
                doc.error(number, "LINK003", "Duplicate reference-link definition.")
            definitions[name] = target
            doc.links.append((number, target))
            continue
        inline_links(doc, number, line)
        for match in REFERENCE.finditer(line):
            references.append((number, label(match[2] or match[1])))
    for number, name in references:
        if name not in definitions:
            doc.error(number, "LINK004", f"Undefined reference label: {name}.")


def check_tables(doc: Document) -> None:
    lines = doc.visible
    for index, (number, line) in enumerate(lines):
        if not SEPARATOR.fullmatch(line) or index == 0:
            continue
        header_number, header = lines[index - 1]
        if "|" not in header or header_number != number - 1:
            doc.error(number, "MD007", "Table separator has no adjacent header.")
            continue
        width = len(cells(header))
        if len(cells(line)) != width:
            doc.error(number, "MD007", "Table separator width differs from its header.")
        for row_number, row in lines[index + 1 :]:
            if not row.strip().startswith("|"):
                break
            if len(cells(row)) != width:
                doc.error(
                    row_number, "MD007", "Table row width differs from its header."
                )


def parse_document(path: str, text: str) -> Document:
    doc = Document(path, text)
    if text.startswith("\ufeff"):
        doc.error(1, "ENC001", "UTF-8 BOM is not allowed.")
    if "\x00" in text:
        doc.error(1, "ENC002", "NUL byte in document.")
    visible_lines(doc)
    collect_links(doc)
    check_tables(doc)
    return doc


def check_links(root: Path, doc: Document, documents: dict[str, Document]) -> None:
    for number, destination in doc.links:
        parts = urlsplit(destination)
        if re.match(r"^[A-Za-z]:", destination):
            doc.error(number, "LINK005", "Use relative paths, not Windows drive paths.")
            continue
        if parts.scheme or parts.netloc:
            if parts.scheme.lower() in {"javascript", "data", "file", "vbscript"}:
                doc.error(number, "LINK011", "Unsafe/nonportable link scheme.")
            continue  # Offline lint does not request external URLs.
        decoded = unquote(parts.path)
        if "\\" in decoded or decoded.startswith("/"):
            doc.error(
                number, "LINK005", "Use portable relative paths with forward slashes."
            )
            continue
        target = (root / doc.path).parent / decoded if decoded else root / doc.path
        target = target.resolve()
        if not target.is_relative_to(root):
            doc.error(number, "LINK006", "Local link escapes the document root.")
            continue
        if not target.is_file():
            doc.error(number, "LINK007", f"Missing local link target: {destination}.")
            continue
        relative = target.relative_to(root).as_posix()
        if not exact_case(root, relative):
            doc.error(
                number, "LINK008", f"Link path has incorrect casing: {destination}."
            )
        if parts.fragment and target.suffix.lower() == ".md":
            linked = documents.get(relative)
            if linked is None:
                doc.error(
                    number,
                    "LINK009",
                    f"Linked Markdown could not be checked: {relative}.",
                )
            elif unquote(parts.fragment) not in linked.anchors:
                doc.error(number, "LINK010", f"Missing heading/anchor: {destination}.")


def exact_case(root: Path, relative: str) -> bool:
    parent = root
    for part in Path(relative).parts:
        if part not in {entry.name for entry in parent.iterdir()}:
            return False
        parent /= part
    return True
