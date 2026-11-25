# Model-Specific Usage Guides


This directory specifies startup commands, permission boundaries, `agm`/`hc` usage, and quota-saving practices for each model/CLI in [AGENTS-TUTORIAL.md](../AGENTS-TUTORIAL.md), [BOT-CEO.md](../BOT-CEO.md), and [HERDR-CORE-BOT.md](../HERDR-CORE-BOT.md).

## Index

bcflow release notes: [CHANGELOG.md](../CHANGELOG.md).

| Document | Model / Tool | Primary Role |
|---|---|---|
| [SKILL-ROUTING.md](SKILL-ROUTING.md) | Claude, Codex, agy, OpenCode workers and Copilot | Canonical workflow/domain selection by phase, Ponytail composition, availability checks and Task Brief examples |
| [CONTEXT-ROTATION.md](CONTEXT-ROTATION.md) | Claude CEO and Codex High | Proactive rotation at 50%, safe handoff through 55%, earlier targets for context-heavy tasks and the AGM shared-threshold boundary |
| [CLAUDE-ARCHITECT-CEO.md](CLAUDE-ARCHITECT-CEO.md) | Claude Code (`claude-opus-5-5`, `claude-fable-5-2`) | Architect / BOT-CEO, lane budget allocation, high-risk escalations |
| [CODEX-ENGINEERING.md](CODEX-ENGINEERING.md) | Codex CLI / `openai/gpt-6.1-sol` | Hard engineering, fixes after 2 failed attempts, complex cross-reviews, single-lane management |
| [AGY-GEMINI-OPUS.md](AGY-GEMINI-OPUS.md) | Antigravity CLI `agy` (3.8 Flash, Hydrogen Low/Medium/High, Neon High, Argon, Opus High) | Fast research (`3.8-flash-low`), lightweight docs/reports (`3.8-flash-medium` / `hydrogen-low`), default reviewer (`hydrogen-high`), hard-case/high-risk reviewer (`neon-high`), technical advisor & harness optimization (`argon`), Co-Architect (`Opus High`) |
| [OPENCODE-WORKERS-REVIEWER.md](OPENCODE-WORKERS-REVIEWER.md) | DeepSeek Flash/Pro on OpenCode Go; scripts/CI and approved fallbacks | Flash for fast/mid/explore; Pro for qualified strong work and fallback review |
| [NVIDIA-BACKGROUND.md](NVIDIA-BACKGROUND.md) | NVIDIA Kimi, Mistral, Nemotron | Read-only background synthesis/proposals/logs; not a merge gate, not benchmarked |
| [HARNESS-LINT.md](HARNESS-LINT.md) | Python 3.11+, standard library only | Links/anchors, Markdown structure, model IDs, bilingual routing and safety contracts |
| [AGM-0.4.0-GUIDE.md](AGM-0.4.0-GUIDE.md) | `agm` 0.4.0 (`agm branch`, `agm mcp`) | Upgrade & usage guide (Vietnamese): locked `BRANCHES.md` writer (`agm branch get/set`) and 7-tool stdio MCP server (`agm mcp`) |
| [COPILOT-GIT.md](COPILOT-GIT.md) | GitHub Copilot CLI (`copilot`) | Git version manager: reads `BRANCHES.md`, merges exact SHA, post-merge verification, reverts, cleans up worktrees/branches |
| [ESLINT-CI-GUARDRAILS.md](ESLINT-CI-GUARDRAILS.md) | ESLint (`--max-warnings 0`) + Git Diff Check + GitHub Actions CI | **Mandatory guardrails for all models:** blocks `any`, `@ts-ignore`, `eslint-disable`, bloated functions, missing `await`; enforced at Audit/CI so lightweight models can be used safely |
| [WINDOWS-AGENT-RUNTIME.md](WINDOWS-AGENT-RUNTIME.md) | `hc`, `opencode run`, `agy -p`, `copilot -p` on Windows | Verified runtime lessons: argv spawning, non-interactive permissions, default-deny read-only agents, headless reviews, Copilot permission rules, parallel-branch merges, worker-completion tracking, merge trials, quota-exhaustion fallback |

