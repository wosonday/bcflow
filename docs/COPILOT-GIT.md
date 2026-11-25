# GitHub Copilot CLI (`copilot`) Guide — Git Version Manager

GitHub Copilot CLI (`copilot-git`) is the **only agent** permitted to integrate (merge), manage history (`git revert`), and clean up merged worktrees/branches on the shared branch (`main`). It runs on your GitHub Copilot account and does not consume Claude or ChatGPT/Codex quotas.

## 1. Startup & Scope

- **Skill selection:** use the Git playbook and [final verification workflow](SKILL-ROUTING.md). Integration normally needs no discovery, design, coding or independent code-review skill. Ponytail is off by default; `verification-before-completion` (or its approved runtime playbook) supports exact-SHA and post-merge checks without waiving them.
- Run **exactly 1 `copilot-git` session** in the **main checkout** (on `main`) so integrations are strictly **sequential**—one branch/feature per merge.
- Launch command:
  ```powershell
  copilot
  # Opening prompt: Read AGENTS.md (and BOT-CEO.md / GIT_BOT.md if present). You are copilot-git; process the queue strictly by verified SHA.
  ```
- **Verified (2026-11-18):** Copilot CLI 1.0.113 passed 3/3 Git smoke tests on an isolated fixture (halted on dirty checkout, rejected mismatched SHA, merged exact SHA—see `COPILOT_GIT_SMOKE.md` in `MATHY-AI`).

---

## 2. Handoff Prerequisites (Before Merging)

Depending on project mode, `copilot-git` receives work from:
- **Multi-lane (`BOT-CEO.md`):** read entries with **`status: ready`** via `agm branch get <lane>/<slug> --file <abs>/BRANCHES.md` (or the MCP tool `agm_branch_get`), checking that all `depends_on` branches are `merged`/`deployed`.
- **Single-lane (`AGENTS-TUTORIAL.md`):** receive a handoff packet from `codex-mgr` / Architect (branch name, full `head` SHA, test commands + real counts, Audit + Review reports, and Hydrogen Medium docs confirmation).

**Immediately reject and return `needs-rebase` / `failed` if:**
1. The main checkout is dirty (`git status --short` has uncommitted files). Only exception: the user explicitly approved leaving named files as they are, the merge task file lists them, and `git merge-tree` shows the merge does not touch them; those files are never staged, restored or stashed.
2. The branch's `head` SHA does not match the approved SHA from Review/Docs (extra commits added after `ready`).
3. Audit/Review results or required doc updates are missing.
4. Any branch in `depends_on` has not been merged into `main`.
5. A non-CEO feature branch modifies `.agents/memory/published/` or `.ua/` without explicit Claude CEO approval.

---

## 3. Six-Step Integration Workflow

1. **Mark `merging`** with `agm branch set <lane>/<slug> status=merging --file <abs>/BRANCHES.md` (or `agm_branch_update`; `agm` takes the lock itself, never `mkdir` it by hand: it reclaims locks older than 60 s) so all BOT-CEOs see it is in progress.
2. **Verify exact SHA and merge sequentially** into `main` at the main checkout.
   - On merge conflicts, business logic conflicts, or >1 migration head: **stop immediately—never edit feature code to force a pass**; set `status: needs-rebase` in `BRANCHES.md` and notify the lane's BOT-CEO via `agm send <lane> --type LỖI --task <ID> "..."`.
3. **Re-run post-merge Audit/CI gates** on `main` (ESLint `--max-warnings 0`, typecheck, tests, build). If any check fails: log the failure, set `failed` (or `git revert` if already pushed further), and return to the manager/BOT-CEO.
4. **Understand-Anything (`.ua/`) batch refresh:** do **not** run full re-indexing on every tiny merge. When a merged branch adds/moves modules or alters public interfaces, notify Claude CEO to run incremental `/understand` on `main` in batch (end of wave/day) and commit `.ua/` (excluding `.ua/intermediate/` and `.ua/diff-overlay.json`).
5. **Update final status & clean up:**
   - Update `BRANCHES.md` to `merged` (or `deployed` if deployment is enabled and authorized).
   - Remove the worktree and merged branch (`cleanup-wt.sh`) only after verifying the worktree is clean and `agm ps` shows no live session inside that directory.
6. **Report completion** via `agm send` and move to the next `ready` entry in the queue.

---

## 4. Actions Requiring Explicit User Approval

`copilot-git` must **never** perform these actions without explicit user authorization:
- `git push` to remote, `git tag`, or cutting a `release`.
- Deploying to production/live environments (when `MATHY_DEPLOY` or equivalent flag is off).
- Rewriting or destroying history on `main` (`git reset --hard`, force-push). To roll back on `main`, use **`git revert`** and append a correction note.

## 5. Non-Interactive Runs (`copilot -p`)

- Permission rules match only the first subcommand: use `shell(git:*)` or `shell(git merge)`, and `shell(python:*)` for pytest. Add explicit deny rules (they win over every allow) for `push`, `reset`, `rebase`, `tag`, `worktree` and file writes unless the brief needs them.
- `shell(python:*)` permits any Python: confirm that the real test step ran instead of trusting a re-printed summary line.
- Tell the session that `LF will be replaced by CRLF` warnings are expected on Windows.
- Trial the merge with `git merge-tree --write-tree main <branch>` and give Copilot a short merge task file: branch, exact SHA, expected conflicts, files it may stage and files it must never stage.
- Cleanup runs (removing merged worktrees and branches) need their own brief and the user's approval. Permission rules cannot tell `git branch -d` from `-D`, or `worktree remove` from `worktree add`, so these limits live in the brief: `git worktree remove <path>` without `--force` and `git branch -d` only, after checking that each branch is listed by `git branch --merged main` and each worktree is clean. `-d` itself refuses an unmerged branch, which backs up the brief.
- Details and a working rebase pattern: [WINDOWS-AGENT-RUNTIME.md](WINDOWS-AGENT-RUNTIME.md#5-copilot-cli-copilot--p-as-copilot-git).
