# Context Rotation for CEO and Codex High

Requested by the user on 2026-11-23. This is the role-specific handoff policy for [Claude CEO](CLAUDE-ARCHITECT-CEO.md) and [Codex High](CODEX-ENGINEERING.md), used alongside [Skill Routing](SKILL-ROUTING.md).

## 1. Targets

Percentages mean **context already used**, not quota consumed or context remaining.

| Session | Normal rotation target | Earlier target for context-heavy work |
|---|---|---|
| Claude CEO / Architect | Request rotation at 50%; use the 50–55% band only to reach a safe handoff point | Normally 40%, with a 35–45% planning band; lower if the projected next step would exhaust the handoff reserve |
| Codex High / high-reasoning manager, developer or reviewer | Request rotation at 50%; do not start another large phase at or above 55% | Normally 40%, with a 35–45% planning band; lower when justified by the next step |
| Other OpenCode workers | Existing shared `ctx_rotate` setting, currently 70% in the example | Existing worker policy unless the brief explicitly assigns an earlier threshold |

Codex High describes the reasoning/work profile, not a hard-coded model slug. A task brief or session startup identifies this profile. Do not infer context usage from the model's name, reasoning token count or subscription quota.

For CEO/Codex High, **50% is the default request point, not 55%**. The extra five percentage points are a handoff reserve, not permission to start more work. If already beyond the band, hand off at the next safe point rather than waiting for another threshold.

## 2. Choose an Earlier Point Proactively

Reassess at task/phase boundaries, after a substantial tool result, and before a large read, diff, review bundle or debugging cycle. Use the context reading already exposed by the client/statusline or AGM's session telemetry; do not add busy polling or claim an exact percentage when telemetry is unavailable.

Select the earlier target before long work, for example a multi-module investigation, a large migration/design review, repeated debug cycles or an upcoming large log/diff. Record the target and reason in the task log or handoff. For ordinary long work, choose 40%; the 35–45% band allows a nearby safe boundary. A still smaller value is appropriate when a concrete next step needs it, rather than because the agent merely prefers a fresh chat.

A practical decision rule is: if **current usage + estimated next step + 5 percentage points of handoff reserve reaches 55%**, rotate before that next step. This is a planning estimate, not a new AGM configuration field. Unknown next-step size on an already context-heavy task favors the earlier target.

When context telemetry is unavailable, report that limitation and hand off at a suitable phase boundary before the predicted long step. Do not invent a measured percentage. Avoid rotating repeatedly without new useful work; a fresh context does not justify creating a duplicate task or reviewer.

## 3. Use the Existing AGM Handoff Path

AGM 0.4.0 has a **shared** `[policy].ctx_rotate` threshold. `agm manage` has no per-role threshold option. Therefore, changing the global value to 50–55% would also change ordinary workers. This harness leaves that shared fallback alone and implements the CEO/Codex High rule through the session's own proactive rotation request.

For interactive Claude/Codex sessions, the user first enables management once for the session, preserving the correct launch command when necessary:

```powershell
agm manage ceo-platform
agm manage codex-mgr
```

At the selected threshold, **the managed session itself calls the supported CLI command**, without requesting a new percentage approval each time:

```powershell
agm rotate ceo-platform
# A Codex session instead targets its own terminal:
agm rotate codex-mgr
```

Use the actual own terminal name/address, not another lane's session. The daemon queues the handoff and opens the successor through its existing managed rotation state machine. This is an agent-initiated request, not a newly installed per-role daemon threshold or background watcher. MCP currently exposes no rotate tool; use CLI when authorized.

If the session is not managed, report that prerequisite and save the handoff; do not claim a successor was opened. If policy is off, respect the emergency stop, save state and report instead of enabling it. Do not kill/interrupt Claude or Codex to force a rotation. If the successor cannot be opened or linked, preserve state and report the failure.

Codex High hosted inside OpenCode may request `agm rotate` on its own OpenCode terminal at the same earlier target, but follows the existing OpenCode handoff/commit/successor protocol. This does not authorize killing an interactive Codex/Claude session or changing all workers' thresholds.

## 4. Handoff Contract

Finish the current tool operation and preserve edits before handing off. Do not interrupt a running write/merge or manufacture a passing test result to create a clean checkpoint. Keep unfinished work in the same worktree, record dirty state and the diff location, and commit only an appropriate completed checkpoint.

The durable handoff is concise (normally at most 300 words plus references) and includes:

- Goal, task ID, role, phase, selected workflow/domain skills and resolved references.
- Decisions, accepted constraints, permissions and budget already granted; do not ask for the same approval again merely because the session changed.
- Worktree/branch and HEAD, changed files, any uncommitted work, verification actually run and unresolved findings.
- Active worker/reviewer task IDs and terminal references, dependencies and the one next action; never redispatch still-running work.
- `rotation_target_pct`, observed usage if known, reason for an earlier target and the role policy to preserve in the successor. These are handoff metadata, not keys to add to `providers.toml`.

For managed Claude/Codex rotation, write the daemon-requested `.agents/handoff/<id>-<ts>.md`, include its exact `rotation_id` and finish with `<!-- agm:handoff-done -->`. Do not invent an ID before the daemon issues it. Follow the link/acknowledgement protocol; after successful handoff, the old session takes no new task. Only the user cleans up old protected sessions with the existing `agm cleanup` flow.

## 5. Configuration Boundary

The shared fallback can remain:

```toml
[policy]
ctx_warn = 50
ctx_rotate = 70
```

This snippet is the ordinary worker/global fallback, **not** the CEO/Codex High target. Do not add guessed keys such as `ceo_ctx_rotate` or unsupported `agm manage --ctx-rotate` flags. A future daemon-enforced per-role threshold requires an AGM implementation change and tests; the current policy uses the verified `agm rotate` path.

Validation for this update covers document consistency, links and the existing harness test suite. It does not claim a live rotation was performed or modify the machine-wide configuration.
