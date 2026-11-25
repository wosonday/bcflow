# Windows Agent Runtime — Verified Lessons (2026-11-24, updated 2026-11-25)

Field notes from one full single-lane batch (two parallel DeepSeek workers, one NVIDIA background task, `agy` reviews and docs, `copilot-git` merges) on Windows 11 with OpenCode 2.4, `agm` 0.4.1, `hc`, `agy` and GitHub Copilot CLI 1.0. Every rule below was reproduced at least once; the evidence lives in the project notebook of the batch (`.agents/decisions/`, `.agents/reviews/`). It complements [AGENTS-TUTORIAL.md](../AGENTS-TUTORIAL.md), [HERDR-CORE-BOT.md](../HERDR-CORE-BOT.md) and the per-tool guides in this directory.

## 1. Spawning Workers on Windows

- A `hc` template whose `command:` contains arguments fails with `cannot start "sh": sh: not found on PATH`. This affects every `command: agy --model ...` example. Bare `command: opencode` works but does not pin a model.
- The OpenCode 2.4 TUI has no `--model` flag. Pin the model with the argv form instead of a template:
  `hc run --bg --name w-<id>-<n> --cwd <worktree> --label agm.task=<id> --label agm.role=worker opencode run --agent worker-strong --model opencode-go/deepseek-v4-pro "<brief pointer>"`.
- Argv spawning bypasses the `agm spawn` checks: the CEO checks `[policy].max_workers`, sets `agm budget set task:<id>` and refreshes `agm quota --refresh` by hand before each dispatch.
- Wait on a worker by **address** (`hc get <t>` until `"state":"finished"`). `hc ls` hides finished terminals, so a wait loop that looks a worker up by name never ends.
- Keep shared `hc` templates project-neutral. Templates copied from one project carried that project's routing and paths into every other project; a missing `worker-strong` template had to be replaced by the argv form.

## 2. `opencode run` Is Non-Interactive

- Every permission at `ask` level is auto-rejected, including compound commands such as `cd X && git status` that do not match the allowlist. Briefs must say: one plain command per call, from the worktree root, only allowlisted commands.
- Consequences seen in practice: workers cannot `git rebase` (route rebases to `copilot-git`), cannot use the `hc send` fallback, and cannot report to a CEO terminal that is busy (`agm send` fails with `status_mismatch: <t> is working, not idle`). The CEO therefore watches worker state and reads `.agents/log/<ID>.md`; briefs tell workers not to retry `agm send`.
- Skill names differ per tool: OpenCode exposes `tdd`, not `test-driven-development`. Write the tool's own skill names into `skills:`.

## 3. Read-Only Boundary for OpenCode Agents

Verified by asking each agent, without calling any tool, for its exact tool list under each configuration:

| Configuration | Tools exposed |
|---|---|
| `bash: "*": deny`, no `execute` rule | `glob, grep, read, ..., execute` |
| same plus `execute: deny` | `execute` gone, but about thirty plugin tools appear: `browse_*` (including `browse_eval` and `browse_upload`), `opencode_worktree_create`, `opencode_session_move`, ... |
| `"*": deny`, then `read`/`glob`/`grep: allow` | `glob, grep, read, skill` |

- `execute` is a code-mode wrapper around plugin/MCP tools. Denying it alone **widens** the agent. Read-only agents start with `"*": deny`, then allow `read`, `glob`, `grep` and only the scoped `edit`/`bash` patterns the role needs.
- `opencode_worktree_remove` stays listed even when denied by name, but a real call on a worktree inside the repository returned `Permission denied: edit`; the `edit` patterns guard it. Test such cases in a scratch repository, never on real worktrees.
- The first `opencode run` in a fresh directory sometimes prints nothing; re-run before concluding.

## 4. `agy` in Headless Mode

- `agy -p` with `--mode plan --sandbox` auto-denies every `command` tool (for example `git diff`) and still exits 0 with **empty output**. Always check the output size, never only the exit code.
- Give headless reviewers a snapshot folder: precomputed `git diff` files, the task/log/review files and the relevant sources, plus a `VERSION.txt` naming the SHAs. Reviews then read files only.
- Docs updates work with `--mode accept-edits --sandbox` (file edits only); a short worker session then commits the docs. Check the docs diff yourself: in this batch one docs step placed a new CHANGELOG bullet under an already released version, and no check caught it.
- `agy` output contains `file:///` links to local paths. Strip them before committing review text to a public repository.

## 5. Copilot CLI (`copilot -p`) as `copilot-git`

