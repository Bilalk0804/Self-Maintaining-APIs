---
name: ponytail
description: On-demand over-engineering review/audit using the "laziness ladder" (DietrichGebert/ponytail) — the always-on rung is already in USER_RULES.txt STATIC PRINCIPLES; this skill is for deep diff/repo audits and debt tracking. Use when the user asks to review a diff or repo for over-engineering, unnecessary abstractions, unused dependencies, or dead code, or invokes /ponytail_review.
compatibility: >
  Cursor has no native plugin/skill-command runtime (unlike Claude Code/Codex, where ponytail ships
  as a plugin with hooks + slash commands). In Cursor this skill + commands/ponytail_review.md
  replace those; the always-on ladder lives in USER_RULES.txt instead of a lifecycle hook.
metadata:
  author: janjaszczak
  version: "1.0"
  upstream: https://github.com/DietrichGebert/ponytail
---

# Ponytail — Laziness Ladder Audit

## Purpose
Catch over-engineering: unnecessary abstractions, unused dependencies pulled in for
one-liners, reinvented stdlib/native-platform features, dead code, and premature
generalization. This is the *review* half of ponytail; the *generation* half (the
ladder itself) is always active via `USER_RULES.txt` → STATIC PRINCIPLES, so every
diff should already be reasonably lean — this skill is for the cases that slip through
or for auditing code this agent did not just write.

## When to activate
- User asks to "review this diff for over-engineering", "does this need to exist",
  "simplify this", "audit the repo for bloat/dead code".
- Explicit command: `/ponytail_review` (diff) — see [`commands/ponytail_review.md`](../../commands/ponytail_review.md).
- Post-implementation self-check before marking a task done, when the diff touched
  more files/lines than the request implied.

## The ladder (source of truth — also in USER_RULES.txt)
Before writing code, stop at the first rung that holds:
1. Does this need to exist? → no: skip it (YAGNI).
2. Already in this codebase? → reuse it, don't rewrite.
3. Stdlib does it? → use it.
4. Native platform/language feature? → use it.
5. Installed dependency already covers it? → use it.
6. Fits in one line? → one line.
7. Only then: the minimum code that works.

**Never on the chopping block:** trust-boundary validation, data-loss handling,
security checks, accessibility. Lazy about the *solution*, never about *reading the
problem* — trace the real flow before picking a rung.

## Audit procedure
1. **Scope**
   - Diff mode: `git diff` (staged+unstaged) or the PR's changed files.
   - Repo mode: walk source directories, excluding generated/vendored paths
     (`node_modules/`, `dist/`, `build/`, `.venv/`, lockfiles).
2. **Flag candidates**
   - New dependency added for something stdlib/native/one-line already covers.
   - New class/module/interface with a single call site and no stated extension need.
   - Wrapper components/functions that just forward to the thing they wrap.
   - Config options, feature flags, or parameters with no caller passing non-default values.
   - Dead code: unused exports, unreachable branches, commented-out blocks.
   - Duplicated logic that could reuse an existing in-repo utility (check before
     proposing a new one — pairs with `repo-grounding`).
3. **Do not flag**
   - Validation, error handling, security, accessibility, or anything load-bearing for
     correctness even if it adds lines.
   - Abstractions with a real second caller or an explicit near-term requirement.
4. **Report** — a delete-list, not a rewrite:
   - Table: file:line, what it is, why it's excess, suggested replacement (often
     "delete" or "inline").
   - Do not apply deletions without confirmation unless invoked as part of `/cleanup`
     with explicit APPLY.

## Debt ledger (optional, lightweight)
If the user defers a `ponytail:`-style shortcut comment (e.g. a native feature used
today that will need a real component later at scale), note it in the PR description
or a TODO with an owner/trigger condition — don't let "later" become "never" silently.
Do not create a dedicated ledger file unless the user asks for one (matches DOC HYGIENE:
no orphan tracking files).

## Output contract
- Delete-list table (file:line, issue, suggested fix).
- One-line verdict: how many rungs of the ladder the diff/repo violates, if any.
- If invoked mid-implementation (self-check), apply the obvious deletions inline before
  calling the task done; if invoked as a standalone audit, propose only — apply after
  confirmation.