## Shared Context, Memory & Ponytail (v4.0)

- **Code Graph:** [Understand-Anything](https://github.com/Egonex-AI/Understand-Anything) (`.ua/knowledge-graph.json`) replaces GitNexus. Run `/understand` once per main repository, share `.ua/` across Claude, Codex, Antigravity (`agy`), and Workers (Workers receive scoped file lists or read-only MCP queries, never raw full JSON dumps), and refresh incrementally in batches after structural merges.
- **Shared Memory & Handoff:** Never forward raw chat history between agents—pass standardized short reports (`.agents/tasks/`, `log/`, `reviews/`, `reports/`). Persistent cross-task memory lives in `.agents/memory/published/` (`decisions.md`, `invariants.md`, `gotchas.md`, plus `HANDOFF.md`) with strict ownership: **Claude CEO** approves/publishes/updates; **Codex** proposes & writes task findings; **Agy** proposes, reviews & flags conflicts; **Workers** write task logs & implementation findings.
- **Ponytail ([DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail)):** design/planning uses `off` / `lite`; Codex/agy implementation and review use `full`; workers use compact rules; **no `ultra` by default**. Keep it separate from [workflow/domain skill selection](SKILL-ROUTING.md): normally one workflow + one domain, justified phase additions allowed, final verification retained.

## Quick Quota Check Before Dispatch

Claude CEO follows the [shared budget policy](../AGENTS-TUTORIAL.md#311-điều-phối-kinh-phí-ai). `agm 0.4.0` reads Claude, Codex, `agy`, and OpenCode Go (`5h`/`7d`/`mo`) via `agm quota --refresh`; verify Extra Usage and billing rules in the [Go Console](https://opencode.ai/console/go):

```powershell
agm quota --refresh           # % LEFT of plan windows (Claude, Codex, agy, OpenCode Go)
agm cost --by source          # token share across claude / codex / opencode / agy
agm cost --by model           # per model (OpenCode as provider/id) with USD at list API prices and a bold SUM row
```

| Situation | Load-balancing action |
|---|---|
| Go near its limit, exhausted or unverified | Near its limit: reserve capacity for review/rework and priorities. Exhausted/unverified: use a capable approved source or mark blocked; never enable Extra Usage autonomously |
| `codex` 7d under 20–30% left | Reserve `openai/gpt-6.1-sol` for genuinely hard engineering; routine review uses `agy` `gemini-4-hydrogen-high` first, OpenCode Go as fallback |
| `claude` 5h/7d rising fast | Open an interactive `agy` Opus High session inside the repo (`--mode plan --sandbox`, keeping `conversation_id` for prompt cache) to offload design/task splitting; hard-case review goes to `agy` `gemini-4-neon-high`, and Claude only after Neon fails; never open Claude for routine reviews |
| `:free` endpoint hits `429` | Switch to a fallback `:free` slug or local Ollama (`num_ctx ≥ 64k`) |
| NVIDIA `429`, timeout or late output | Do not rotate aliases to bypass quota or layer retries; the CEO checks the previous task/deadline and SHA/snapshot before use |

Confirmed IDs: `opencode-go/deepseek-v4.1-flash`, `opencode-go/deepseek-v4-pro`. Go still requires role qualification; DeepSeek is not free and Personal/API `opencode/deepseek-*` is not a silent substitute. NVIDIA does not replace Review/Audit/Docs/Argon.

## Verify Documents Before Handoff

```powershell
python -B scripts/lint_harness.py
python -B -m unittest discover -s tests -v
```

Run from the `agent-tutorial` root, without Git, networking or API keys. See [lint coverage and limitations](HARNESS-LINT.md).
