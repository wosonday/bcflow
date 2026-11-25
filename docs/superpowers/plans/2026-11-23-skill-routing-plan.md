# Phase and Role Skill Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore workflow and domain skill selection without letting Ponytail suppress it.

**Architecture:** One canonical operational guide and a checked phase-to-workflow contract, referenced by root policies and role guides. Existing comma-separated task skills remain compatible.

**Tech Stack:** Markdown, dependency-free Python unittest, isolated Bash examples.

**Spec:** [Approved design](../specs/2026-11-23-skill-routing-design.md).

## Global Constraints

- Work in a staged copy because the source collection is not a Git repository; publish only changed files after checking source hashes.
- No global skill/plugin installation or machine-template edits.
- No model, quota, permission, merge-gate or AGM-version changes.
- User approval already covers this implementation; proceed directly without another approval checkpoint.

## Review Focus

- A skill selected for one phase must not silently suppress final verification.
- Domain skills are selected by the task, not loaded for every role.
- `requesting-code-review`, `code-review` and `receiving-code-review` have distinct owners.
- Extra required skills may exceed two; zero workflow skills remains valid for mechanical work.
- CLI-specific installation and namespaces are not inferred from files in a cache.

## Task 1: Regression gates and canonical matrix

- [x] Add missing/altered contract negative tests and real dispatch-example cases. The contract negatives failed before implementation; the four-skill phase exception exposed the old cap (the three-skill case already passed due to the old line-count behavior).
- [x] Add the canonical matrix, workflow contract and task brief examples; remove the dispatch hard cap.
- [x] Run focused tests and lint.

## Task 2: Role integration and handoff

- [x] Update root policy, BOT-CEO, HERDR template examples and Claude/Codex/agy/OpenCode/Copilot guides to reference the matrix.
- [x] Run full tests, lint and independent review; address material findings. Full suite: 97 tests passed with Git Bash allowed outside the Windows sandbox; lint: 20 documents, zero errors; independent review: no material findings.
- [x] Publish only the verified changed files after detecting concurrent source edits; verify the actual destination. Sixteen files were published with backups and byte/hash checks. Destination verification: 97 tests passed; lint checked 20 documents with zero errors.
