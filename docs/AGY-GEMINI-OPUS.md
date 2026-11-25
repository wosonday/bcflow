# `agy` Guide — Gemini (Argon, Hydrogen, 3.8 Flash) & Opus High

Antigravity CLI (`agy`) absorbs **architecture offloading, technical advising, harness optimization, research, and docs/report updates** to protect Claude Code and Codex quotas.

> **General `agy` rules:**
> - Check available models for your signed-in Google account with `agy models`.
> - `agy` does **not** enforce `.opencode/agent/*.md` permissions. Read-only one-shot lookups must use `--mode plan --sandbox` outside the repo (or `agm ask`, which runs in temp dir `agmag-ask` and records tokens to `agm.db`). **Never use `--dangerously-skip-permissions`.**
> - Never include secrets (`.env*`), passwords, or real customer data in prompts.

---

## 1. Four Model Groups via `agy`

### 1.1. Opus High via `agy` — Co-Architect & Senior Reviewer (offloads Claude Code)
- **Purpose:** directly replace Claude Code for the two most reasoning-heavy tasks when preserving Claude 5h/7d quota:
  1. **Co-Architect / Design & Task Splitting:** multi-turn design discussions to lock `.agents/decisions/<ID>.md` or split `.agents/tasks/<ID>.md` with the user / BOT-CEO.
  2. **Senior Reviewer / Hard-Case Arbitrator:** review complex/high-risk branches or arbitrate when the default Reviewer returns `CHƯA ĐẠT` or is uncertain (routine branches skip Opus High pre-review to avoid paying for two large-model passes).
- **Optimal execution (keep an interactive session in-repo for `cache_read_tokens`, do NOT use one-shot `agm ask`):**
  - Launch inside the repo or worktree (`--mode plan --sandbox`) via `sparring-opus` or:
    ```powershell
    agy --model <opus-high-slug> --mode plan --sandbox
    # Resume an ongoing session:
    agy --model <opus-high-slug> --mode plan --sandbox --conversation <conversation-id>
    ```
    *(**[CHECK]** get exact `<opus-high-slug>` from `agy models`.)*
  - **Why not `agm ask` for Opus High:** `agm ask` starts a fresh temp session (`agmag-ask`) per call → `cache_read_tokens = 0` (always pays 100% fresh input tokens) and forces pasting raw diffs into CLI args (~32k char limit on Windows). Keeping an interactive session in-repo lets `agy` read exact files and leverage prompt caching while `agm` still tracks tokens from `transcript.jsonl`.

### 1.2. Gemini Argon (`gemini-4-argon-medium` / `high`) — Technical Advisor & Harness Optimizer
- **Two core responsibilities:**
  1. **Technical Advising & Deep Review:** read code/diffs via `agm ask --model gemini-4-argon-high ...` or module-grouped scripts like `agmag/tools/argon_review.py` (keep prompts < ~28k chars on Windows). Also usable as default/secondary Reviewer off Codex quota.
  2. **Cross-Session Harness Optimization:** after each batch/session, synthesize evidence from `.agents/reports/`, `.agents/log/`, `.agents/reviews/`, and `agm inbox` (recurring failures ≥ 2 times, `:free` models failing `CHƯA ĐẠT`, `BRANCHES.md` hotspot collisions) to **update `AGENTS.md`, `CLAUDE.md`, `BOT-CEO.md`, templates, skills, and `tier:` mappings**.
- **Harness editing rules:**
  - Read-only advising/review: `agm ask` / `--mode plan --sandbox`.
  - Editing harness files: run `argon-advisor` on a **dedicated branch/worktree** (`harness/<topic>`), document the root cause and expected improvement in `.agents/decisions/<ID>.md`, and commit. Any relaxation of hard rules/permissions/gates (Class B) requires explicit user approval.

### 1.3. Gemini 4 Hydrogen (`low` / `medium` / `high`) & 3.8 Flash — Docs, Reports & Diff Review (lightweight first)
- **Prefer lightweight tiers for small docs & final reports (`.agents/reports/<ID>.md`):**
  - For **summarizing `.agents/reports/<ID>.md`** (from existing `log/` + `reviews/`) or **1–2 short doc edits**: use **`gemini-3.8-flash-medium` or `gemini-4-hydrogen-low`**.
  - Use **`gemini-4-hydrogen-medium`** for longer architectural docs or multi-file `docs/*.md` updates after Reviewer approval (before calling GitHub Copilot).
  - **Workflow:** read `.agents/tasks/<ID>.md`, `.agents/log/<ID>.md`, `.agents/reviews/<ID>-*.md`, and `git diff`; update affected docs + `.agents/reports/<ID>.md`, append to `.agents/log/<ID>.md`, and commit on the branch to produce the final `head` SHA for `copilot-git`.
- **`gemini-4-hydrogen-high` (optional diff-bundle review):**
  - Useful for reviewing packaged spec/doc diffs outside the repo.

### 1.4. Gemini 3.8 Flash (`gemini-3.8-flash-low` / `medium`) — Default for Fast Research & Summaries
- **Role:** **first-choice default** for `agm ask` web research (`search_web`) and quick summaries (~7s observed). Escalate to `gemini-4-argon-low/medium` only when multi-source synthesis is needed.
- **Verification rule:** Gemini research output is **advisory only** (it once misreported an npm package version despite citing the right URL). Always verify versions, prices, and CLI flags with source commands (`npm view`, `--help`, official docs) before writing `.agents/tasks/<ID>.md`.

