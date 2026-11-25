# Harness Lint — Execute Checks, Fail on Errors


For the `agent-tutorial` collection; does not edit documents or OpenCode settings. Requires Python **3.11+**, not Git, networking, API keys or third-party packages.

## 1. Required Handoff Commands

Run from the collection root:

```powershell
python -B scripts/lint_harness.py
python -B -m unittest discover -s tests -v
```

Lint resolves its default root from the script location, not the current working directory. Use `--root <directory>` for complete fixtures/copies and `--format json` for machine-readable output:

```powershell
python -B scripts/lint_harness.py --format json
```

**Exit 0**: all checks completed without errors. **Exit 1**: lint errors; **2**: invalid CLI arguments. Traversal/read errors or missing required documents are not a pass. Text output: `file:line: CODE message`.

## 2. Coverage

| Group | Codes | Gate |
|---|---|---|
| Text/Markdown | `ENC*`, `MD*` | UTF-8, no BOM/NUL/trailing whitespace/conflict markers, final newline, one H1, no heading jumps, closed fences, table column counts |
| Links | `LINK*` | Existing relative files, root containment, exact case, heading/HTML anchors, URL-encoded paths/fragments, images and defined reference links |
| Models | `MODEL*` | Verified DeepSeek Go/NVIDIA IDs and snapshot variants; rejects obsolete free IDs and Personal/API substitutions in operational docs |
| Routing | `POLICY*` | Complete, correct, unique model roles and phase workflows; no NVIDIA worker/reviewer assignments; NVIDIA safety contract matches policy A; Ponytail cannot replace debug/review/verification workflows |
| Single language | `POLICY006` | `docs/` is English-only: each policy guide exists once under `docs/`; the root rule documents stay Vietnamese |
| Secrets | `SEC001` | Selected common NVIDIA/Anthropic/OpenAI key patterns; values are not printed in diagnostics |
| Input | `IO*` | Valid root, required documents, traversable/readable inputs; symlinks are not silently followed outside the tree |

Scans every `.md` under the root except `.git`, `.wt`, `.agents`, `.opencode`, `.ua`, `.venv`, `node_modules`, `__pycache__`. Fenced/inline code examples are not treated as live links/headings. Symlinks outside ignored directories fail without reading their target tree.

Obsolete-model checks exclude history under `docs/superpowers/` so designs can explain old decisions; **Markdown, links and secret patterns are still checked**. Current defaults belong only in operational guides.

## 3. Machine-Checkable Routing Contracts

- The [OPENCODE-WORKERS-REVIEWER.md](OPENCODE-WORKERS-REVIEWER.md) pair uses `harness:routing`.
- The [NVIDIA-BACKGROUND.md](NVIDIA-BACKGROUND.md) pair uses `harness:background` and `harness:safety`.
- [SKILL-ROUTING.md](SKILL-ROUTING.md) uses `harness:skill-routing` for the approved phase workflows, including distinct review request, execution and feedback roles. Removing the guide or weakening a route fails lint.
- Start/end markers must be standalone, ordered and unique outside fences. Deleting a table, omitting rows, duplicating roles or hiding markers in examples cannot pass.
- Canonical assignments and variant snapshots live in `scripts/harness_routing.py`. Policy/model changes require an approved harness task updating the canonical contract, the `docs/` guide and tests; do not loosen checks just to make them green.
- These are document contracts, not installed agents, schedulers or enforced billing limits.

## 4. Tests That Prove Rejection

Tests create independent valid/invalid fixtures covering broken links/anchors, Unicode/parentheses/case-sensitive paths, traversal, broken fences, malformed tables, wrong provider/model/variant, missing/duplicate/unsafe contracts, both translations being wrong, missing/invalid-UTF-8 files and CLI exit codes.

`tests/test_guardrail_examples.py` also extracts the actual Bash/CI examples from both ESLint guides and executes them in temporary Git fixtures: invalid bases must fail; TS/MJS/MTS/CTS bypasses must be rejected; nested lint configs must fail CI; clean diffs and filenames containing bypass keywords must pass. This group needs Git + Bash (Git Bash preferred on Windows), without modifying user Git configuration. Missing tools produce explicit **skips**; report shell coverage as unverified rather than claiming full verification. Document lint itself still needs only Python.

Run lint and tests. Pretty output or positive tests alone do not prove the checker works; negative cases must actually be rejected.

`tests/test_skill_routing.py` rejects deleted/fenced matrices, missing debugging/verification, Ponytail substituted for a workflow and confused review responsibilities. The Bash fixture group also executes the HERDR dispatch example with zero, three and four selected skills, intercepting `agm` so no agent is started. The wrapper must not impose a hard two-skill cap. These tests do not establish actual skill installation or model behavior in any CLI.

## 5. Explicit Limitations

- This is a targeted Markdown checker for this collection, not a full CommonMark parser. It does not support all extensions, shortcut references, autolinks, HTML links or complex heading slugs; use explicit Markdown links in new docs.
- No external HTTP requests, quota/pricing checks, API benchmarks or tool-calling qualification.
- It cannot prove full translation equivalence or detect every prose contradiction. Automated gates protect **contract tables**; explanatory prose still requires independent human/model review.
- Pattern scanning is not a complete secret scanner. Never put credentials in the repository and rely on lint to protect them.
- Does not replace application ESLint/typechecks/tests. Code guardrails remain in [ESLINT-CI-GUARDRAILS.md](ESLINT-CI-GUARDRAILS.md); this checker does not authorize lint-config edits in other projects.
