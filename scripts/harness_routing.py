"""Explicit, reviewable routing contracts; does not infer prose semantics."""

import re

from harness_markdown import Document, Issue, cells

FLASH = "opencode-go/deepseek-v4.1-flash"
PRO = "opencode-go/deepseek-v4-pro"
ROUTING = {
    "auditor": "script/CI",
    "explore": FLASH,
    "worker-fast": FLASH,
    "worker-mid": FLASH,
    "worker-strong": PRO,
    "reviewer": PRO,
}
BACKGROUND = {
    "background-kimi": "nvidia-kimi/moonshotai/kimi-k3.1-pro",
    "background-mistral": "nvidia-mistral/mistralai/mistral-large-4-1t-a52b",
    "background-nemotron": "nvidia-nemotron/nvidia/nemotron-3-ultra-550b-a55b",
}
SAFETY = {
    "execution": "background-only",
    "writes": "none",
    "merge-gate": "false",
    "max-concurrent": "1",
    "pilot-deadline-minutes": "15",
    "enforcement": "manual",
    "on-429": "stop-and-check",
    "stale-output": "revalidate",
    "outer-retry": "none",
    "cloud-approval": "required",
}
SKILL_ROUTING = {
    "discovery": "brainstorming",
    "system-design": "system-design",
    "adr": "architecture",
    "planning": "writing-plans",
    "test-strategy": "testing-strategy",
    "feature": "test-driven-development",
    "bugfix": "systematic-debugging",
    "review-fix": "receiving-code-review",
    "review-request": "requesting-code-review",
    "review": "code-review",
    "audit": "script/CI",
    "docs": "documentation",
    "verification": "verification-before-completion",
    "skill-authoring": "writing-skills",
    "mechanical": "none",
}
CONTRACTS = {
    "OPENCODE-WORKERS-REVIEWER.md": {"routing": ROUTING},
    "NVIDIA-BACKGROUND.md": {"background": BACKGROUND, "safety": SAFETY},
    "SKILL-ROUTING.md": {"skill-routing": SKILL_ROUTING},
}
MODELS = {
    FLASH: {"low", "high", "max"},
    PRO: {"high", "max"},
    **{model: {"low", "medium", "high"} for model in BACKGROUND.values()},
}
MODEL_ID = re.compile(
    r"(?:opencode(?:-go)?/deepseek-[\w.*-]+|nvidia-[a-z0-9_-]+/[\w.*/-]+)"
    r"(?:#[\w-]+)?",
    re.IGNORECASE,
)
RETIRED = "deepseek-v4.1-flash-free"
SECRET = re.compile(
    r"\b(?:nvapi-[A-Za-z0-9_-]{20,}|sk-(?:ant-api|proj-)[A-Za-z0-9_-]{20,})"
)


def check_model_ids(doc: Document) -> None:
    archived = doc.path.startswith("docs/superpowers/")
    for number, line in enumerate(doc.text.splitlines(), 1):
        if SECRET.search(line):
            doc.error(
                number, "SEC001", "Possible literal API credential; value redacted."
            )
        if archived:
            continue  # Design/plan history may explicitly discuss a retired ID.
        if RETIRED in line:
            doc.error(
                number,
                "MODEL001",
                "Retired/unconfirmed free DeepSeek slug in operational docs.",
            )
        for match in MODEL_ID.finditer(line):
            reference = match[0].rstrip(".")
            if reference == "opencode/deepseek-*":
                if re.search(
                    r"(?:--model\s+|model[\"']?\s*:\s*[\"'`]*)opencode/deepseek-\*",
                    line,
                ):
                    doc.error(
                        number,
                        "MODEL004",
                        "Namespace wildcard is not a runnable model selection.",
                    )
                continue  # A prose description of the excluded Personal/API namespace.
            model, _, variant = reference.partition("#")
            if model not in MODELS:
                doc.error(
                    number,
                    "MODEL002",
                    "Unknown or wrong-provider DeepSeek/NVIDIA model ID.",
                )
            elif variant and variant not in MODELS[model]:
                doc.error(
                    number,
                    "MODEL003",
                    "Variant is not in the verified catalog snapshot.",
                )


def contract(doc: Document, name: str, expected: dict[str, str]) -> dict[str, str]:
    begin = f"<!-- harness:{name}:start -->"
    end = f"<!-- harness:{name}:end -->"
    # Only standalone markers outside fenced examples define the contract.
    starts = [i for i, (_, line) in enumerate(doc.visible) if line.strip() == begin]
    ends = [i for i, (_, line) in enumerate(doc.visible) if line.strip() == end]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        doc.error(
            1, "POLICY001", f"Expected exactly one ordered {name} contract block."
        )
        return {}
    table = [
        (number, line)
        for number, line in doc.visible[starts[0] + 1 : ends[0]]
        if line.strip()
    ]
    if (
        len(table) < 2
        or len(cells(table[0][1])) != 3
        or cells(table[0][1])[0].strip() not in {"Vai trò", "Role", "Quy tắc", "Rule"}
        or not all(
            re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells(table[1][1])
        )
        or len(cells(table[1][1])) != 3
    ):
        doc.error(
            doc.visible[starts[0]][0],
            "POLICY008",
            f"Invalid {name} contract header/separator.",
        )
    rows: dict[str, str] = {}
    for number, line in doc.visible[starts[0] + 1 : ends[0]]:
        if not line.strip():
            continue
        if not line.strip().startswith("|"):
            doc.error(number, "POLICY002", f"Non-table content in {name} contract.")
            continue
        columns = [cell.strip().strip("`") for cell in cells(line)]
        if len(columns) != 3:
            doc.error(number, "POLICY002", f"Expected 3 columns in {name} contract.")
            continue
        key, value, _ = columns
        if key in {"Vai trò", "Role", "Quy tắc", "Rule"} or re.fullmatch(
            r"[-: ]+", key
        ):
            continue
        if key in rows:
            doc.error(number, "POLICY003", f"Duplicate {name} key: {key}.")
        rows[key] = value
        if expected.get(key) != value:
            doc.error(number, "POLICY004", f"Unexpected {name} assignment for {key}.")
    for key in expected.keys() - rows.keys():
        doc.error(doc.visible[starts[0]][0], "POLICY005", f"Missing {name} key: {key}.")
    return rows


def check_contracts(documents: dict[str, Document]) -> list[Issue]:
    issues: list[Issue] = []
    # docs/ holds a single (English) version of each guide, so there is no bilingual drift check.
    for filename, blocks in CONTRACTS.items():
        path = "docs/" + filename
        doc = documents.get(path)
        if doc is None:
            issues.append(
                Issue(
                    path, 1, "POLICY006", "Required policy document missing/unreadable."
                )
            )
            continue
        for name, expected in blocks.items():
            contract(doc, name, expected)
    return issues