### 1.5. Reviewer Routing: Hydrogen High → Neon High → Claude
- **User decision (2026-11-24):** `agy` does the reviews and keeps progress moving, to save Claude Opus quota.
  1. **Default Reviewer:** `gemini-4-hydrogen-high`, `--mode plan --sandbox`, input given as a file (section 4).
  2. **Hard analysis and high-risk changes:** `gemini-4-neon-high` (`-xhigh` if needed). Also use Neon whenever Hydrogen wrote the code, so the reviewer is never the author. Hydrogen and Neon are both Gemini 4: the user accepts this cross-tier pair as the reviewer for routine code, but code that Gemini wrote for a high-risk change also gets a different-family review (OpenCode Pro, or Claude).
  3. **Claude Opus:** only for problems Neon also fails on, or for the high-risk escalations named in the project `AGENTS.md`.
  4. **Fallback:** OpenCode `reviewer` (`opencode-go/deepseek-v4-pro`) when `agy` quota is unavailable.
- **Neon pre-review of high-risk specs:** before dispatching a rework specification that touches concurrency, exit paths or data formats, give Neon the specification **plus the real source files** it changes. Workers follow a specification literally, including its mistakes.
- **Docs reviews:** add a dump of the real public signatures (for Python, an `ast` listing of public functions and parameters) so the reviewer can check every documented name and argument against the code.
- **Splitting Minor findings:** when a `PASS` review leaves several independent Minor findings, dispatch them as small parallel follow-up tasks (one worktree each) instead of one long rework round.

### 1.6. Hydrogen as a Fallback Editor
- When the Go 5 h quota runs out, `agy --model gemini-4-hydrogen-high|medium --mode accept-edits --sandbox -p="..."` inside the task worktree can carry on: it reads and edits files only. The CEO runs the tests and commits; a different model (Neon) reviews the result.
- A worker that stops because its quota ran out leaves uncommitted edits. Check `git status` in the worktree and decide to keep or discard them before dispatching the next editor.

---

## 2. Shared Context, Memory & Ponytail Policy

- **Phase and role skills:** follow [Skill Routing](SKILL-ROUTING.md). Co-Architect uses `system-design`, adding `architecture` for an ADR; Senior Reviewer uses `code-review`; a developer uses feature/debugging/review-fix workflows; Docs uses `documentation`. Argon uses `writing-skills` when reusable skills change, or `skill-creator` for a new skill, rather than for every harness wording edit.
- **Availability and verification:** resolve skill IDs/paths in agy before dispatch and record `skill_refs`. Do not assume Codex/Claude plugins are visible here. Default to one workflow plus one relevant domain skill, allow justified phase additions, and retain final `verification-before-completion` or its approved runtime playbook.
- **Understand-Anything (`.ua/`):** Install via `install.ps1` (Windows) or `install.sh antigravity` (Linux/macOS) and query the committed `.ua/knowledge-graph.json` read-only instead of scanning the entire repository.
- **Shared Memory permissions (`Propose, review, flag conflicts`):** Read `.agents/memory/published/` and task logs; write proposals and conflict flags to `.agents/memory/inbox/<ID>-agy.md` (with `type: proposal | conflict`) and review notes to `.agents/reviews/`. Never edit `.agents/memory/published/` directly—only Claude CEO publishes. Do not self-review code authored by `agy`.
- **Ponytail by role (never `ultra` by default):** design/advisory exploration uses `off` / `lite`; implementation and independent review use `full`; short docs use `off` / `lite` only when helpful. Ponytail complements the workflow and does not remove approved requirements or domain checks.

## 3. Token Tracking in `agm`

- **`agm ask` sessions:** usage and summaries are recorded directly in `agm.db` and deduplicated against transcript files.
- **Interactive `agy` CLI sessions:** `agm` scans `transcript.jsonl` under `~/.gemini/antigravity-cli/brain/` and correlates `antigravity` terminals by working directory.
- **Note:** Antigravity IDE chats do not write `transcript.jsonl`, so they show step summaries only (no token counts unless invoked via `agm ask`).

## 4. Headless Mode (`agy -p`)

- With `--mode plan --sandbox`, every `command` tool (for example `git diff`) is auto-denied and the run can exit 0 with empty output. Check the output size, and give reviewers a snapshot folder with precomputed diffs and files.
- **Pass long prompts as a file.** Windows caps one command line at about 32 KB, so an inline `-p` with a diff breaks. Write `REVIEW-INPUT.md` (task, design decisions, `git diff main...HEAD`, instructions) into a scratch folder, `cd` there and run `agy --model gemini-4-hydrogen-high --mode plan --sandbox -p="Use only your file-read tool (never run commands) to read REVIEW-INPUT.md in the current folder, then follow its instructions."`
- Docs updates can run with `--mode accept-edits --sandbox`; commit them from a short worker session after checking the diff.
- Strip `file:///` links to local paths from review text before committing it to a public repository.
- Details: [WINDOWS-AGENT-RUNTIME.md](WINDOWS-AGENT-RUNTIME.md#4-agy-in-headless-mode).
