# Skill Routing by Phase, Role and Work Type

Approved on 2026-11-23. This guide restores workflow selection alongside Ponytail in [AGENTS-TUTORIAL.md](../AGENTS-TUTORIAL.md). It is dispatch guidance, not a skill installer or a new scheduler.

## 1. Compose Three Layers

1. **Behavior:** Ponytail controls completeness, reuse and unnecessary complexity. It does not replace design, debugging, testing or review workflows and does not consume a workflow/domain slot.
2. **Workflow:** select the skill for the current phase from the matrix below. Start with one workflow; add another only when a concrete phase requirement calls for it.
3. **Domain:** normally add one skill for the actual stack or artifact. Rust, frontend, database and document skills are task-specific, not a universal startup bundle.

**One workflow plus one domain skill is a default, not a hard cap.** Final `verification-before-completion` and requirements imposed by the active client or project must not be dropped to meet a number. Record additional skills and their reason in the brief or task log. Mechanical work may use no workflow/domain skill; its required checks still run.

Keep skills discoverable and read their full instructions only when relevant. Changing phase changes what to select next; it does not uninstall skills or erase instructions already read from the conversation. Start a fresh session with a compact handoff if accumulated context becomes a problem.

For CEO and Codex High, use [Context Rotation](CONTEXT-ROTATION.md): proactively request a managed handoff at 50% context used, with a safe handoff band through 55%; choose an earlier target (normally 40%) for long/context-heavy work. Preserve selected skills, resolved references, approved permissions and active task IDs in the successor's handoff instead of restarting discovery or duplicating work.

## 2. Canonical Workflow Matrix

The first column contains logical phase IDs, not CLI commands. The third column identifies the owner and the condition for any additional skill. Lint rejects deleted or weakened rows.

<!-- harness:skill-routing:start -->
| Role | Primary workflow | Owner and selection condition |
|---|---|---|
| `discovery` | `brainstorming` | Claude CEO or agy Co-Architect: clarify a new or ambiguous goal; optionally use `grill-me` for unresolved assumptions instead of a second duplicate interview |
| `system-design` | `system-design` | Claude or agy Opus/Argon: boundaries, APIs, data flow, scale, reliability and trade-offs; Codex may advise on hard engineering constraints |
| `adr` | `architecture` | Architect: record or evaluate a concrete decision and its consequences; use after design when an ADR is needed |
| `planning` | `writing-plans` | Claude CEO or Codex manager: translate an approved design into scoped tasks and acceptance commands |
| `test-strategy` | `testing-strategy` | Manager or senior engineer: plan coverage across components, critical paths or missing test architecture |
| `feature` | `test-driven-development` | Workers, Codex Dev or agy Dev: implement non-trivial logic with behavior tests; add the relevant domain skill |
| `bugfix` | `systematic-debugging` | Capable worker, Codex or agy: reproduce and find the cause before editing; add domain knowledge for the failing area |
| `review-fix` | `receiving-code-review` | Original implementer: validate feedback against the code; add debugging if the cause is still unknown |
| `review-request` | `requesting-code-review` | Manager or authorized implementer: package requirements, exact base/head and test evidence for the existing independent reviewer |
| `review` | `code-review` | Fresh independent Codex, agy or qualified OpenCode reviewer: correctness, security, performance and tests; add a relevant domain skill |
| `audit` | `script/CI` | Deterministic lint, typecheck, tests, build and scope checks; call an agent only to interpret a failure |
| `docs` | `documentation` | agy Docs: technical docs, API docs, runbooks and reports; add an artifact skill only for the requested format |
| `verification` | `verification-before-completion` | Implementer, reviewer, manager or Copilot: run the required checks, inspect actual output and report limitations before claiming completion |
| `skill-authoring` | `writing-skills` | Assigned agy Argon or Codex: change reusable skills; use `skill-creator` when creating a new skill, not for every harness wording edit |
| `mechanical` | `none` | Worker: scoped rename, formatting or simple wording; no discovery/TDD ritual, but retain project checks and final verification |
<!-- harness:skill-routing:end -->

The matrix chooses a workflow, not a model. Existing model qualification, quota, review independence, permissions and release gates remain in force. Do not promote a worker just because a skill name is available.

## 3. Role Boundaries and Ponytail

| Role | Ponytail | Skill boundaries |
|---|---|---|
| Claude CEO / Architect | `off` for exploration, `lite` for a focused decision | Discovery, design/ADR and planning as separate phases; use available evidence rather than repeating an approved discovery interview |
| Codex manager | `lite` for planning; `full` for scoped engineering | `writing-plans`; `using-git-worktrees` only when isolation must be created or verified; request review without becoming the routine reviewer |
| Codex Developer | `full` | Feature, bugfix or review-fix workflow plus the actual domain |
| agy Co-Architect / design advisor | `off` or `lite` | `system-design` / `architecture` when designing; no coding workflow solely because the CLI can edit |
| agy Developer | `full` | Same feature/bugfix/review-fix selection as other implementers |
| Codex / agy / OpenCode Reviewer | `full` | `code-review` plus relevant domain knowledge; Ponytail checks complexity and reuse without replacing correctness/security review |
| OpenCode workers | Compact inline rules | Use the approved brief and current implementation phase; no self-spawning, managerial discovery loop or scope expansion |
| agy Docs | `lite` only when useful; otherwise `off` | `documentation`; no extra model/skill pass for a trivial report |
| agy Argon harness advisor | `off` / `lite` for analysis; `full` for a scoped edit | Use evidence in logs/reviews; invoke skill-authoring tools only when reusable skills actually change |
| Audit / explore / Copilot | `off` by default | Scripts first for Audit/Git; scoped read/search for explore; verification before completion |

