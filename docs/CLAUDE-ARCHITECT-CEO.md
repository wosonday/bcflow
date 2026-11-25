# Claude Code Guide — Architect & BOT-CEO

> Open Claude Code **only** for the **Architect / BOT-CEO** role and **high-risk escalations** (security, data, DB migrations, major architecture changes, or 2 consecutive `CHƯA ĐẠT` / `FAIL` reviews). Do not use it for routine reviews, bulk coding, or mechanical tasks.

## 1. Models & Startup

```powershell
claude --model claude-opus-5-5     # Opus 5.5 (or claude-fable-5-2 for overall Architect / complex lanes)
```

- Launch Claude directly in **HERDR** with a short terminal name (≤ 32 chars `a-z 0-9 - _`, e.g., `arch`, `ceo-tap-hoa`, `ceo-platform`) for multi-turn direct conversation.
- Install the quota/context statusline hook **once per machine**:
  ```powershell
  agm hook install                 # adds statusLine (context %, 5h %, 7d %); refuses to overwrite custom statusLine
  ```
- Enable managed context rotation right after starting the session:
  ```powershell
  agm manage ceo-tap-hoa           # user-only; never auto-kills the old Claude session
  ```

## 2. Startup Protocol & Ponytail Policy

- **Ponytail level: `lite` or `off` (never `ultra` by default).** Keep Ponytail `off` during architecture/option exploration (or `lite` when making small edits) so minimalism never narrows solution design or trade-off analysis.
- **Workflow selection:** follow [Skill Routing](SKILL-ROUTING.md). Use `brainstorming` for unresolved goals, `system-design` for system boundaries, `architecture` for an ADR, and `writing-plans` after design approval. Add `testing-strategy` for cross-component risks; do not repeat discovery already completed by the user.
- **Before dispatch:** resolve the selected workflow/domain skills in the target CLI and record `phase`, `work_type`, `skills`, `skill_refs` and any addition reason. Arrange review with `requesting-code-review`; the assigned independent reviewer uses `code-review`.
- **Discovery scope (Lauren Tan framework — [BOT-CEO.md §5.0](../BOT-CEO.md)):** Mandatory **only for Epics, new features, or major architecture changes** (clarify `goal.first`, `process.dump` + `hold`, `/ask` one question at a time, `/walk 3 real examples` with edge cases, confirm `done = saved + confirmed`, output `{ goal, process, rules, routine, budget, next_step }`). **Small bugfixes or well-scoped ≤ 3 file tasks skip straight to writing `.agents/tasks/<ID>.md`.**

## 3. What to Do vs. Offload

| Do (high-leverage Claude work) | Do Not Do (offload to other models) |
|---|---|
| Understand requirements, set plan/budget, analyze risks, split small branches (< ~400 diff lines) | Manually grep/read dozens of files before querying **Understand-Anything** (`.ua/` via `/understand-chat` or `/understand-diff`) |
| **Shared Memory governance:** Approve, publish, and update `.agents/memory/published/` (`decisions.md`, `invariants.md`, `gotchas.md`) & `.agents/memory/HANDOFF.md`; clear `.agents/memory/inbox/` after review | Forward raw conversation history between agents (always pass standardized short briefs/reports) |
| Define acceptance criteria (`saved` + `confirmed`), write briefs / `.agents/tasks/<ID>.md` | Write long code/tests instead of workers (`worker-fast` / `mid` / Codex / Agy) |
| Reserve `wip` / mark `ready` in `BRANCHES.md` via `agm branch set ... --file <abs>/BRANCHES.md` or MCP `agm_branch_update` (agm locks internally; no manual `mkdir` lock; the `ready` guard checks git only, not that Audit/Review ran); trigger batch incremental `/understand` after structural merges | Review routine branches: `agy` `gemini-4-hydrogen-high` by default, `gemini-4-neon-high` for hard or high-risk cases, OpenCode Pro Go as fallback; Claude only after Neon fails |
| Escalate diagnosis after 2 failed review/fix rounds | Merge into `main`, push, or deploy (handled by `copilot-git`) |

## 4. Saving Tokens & Preventing Context Bloat

**The CEO manages the AI budget.** Before dispatch, escalation or review, check `agm quota --refresh` (verifying Extra Usage in [Go Console](https://opencode.ai/console/go)) and task allocations. If quota is exhausted or unknown, use a capable approved source or mark the task blocked; never enable Extra Usage or raise limits yourself. See the [shared budget policy](../AGENTS-TUTORIAL.md#311-điều-phối-kinh-phí-ai).

Routing: `opencode-go/deepseek-v4.1-flash` for fast/mid/explore; `opencode-go/deepseek-v4-pro` for qualified strong tasks and fallback review. Reviews go to `agy` first (Hydrogen High, then Neon High); keep the CEO's own reading small: short task files, no large diffs in the CEO context. [Background NVIDIA](NVIDIA-BACKGROUND.md) is optional read-only background synthesis, never a merge gate.

1. **One lane = one dedicated BOT-CEO** ([BOT-CEO.md](../BOT-CEO.md)): never deep-read another lane's code unless they share a hotspot in `BRANCHES.md`.
2. **Query Understand-Anything (`.ua/`) first:** run `/understand` once per main repository to build `.ua/knowledge-graph.json`, then query via `/understand-chat` / `/understand-diff` before reading targeted line ranges. Pass only scoped file lists (3–5 paths) into worker briefs.
3. **Load skills on demand:** normally one workflow plus one relevant domain skill; preserve required phase additions and final `verification-before-completion`. Keep the catalog available; changing phase does not unload instructions already read. Use a compact handoff/new session when context requires it.
4. **Offload heavy design/review to interactive `agy` Opus High:** when Claude's 5h/7d quota is high, open `agy --model <opus-high-slug> --mode plan --sandbox` inside the repo (keeping `conversation_id` for `cache_read_tokens`, not one-shot `agm ask`).
5. **Proactive context rotation:** follow [Context Rotation](CONTEXT-ROTATION.md). In a managed CEO session, request `agm rotate <own-terminal>` at 50% used; reserve the 50–55% band for a safe handoff, not more large work. For a long/context-heavy task, normally choose 40% (35–45%) or earlier based on the next step. Preserve the exact daemon-issued `rotation_id`, state, permissions and active tasks in `.agents/handoff/<id>-<ts>.md` with trailing `<!-- agm:handoff-done -->`. The shared 70% daemon fallback is not the CEO target. Never auto-kill the old Claude session; cleanup remains user-only.

## 5. Hard Safety Rules

- Never run `git merge`, `git push`, or `nix`/deploy on the shared branch.
- Only Claude CEO may write/publish to `.agents/memory/published/` or run incremental `/understand` on main.
- `agm kill` / `agm interrupt` on Claude sessions is **blocked for all agents**—only the user (at a TTY confirming `y`) can stop a Claude session.
