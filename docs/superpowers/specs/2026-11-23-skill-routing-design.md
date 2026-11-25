# Phase and Role Skill Routing Design

Approved by the user on 2026-11-23 in the harness audit chat.

## Intent

Restore the phase/role skill matrix removed when Ponytail was added. Select workflows for Claude, Codex, agy and workers by the current task, while keeping Ponytail as a complementary behavior policy.

## Approach

A fixed two-skill cap preserves small prompts but can suppress debugging, domain knowledge and final verification. The approved approach uses one workflow plus one relevant domain skill by default, with explicit additions for phase requirements. Skill discovery stays available; instructions are read on demand.

## Scope

Document the approved matrix, update role guides and template examples, retain the comma-separated `skills:` interface, and remove the dispatch example's hard two-skill rejection. Add document-contract and executable example regressions. Verification remains required even for mechanical tasks with no workflow skill.

## Constraints

No global skill/plugin installation or machine-template edits. No model, quota, permission, merge-gate or AGM-version changes. Never treat a skill available in one CLI as installed in all CLIs. Keep optional delegation under the existing role and task authorization rules.

## Acceptance

The matrix covers discovery, system design/ADR, planning/testing strategy, feature work, debugging, review request/execution/feedback, Audit, documentation, verification, skill authoring and mechanical work. Role guides link to one canonical matrix. The real dispatch example accepts an explicitly listed third skill. Missing or weakened routing contracts fail lint.