`ultra` is never the default. Changing Ponytail mode must not silently broaden write access, narrow approved requirements or cancel checks.

### Review Is Three Different Jobs

- `requesting-code-review` belongs to the role arranging review. In this harness, hand the package to the existing assigned reviewer; do not spawn a duplicate reviewer or bypass role authorization because a generic skill suggests delegation.
- `code-review` belongs to the independent reviewer. The review report identifies the input base/head and provides file:line evidence. The skill alone does not authorize writes to product code.
- `receiving-code-review` belongs to the implementer processing feedback. It is not an instruction to agree with every finding or a substitute for independent review.

`ponytail-review` may supplement a specific complexity/reuse check when available. It is not a replacement for `code-review`, and it does not add another mandatory review stage.

## 4. Domain Selection and Availability

| Task | Domain skill to consider | Boundary |
|---|---|---|
| Rust logic, ownership or error handling | `rust-best-practices` | Select for Rust changes, not every worktree |
| Streamlit implementation or troubleshooting | `developing-with-streamlit` | Follow the installed Streamlit version and the skill's own scope |
| React changes | Available React best-practices skill | Use the actual installed name/namespace; no assumed generic skill ID |
| SQL, schema or database behavior | Available database/provider skill | Match the existing provider and task; do not migrate a backend to fit a skill |
| DOCX, PDF, slides or spreadsheets | Relevant artifact skill | Select by requested output format; plain Markdown docs use `documentation` |

The matrix uses logical skill names. **Available in Codex does not imply available in Claude Code, OpenCode or agy.** Before dispatch, the manager resolves each selected name to an installed skill ID/path or an approved equivalent playbook in that runtime. Record the resolved reference in `skill_refs`; a cache directory alone is not proof that a client exposes it.

If a required skill is unavailable, report the gap. Use an existing approved equivalent only when it covers the workflow and is recorded explicitly; otherwise mark the affected phase blocked and request installation separately. Do not claim an unavailable skill ran or silently replace it with Ponytail. This update installs no plugins or skills globally.

## 5. Task Brief and Phase Changes

Retain a **single-line, comma-separated `skills:` field**, as consumed by the documented dispatch wrapper. Do not use YAML block lists there. An empty field means no selected workflow/domain skill; it does not waive verification.

The brief records `phase`, `work_type`, `ponytail`, `skills`, resolved `skill_refs`, any `skill_reason` for extra skills, `files_allowed`, relevant context refs, goal, exclusions and `verify`/Done-when commands. These are coordination fields, not new AGM configuration keys.

### Example: Feature

```text
task_id: add-total
owner: worker-mid
tier: vừa
phase: implementation
work_type: feature
ponytail: compact
skills: test-driven-development, rust-best-practices
skill_refs: resolve and record both installed references before dispatch
files_allowed: src/total.rs, tests/total.rs, .agents/log/add-total.md
goal: Return the documented total, including empty input and overflow behavior.
out_of_scope: Changing storage or the public API.
verify: cargo test --offline; cargo clippy --all-targets -- -D warnings
```

### Example: Bugfix and Review Feedback

```text
task_id: fix-total
owner: codex-dev
tier: mạnh
phase: review-fix
work_type: bugfix
ponytail: full
skills: receiving-code-review, systematic-debugging, rust-best-practices, verification-before-completion
skill_reason: Validate the finding, diagnose its unresolved cause, apply Rust constraints and verify the fix.
skill_refs: resolve and record all four installed references before dispatch
files_allowed: src/total.rs, tests/total.rs, .agents/log/fix-total.md
goal: Reproduce and fix the confirmed overflow finding.
out_of_scope: Unrelated refactors.
verify: cargo test --offline; cargo clippy --all-targets -- -D warnings
```

### Example: Mechanical Work

```text
task_id: rename-label
owner: worker-fast
tier: nhanh
phase: implementation
work_type: mechanical
ponytail: compact
skills:
skill_refs: none selected; retain the final verification protocol
files_allowed: docs/labels.md, .agents/log/rename-label.md
goal: Apply the approved terminology change.
out_of_scope: Changes to code or policy.
verify: Run the project's Markdown/link checks and inspect the diff.
```

At completion, apply `verification-before-completion` (or its explicitly approved runtime playbook), record checks and skipped work, and preserve the existing audit/reviewer gates. For an active mandatory client workflow, follow its higher-priority instructions rather than treating the matrix as a way to disable it.

The implementer may add final verification or a client-mandated skill without waiting for a skill-count approval. For another workflow/domain addition, stay within existing task authorization and record the reason. The manager updates the brief or handoff when the phase changes; discovery/planning questions remain with the authorized coordinator.

## 6. Verification and Sources

Run `python -B scripts/lint_harness.py` and `python -B -m unittest discover -s tests -v` from the collection root. Contract tests reject missing phases, Ponytail replacing debugging/review, and swapped review responsibilities. Bash fixture tests execute the actual dispatch example with zero, three and four selected skills while intercepting agent launch.

These checks verify document contracts and the example wrapper. They do not prove that every CLI discovers a skill or that a model applies it correctly; resolve and smoke-check the intended runtime separately before operational rollout.

- [Ponytail rules](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md)
- [OpenAI skill discovery and progressive disclosure](https://learn.chatgpt.com/docs/build-skills)
- [OpenCode skill discovery, loading and permissions](https://opencode.ai/docs/skills/)
