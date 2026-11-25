# NVIDIA NIM — Read-Only Background Work, Off the Critical Path


Policy A in [AGENTS-TUTORIAL.md](../AGENTS-TUTORIAL.md) uses slower NVIDIA models for nonurgent work; DeepSeek Go handles interactive work per the [routing table](OPENCODE-WORKERS-REVIEWER.md). No additional review stage or pre-merge gate.

## 1. Models and Trial Roles

These three IDs are present in the workspace catalog. API quality, latency, actual context limits, tool calling, pricing and quota have not been benchmarked. Assignments below are **trial hypotheses**, not capability rankings.

<!-- harness:background:start -->
| Role | Model | Expected output |
|---|---|---|
| `background-kimi` | `nvidia-kimi/moonshotai/kimi-k3.1-pro` | Synthesis of selected code/documents for a future task |
| `background-mistral` | `nvidia-mistral/mistralai/mistral-large-4-1t-a52b` | Proposed terminology, wording and documentation consistency fixes |
| `background-nemotron` | `nvidia-nemotron/nvidia/nemotron-3-ultra-550b-a55b` | Log synthesis, recurring failures and harness improvement proposals for Gemini Argon |
<!-- harness:background:end -->

Do not assign code, default review or Audit; do not let these agents edit docs/harness files. Existing Docs and Gemini Argon roles still decide and write official changes.

## 2. Safety Contract

Lint checks this table across languages. **This is not OpenCode configuration or keys automatically interpreted by `agm`**; the CEO enforces new limits manually until a verified wrapper exists.

<!-- harness:safety:start -->
| Rule | Value | Meaning |
|---|---|---|
| `execution` | `background-only` | Keep this work off the critical path |
| `writes` | `none` | The agent does not create/edit any files, including another session's records |
| `merge-gate` | `false` | Do not wait for an auxiliary report to merge |
| `max-concurrent` | `1` | Initially at most 1 task across all NVIDIA aliases |
| `pilot-deadline-minutes` | `15` | Proposed pilot budget; each brief supplies its actual deadline |
| `enforcement` | `manual` | CEO checks, not an installed scheduler |
| `on-429` | `stop-and-check` | Pause affected dispatch and inspect quota/reset |
| `stale-output` | `revalidate` | Recheck SHA/snapshot before use |
| `outer-retry` | `none` | No retry loop outside OpenCode retries |
| `cloud-approval` | `required` | Verify data permissions, source and cost before sending |
<!-- harness:safety:end -->

Slow does not mean weak. Keep worker `tier: nhanh | vừa | mạnh`; record background/interactive requirements and deadlines separately. Do not invent scheduler keys in `providers.toml`.

## 3. Before Dispatch

1. The CEO verifies cloud data permission, endpoint entitlement, pricing, quota and allocation. Empty pricing metadata does not prove free usage; catalog presence does not prove endpoint health.
2. Do not send keys, customer data or sensitive code without user approval. Supply only selected minimal inputs; do not grant unrestricted shell or credential-store access.
3. Apply OpenCode v2 [permissions](https://opencode.ai/docs/permissions): deny edits, allow only required read paths; if a shell is necessary, allowlist specific read commands rather than granting a general shell. A “do not edit” prompt is not a permission boundary.
4. Qualify the exact role using section 6. Unknown cost/quota/data permission means `blocked`, not mass smoke tests across all three models.
5. The CEO keeps at most one active NVIDIA task across the alias group. `[policy].max_workers` only caps workers, not this background limit. Different aliases do not prove independent quotas.

## 4. Brief and Handoff

Record task ID, full model ID, goal/output, allowed read paths, allocation, a timezone-qualified deadline, input SHA and capture time. Without Git, supply a `snapshot`: file list + content SHA-256 hashes + capture time. Do not hash/include secrets in records.

The agent only returns results to the coordinator:

```text
task: <id>
model: <provider/model>
input-version: <SHA or snapshot>
captured-at: <timestamp with timezone>
finished-at: <timestamp with timezone>
findings: <conclusions with file:line or log locations>
uncertain: <unverified points>
proposals: <proposals only; do not apply>
```

The appropriate role saves useful results in `.agents/decisions/` or existing records, then sends a short `agm send` pointer. Background agents must not compete to write `.agents/log/`, docs, code or `BRANCHES.md`.

Expired or mismatched SHA/snapshot output is reference material only: revalidate it before use, never auto-apply patches or count it as a valid review for a newer SHA. Escalate evidence of serious risk immediately through existing safety procedures; optional work does not make substantiated risks optional.

## 5. Deadlines, Timeouts and Errors

- Proposed pilot budget: 15 minutes from dispatch; the brief specifies the actual deadline. On expiry, the coordinator records it and stops waiting on the critical path. Do not duplicate the task or release/reuse its slot until the old run is confirmed stopped/finished. Never autonomously kill/interrupt Codex/Claude.
- Per [OpenCode providers](https://opencode.ai/docs/providers), `settings.timeout` limits **one HTTP request**, provider-wide; `settings.headerTimeout`/`settings.chunkTimeout` are transport timeouts, not whole-task deadlines. This change does not modify live settings.
- OpenCode may retry timed-out requests up to 3 times. Do not add wrapper retry loops or resend prompts while the previous request's state is unknown; track status and total elapsed time including retries.
- On `429`/quota exhaustion, pause affected dispatch and respect `Retry-After` or reset time. If scope is unknown, pause the NVIDIA group pending CEO inspection; do not rotate aliases to bypass the block. Do not invent a fixed cooldown or claim an automatic circuit breaker is installed.
- Authentication/missing-model errors: mark `blocked` for configuration checks, not credential edits or attempts against another paid source.

## 6. Qualification and Measurements

- Before real work, run **one small read-only trial per model** on approved data. Run sequentially, not fan-out or three duplicate reports.
- Kimi: synthesize correct relationships/locations in selected code; Mistral: identify known documentation inconsistencies; Nemotron: classify known log failures. Prepare expected answers and acceptance criteria first.
- Pass only when permissions are respected, evidence is accurate, no facts/test runs are invented, planted defects are found and the brief deadline is met. Failure leaves that role unqualified.
- Record elapsed time, timeouts/retries/errors, actual model, measurable tokens, comparison with expected answers and input SHA/snapshot. Tokens are not invoices; one trial does not establish p95 latency.
- After several tasks, adjust roles/limits using measurements and budget. Do not autonomously promote NVIDIA to worker/reviewer under policy A.
- First measured result (2026-11-24): `background-nemotron` passed log synthesis (5 of 5 planted findings, 161 s, no writes) but reported fixed issues as open; briefs now ask for `status: open | resolved | unknown` per finding. Use a default-deny agent configuration ([details](WINDOWS-AGENT-RUNTIME.md#8-nvidia-background-qualification)). Second batch (2026-11-25): `background-mistral` gave usable summaries of long inlined test/review logs, while Nemotron and Kimi timed out on the same long prompts; this is practical evidence, not a formal section-6 qualification ([details](WINDOWS-AGENT-RUNTIME.md#9-second-batch-2026-11-25-twelve-task-branches-agy-first-reviews)).

## 7. Verification Limits

This change confirms catalog IDs and adds policy/lint; **no API benchmarks** or **Opus review** have completed because quota/credit was unavailable. No scheduler, agent or machine-wide configuration was installed. Check documents using [HARNESS-LINT.md](HARNESS-LINT.md).