- Permission rules match only the **first** subcommand: `shell(python -m pytest)` never matches and the post-merge audit was auto-denied. `shell(git rebase)` covers `git rebase --continue`.
- A pattern that worked for a rebase with a known conflict: `--allow-tool='shell(git:*)'` plus explicit denies (`push`, `merge`, `reset`, `branch`, `tag`, `worktree`, `checkout`, `switch`, `clean`, `stash`), `--allow-tool='write(<the one conflicted file>)'` and `--add-dir` for the brief. Deny rules win over every allow.
- `shell(python:*)` allows any Python. When `Write-Output` was denied, Copilot re-printed a pytest summary with `python -c "print(...)"`. A re-printed line is not evidence: confirm that the real pytest step ran, or re-run it.
- Git `LF will be replaced by CRLF` warnings are normal on Windows; tell Copilot so, or a "stop on unexpected output" brief halts after the first commit.

## 6. Parallel Branches on the Same Module

- Two branches can each pass their own Audit and still break each other. Before merging parallel work on one module, merge both heads in a detached scratch worktree and run the full suite.
- Make expected test counts arithmetic (`base + new tests of A + new tests of B`); a green run with the wrong count is suspicious.
- If `main^{tree}` equals the tree of the audited trial merge, the measured result applies byte for byte; still re-run the Auditor after the real merge.
- After a rebase, compare `git range-diff old_base..old_head new_base..new_head`: unchanged commits show `=`, so the earlier review still covers them.
- A rebase brief states the exact expected conflict and its only allowed resolution; anything else aborts.

## 7. Review Practice That Converged

- Re-review only the delta plus the previous review; findings fell 3, 2, 1, 0 over four rounds of a concurrency rework.
- Withhold one finding you already know from the review prompt. An independent reviewer that finds it is evidence of real independence (two out of two times in this batch).
- Workers follow a precise fix specification literally, including its mistakes: one specification told the worker to mark all remaining futures failed, which discarded finished results. Have rework specifications for concurrency and exit paths pre-reviewed before dispatch.

## 8. NVIDIA Background Qualification

- `nvidia-nemotron/nvidia/nemotron-3-ultra-550b-a55b` qualified for read-only log synthesis: 5 of 5 planted expected findings, 161 s against a 15-minute deadline, snapshot hashes unchanged.
- It reported already fixed issues as open. Background briefs ask for `status: open | resolved | unknown` per finding, with evidence for `resolved`.
- Run the background agent in a snapshot folder outside the repository with the default-deny configuration of section 3; never send real model data or run logs to a cloud model.

## 9. Second Batch (2026-11-25): Twelve Task Branches, `agy`-First Reviews

Lessons from a larger single-lane batch (twelve task branches, up to three parallel DeepSeek workers, `agy` reviews, `copilot-git` merges, one NVIDIA log summary per wave).

- **Know which worker finished before auditing.** With several workers running, a wait loop returns as soon as any terminal finishes. Read the terminal address in the result (`hc get <t>` shows `"state":"finished"`) and audit that worktree only. One branch was audited while its worker was still writing, because the wrong terminal was assumed.
- **Workers cannot merge.** OpenCode workers have `python` and plain `git` commands but no `git merge`. When a task branch falls behind `main`, the CEO merges `main` into the task branch and resolves the conflict (in this batch almost always one-line exports in a package `__init__`). `copilot-git` stays on `main` and never resolves feature conflicts. The merge changes the branch `head` SHA, so Audit and Review must cover the new SHA before the branch is handed off.
- **Trial every merge first.** `git merge-tree --write-tree main feat/<x>` shows conflicts without touching any checkout. Give `copilot-git` a short merge task file (branch, exact SHA, expected conflicts, the files it may stage, the files it must never stage) and keep the allow/deny pattern of section 5.
- **Unexpected dirty files in the main checkout.** Files changed by the user (or another tool) outside the batch make the main checkout dirty, and `copilot-git` rejects a dirty checkout ([gate](COPILOT-GIT.md#2-handoff-prerequisites-before-merging)). Never stage, restore or stash them: stop and ask the user. Only with the user's explicit approval may a merge task file name those exact files as "leave as is", and only when `git merge-tree` shows the merge does not touch them.
- **Long log summaries:** `nvidia-mistral/mistralai/mistral-large-4-1t-a52b` returned usable summaries of long test and review logs when the log text was inlined in the prompt. Nemotron and Kimi timed out on the same long prompts. Keep the default-deny agent of section 3 and the `status: open | resolved | unknown` rule of section 8.
- **Quota exhaustion mid-task:** the Go 5 h window ran out once with a worker half way; the worker stopped with uncommitted edits. Continue with `agy` Hydrogen in `accept-edits` mode ([agy guide](AGY-GEMINI-OPUS.md#16-hydrogen-as-a-fallback-editor)) after checking `git status` in the worktree.
- **Auto-mode guards in Claude Code** block `git push`, branch deletion, some `git worktree remove` forms and `agy --dangerously-skip-permissions`. Do not work around them: ask the user, who can run the command with the `!` prefix.
