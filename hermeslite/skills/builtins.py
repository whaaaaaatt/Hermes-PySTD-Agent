"""Built-in skill packs.

A collection of high-value :class:`SKILL.md` files bundled with the
install. On first run we copy them into ``$HERMESLITE_HOME/skills/``
so they're discoverable by the agent. Users can edit / delete them
just like user-added skills.

Why bundle? Without a default skill catalogue, a fresh install
shows zero skills in the system prompt, which makes the agent look
less capable than it is. The skills here cover the most common
prompts we see:

  - ``git-helper``              — clean, conventional commits and PRs
  - ``code-review``             — structured review checklist
  - ``doc-writer``              — produce / update Markdown documentation
  - ``shell-cookbook``          — common shell one-liners
  - ``sql-queries``             — read-only SQL patterns
  - ``systematic-debugging``    — 4-phase root cause debugging
  - ``plan``                    — write actionable markdown plans
  - ``test-driven-development`` — TDD: enforce RED-GREEN-REFACTOR
  - ``requesting-code-review``  — pre-commit verification pipeline
  - ``github-pr-workflow``      — PR lifecycle: branch, commit, open, merge
  - ``github-code-review``      — review PRs: diffs, inline comments
  - ``spike``                   — throwaway experiments to validate ideas
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict

from ..paths import get_skills_dir, get_hermes_home

logger = logging.getLogger(__name__)


# Each skill is a {name: (frontmatter, body)} pair. Bodies are written
# verbatim into ``SKILL.md``.

SKILLS: Dict[str, str] = {

"git-helper": """---
name: git-helper
description: "Conventional commits, branch hygiene, and PR-ready summaries."
version: 1.0.0
---

# Git Helper

When the user asks for help with git (commits, branches, PRs, conflict
resolution), follow this playbook.

## Commit messages

Use Conventional Commits format: ``type(scope): summary``

Allowed types: ``feat``, ``fix``, ``docs``, ``style``, ``refactor``,
``perf``, ``test``, ``chore``, ``build``, ``ci``.

- Subject line ≤ 72 chars
- Use imperative mood ("add", not "added")
- Body wraps at 72 chars, explains *why*
- Reference issues: ``Refs #1234`` or ``Closes #1234``

## Branch hygiene

- One concern per branch
- Prefix: ``feat/``, ``fix/``, ``chore/``, ``docs/``, ``refactor/``
- Keep the working tree clean before opening a PR

## PR description template

```
## What
- 1-3 bullets describing the change

## Why
- The user-visible problem / motivation

## How
- Implementation notes; tradeoffs

## Test plan
- [ ] unit tests
- [ ] manual verification
- [ ] screenshots (if UI)

Refs #issue
```

## Common tasks

- "stage and commit"  → ``git add -A && git commit``
- "undo last commit, keep changes" → ``git reset --soft HEAD~1``
- "rewrite the last commit message" → ``git commit --amend``
- "list stale branches" → ``git branch --merged main | grep -v main``
""",

"code-review": """---
name: code-review
description: "Structured code review with a consistent checklist and tone."
version: 1.0.0
---

# Code Review

When asked to review code, produce a structured report with these
sections. Be specific; reference line numbers / function names when
possible.

## Checklist

1. **Correctness** — does the code do what the PR claims?
2. **Edge cases** — empty inputs, None, zero, max-int, unicode
3. **Error handling** — exceptions typed correctly, messages useful
4. **Tests** — is the new code covered? Are assertions specific?
5. **Naming** — do names describe what the code actually does?
6. **Complexity** — could this be split into smaller functions?
7. **API surface** — backwards compatible? Documented?
8. **Security** — input validation, secrets, injection vectors
9. **Performance** — obvious O(n²) loops, N+1 queries, blocking I/O
10. **Accessibility** — alt text, keyboard nav, contrast (if UI)

## Severity tags

- **BLOCKER** — must fix before merge
- **MAJOR** — should fix; push back hard
- **MINOR** — nice to have; suggest with reasoning
- **NIT** — preference; defer to author
- **PRAISE** — call out good patterns; reinforces good habits

## Tone

- Critique the code, not the author
- Suggest, don't command ("consider…" vs "you must…")
- Distinguish "I think" (opinion) from "this will" (fact)
""",

"doc-writer": """---
name: doc-writer
description: "Write or improve Markdown documentation (READMEs, ADRs, API docs)."
version: 1.0.0
---

# Doc Writer

When asked to write or improve documentation, follow these conventions.

## README structure

```
# Project Name

One-sentence tagline.

## What
Why does this project exist? What problem does it solve?

## Quickstart
The shortest path to "hello world". Code first, prose second.

## Usage
The most common commands / API calls. One section per use case.

## Configuration
Every config knob, its default, and what it controls.

## Project layout
Directory tree with one-line annotations.

## License
```

## ADR (Architecture Decision Record) template

```
# N. Title

## Status
Proposed | Accepted | Deprecated | Superseded by N+1

## Context
What is the issue we're seeing?

## Decision
What did we choose to do?

## Consequences
What becomes easier? What becomes harder?
```

## API doc conventions

- One section per public symbol
- Show the signature first
- One example block; use realistic data, not ``foo`` / ``bar``
- Document error / return conditions explicitly
""",

"shell-cookbook": """---
name: shell-cookbook
description: "Common shell patterns — file ops, text processing, network, JSON."
version: 1.0.0
---

# Shell Cookbook

## File operations

- Find files modified in the last day: ``find . -mtime -1 -type f``
- Find files larger than 100MB: ``find . -type f -size +100M``
- Show the disk usage of a directory tree: ``du -sh */ | sort -h``
- Symlink a directory: ``ln -s /source /target``

## Text processing

- Count lines in every .py file: ``find . -name '*.py' -exec wc -l {} +``
- Top 10 longest lines: ``awk '{ print length, $0 }' file | sort -rn | head``
- Replace in-place across files: ``find . -name '*.md' -exec sed -i 's/old/new/g' {} +``
- Strip trailing whitespace: ``sed -i 's/[[:space:]]*$//' file``

## JSON (no jq)

- Pretty-print: ``python -m json.tool < file.json``
- Extract a key: ``python -c "import json,sys; print(json.load(sys.stdin)['key'])" < file.json``
- Convert CSV→JSON: ``python -c "import csv,json; print(json.dumps(list(csv.DictReader(open('x.csv')))))"``

## Network

- Check a port: ``curl -sS -o /dev/null -w '%{http_code}' http://host:port/``
- Trace redirects: ``curl -sS -L -o /dev/null -w '%{url_effective}\\n' http://x``
- Public IP: ``curl -s https://api.ipify.org``

## Misc

- Watch a command: ``watch -n 1 'command'``
- Show process tree: ``ps -ef --forest`` (Linux) / ``Get-Process | Format-Table`` (Windows)
- Quick HTTP server: ``python -m http.server 8000``
""",

"sql-queries": """---
name: sql-queries
description: "Read-only SQL patterns — joins, window functions, debugging slow queries."
version: 1.0.0
---

# SQL Queries

Read-only patterns. Always prefer parameterized queries; never
interpolate user input into SQL.

## Top-N within group

```sql
SELECT *
FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
    FROM events
) t
WHERE rn <= 10;
```

## Cumulative sum

```sql
SELECT date, amount,
       SUM(amount) OVER (ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
FROM ledger;
```

## Find duplicate rows

```sql
SELECT col, COUNT(*) AS n
FROM t
GROUP BY col
HAVING n > 1;
```

## Inspect query plan

- SQLite: ``EXPLAIN QUERY PLAN <your query>``
- PostgreSQL: ``EXPLAIN ANALYZE <your query>``

## Useful pragmas (SQLite)

- ``PRAGMA journal_mode = WAL;``  — concurrent readers
- ``PRAGMA foreign_keys = ON;``   — enforce FK constraints
- ``PRAGMA query_only = ON;``     — refuse writes (read-only sessions)
""",

"systematic-debugging": """---
name: systematic-debugging
description: "4-phase root cause debugging: understand bugs before fixing."
version: 1.1.0
---

# Systematic Debugging

## Overview

Random fixes waste time and create new bugs. Quick patches mask underlying issues.

**Core principle:** ALWAYS find root cause before attempting fixes. Symptom fixes are failure.

## The Iron Law

```
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST
```

If you haven't completed Phase 1, you cannot propose fixes.

## When to Use

Use for ANY technical issue: test failures, bugs, unexpected behavior, performance problems, build failures, integration issues.

**Use this ESPECIALLY when:**
- Under time pressure (emergencies make guessing tempting)
- "Just one quick fix" seems obvious
- You've already tried multiple fixes
- You don't fully understand the issue

## The Four Phases

### Phase 1: Root Cause Investigation

**BEFORE attempting ANY fix:**

1. **Read error messages carefully** — don't skip past errors. Read stack traces completely. Note line numbers, file paths, error codes.
2. **Reproduce consistently** — can you trigger it reliably? What are the exact steps? If not reproducible, gather more data.
3. **Check recent changes** — ``git log --oneline -10``, ``git diff``, recent commits, new dependencies.
4. **Gather evidence** — for multi-component systems, add diagnostic instrumentation at each boundary.
5. **Trace data flow** — where does the bad value originate? Keep tracing upstream until you find the source.

**Phase 1 completion checklist:**
- [ ] Error messages fully read and understood
- [ ] Issue reproduced consistently
- [ ] Recent changes identified
- [ ] Evidence gathered (logs, state, data flow)
- [ ] Root cause hypothesis formed

### Phase 2: Pattern Analysis

1. **Find working examples** — locate similar working code in the same codebase.
2. **Compare against references** — read the reference implementation completely.
3. **Identify differences** — what's different between working and broken? List every difference.
4. **Understand dependencies** — what components, settings, config does this need?

### Phase 3: Hypothesis and Testing

1. **Form a single hypothesis** — "I think X is the root cause because Y"
2. **Test minimally** — make the SMALLEST possible change. One variable at a time.
3. **Verify** — did it work? Didn't work? Form NEW hypothesis. Don't add more fixes on top.

### Phase 4: Implementation

1. **Create failing test case** — simplest reproduction. MUST have before fixing.
2. **Implement single fix** — address the root cause. ONE change at a time.
3. **Verify fix** — run the specific test, then full suite.
4. **Rule of Three** — if 3+ fixes failed, STOP. Question the architecture. Discuss with user.

## Red Flags — STOP and Return to Phase 1

- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "I don't fully understand but this might work"
- "One more fix attempt" (when already tried 2+)

## Quick Reference

| Phase | Key Activities |
|-------|---------------|
| **1. Root Cause** | Read errors, reproduce, check changes, trace data flow |
| **2. Pattern** | Find working examples, compare, identify differences |
| **3. Hypothesis** | Form theory, test minimally, one variable at a time |
| **4. Implementation** | Create regression test, fix root cause, verify |
""",

"plan": """---
name: plan
description: "Plan mode: write an actionable markdown plan, no execution."
version: 2.0.0
---

# Plan Mode

Use this skill when the user wants a plan instead of execution.

## Core behavior

For this turn, you are planning only.

- Do not implement code.
- Do not edit project files except the plan markdown file.
- Do not run mutating terminal commands, commit, push, or perform external actions.
- You may inspect the repo with read-only commands when needed.
- Your deliverable is a markdown plan saved under ``.hermes-lite/plans/``.

## Output requirements

Write a markdown plan that is concrete and actionable. Include when relevant:

- Goal
- Current context / assumptions
- Proposed approach
- Step-by-step plan with bite-sized tasks (2-5 min each)
- Files likely to change (exact paths)
- Tests / validation
- Risks, tradeoffs, and open questions

## Task Structure

Each task follows this format:

```
### Task N: Descriptive Name

**Objective:** One sentence

**Files:**
- Create: ``path/to/new_file.py``
- Modify: ``path/to/existing.py``

**Steps:**
1. Write failing test
2. Run test to verify failure
3. Write minimal implementation
4. Run test to verify pass
5. Commit
```

## Principles

- **DRY** — Don't Repeat Yourself
- **YAGNI** — You Aren't Gonna Need It (implement only what's needed now)
- **TDD** — Every task that produces code should include the full Red-Green-Refactor cycle
- **Frequent commits** — commit after every task

## Save location

Save with ``write_file`` under: ``.hermes-lite/plans/YYYY-MM-DD_HHMMSS-<slug>.md``

## Interaction style

- If the request is clear, write the plan directly.
- If underspecified, ask a brief clarifying question.
- After saving, reply briefly with what you planned and the saved path.
""",

"test-driven-development": """---
name: test-driven-development
description: "TDD: enforce RED-GREEN-REFACTOR, tests before code."
version: 1.1.0
---

# Test-Driven Development (TDD)

## Overview

Write the test first. Watch it fail. Write minimal code to pass.

**Core principle:** If you didn't watch the test fail, you don't know if it tests the right thing.

## The Iron Law

```
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST
```

Write code before the test? Delete it. Start over.

## Red-Green-Refactor Cycle

### RED — Write Failing Test

Write one minimal test showing what should happen.

```python
def test_add_positive_numbers():
    assert add(2, 3) == 5
```

**Requirements:**
- One behavior per test
- Clear descriptive name
- Real code, not mocks (unless unavoidable)

### Verify RED — Watch It Fail

```bash
pytest tests/test_feature.py::test_specific_behavior -v
```

- Test passes immediately? You're testing existing behavior. Fix the test.
- Test errors? Fix the error, re-run until it fails correctly.

### GREEN — Minimal Code

Write the simplest code to pass the test. Nothing more.

```python
def add(a, b):
    return a + b
```

**Cheating is OK in GREEN:** hardcode return values, copy-paste, skip edge cases. Fix in REFACTOR.

### Verify GREEN — Watch It Pass

```bash
pytest tests/test_feature.py::test_specific_behavior -v
pytest tests/ -q  # full suite — no regressions
```

### REFACTOR — Clean Up

After green only: remove duplication, improve names, extract helpers.

**If tests fail during refactor:** Undo immediately. Take smaller steps.

### Repeat

Next failing test for next behavior. One cycle at a time.

## Common Rationalizations

| Excuse | Reality |
|--------|---------|
| "Too simple to test" | Simple code breaks. Test takes 30 seconds. |
| "I'll test after" | Tests passing immediately prove nothing. |
| "Deleting X hours is wasteful" | Sunk cost fallacy. Keeping unverified code is technical debt. |
| "TDD will slow me down" | TDD faster than debugging. |

## Verification Checklist

- [ ] Every new function has a test
- [ ] Watched each test fail before implementing
- [ ] Wrote minimal code to pass each test
- [ ] All tests pass
- [ ] Tests use real code (mocks only if unavoidable)
""",

"requesting-code-review": """---
name: requesting-code-review
description: "Pre-commit review: security scan, quality gates, verification."
version: 2.0.0
---

# Pre-Commit Code Verification

Automated verification pipeline before code lands.

**Core principle:** Catch issues before they reach the repository.

## When to Use

- After implementing a feature or bug fix, before ``git commit`` or ``git push``
- When user says "commit", "push", "ship", "done", "verify", or "review"
- After completing a task with 2+ file edits in a git repo

## Step 1 — Get the diff

```bash
git diff --cached
```

If empty, try ``git diff`` then ``git diff HEAD~1 HEAD``.

## Step 2 — Static security scan

Scan added lines only:

```bash
# Hardcoded secrets
git diff --cached | grep "^+" | grep -iE "(api_key|secret|password|token)\\s*=\\s*['\\\"]"

# Shell injection
git diff --cached | grep "^+" | grep -E "os\\.system\\(|subprocess.*shell=True"

# Dangerous eval/exec
git diff --cached | grep "^+" | grep -E "\\beval\\(|\\bexec\\("

# Unsafe deserialization
git diff --cached | grep "^+" | grep -E "pickle\\.loads?\\("
```

## Step 3 — Tests and linting

Detect project language and run appropriate tools:

```bash
# Python (pytest)
python -m pytest --tb=no -q 2>&1 | tail -5

# Node (npm test)
npm test -- --passWithNoTests 2>&1 | tail -5
```

Linting:
```bash
which ruff && ruff check . 2>&1 | tail -10
which mypy && mypy . --ignore-missing-imports 2>&1 | tail -10
```

## Step 4 — Self-review checklist

- [ ] No hardcoded secrets, API keys, or credentials
- [ ] Input validation on user-provided data
- [ ] SQL queries use parameterized statements
- [ ] File operations validate paths (no traversal)
- [ ] External calls have error handling
- [ ] No debug print/console.log left behind
- [ ] No commented-out code
- [ ] New code has tests

## Step 5 — Present findings

Combine results from Steps 2, 3, and 4. If all passed, proceed to commit. If failures, report what failed.

## Step 6 — Commit

```bash
git add -A && git commit -m "[verified] <description>"
```
""",

"github-pr-workflow": """---
name: github-pr-workflow
description: "GitHub PR lifecycle: branch, commit, open, CI, merge."
version: 1.1.0
---

# GitHub Pull Request Workflow

Complete guide for managing the PR lifecycle with ``gh`` CLI.

## Prerequisites

- ``gh`` CLI installed and authenticated (``gh auth status``)
- Inside a git repository with a GitHub remote

### Auth detection

```bash
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  AUTH="gh"
else
  AUTH="git+curl"
fi
```

## 1. Branch Creation

```bash
git fetch origin
git checkout main && git pull origin main
git checkout -b feat/add-user-authentication
```

Branch naming: ``feat/``, ``fix/``, ``refactor/``, ``docs/``, ``ci/``

## 2. Making Commits

```bash
git add src/auth.py tests/test_auth.py
git commit -m "feat: add JWT-based user authentication

- Add login/register endpoints
- Add User model with password hashing
- Add unit tests for auth flow"
```

Commit format: ``type(scope): short description``

Types: ``feat``, ``fix``, ``refactor``, ``docs``, ``test``, ``ci``, ``chore``, ``perf``

## 3. Push and Create PR

```bash
git push -u origin HEAD

gh pr create \\
  --title "feat: add JWT-based user authentication" \\
  --body "## Summary
- Adds login and register API endpoints

## Test Plan
- [ ] Unit tests pass

Closes #42"
```

Options: ``--draft``, ``--reviewer user1,user2``, ``--label "enhancement"``

## 4. Monitoring CI

```bash
gh pr checks          # one-shot
gh pr checks --watch  # poll until done
```

## 5. Auto-Fixing CI Failures

```bash
gh run list --branch $(git branch --show-current) --limit 5
gh run view <RUN_ID> --log-failed
# Fix the issue, then:
git add <fixed_files>
git commit -m "fix: resolve CI failure in <check_name>"
git push
```

Auto-fix loop: check CI → read logs → fix → push → re-check (up to 3 attempts).

## 6. Merging

```bash
gh pr merge --squash --delete-branch          # squash merge + delete branch
gh pr merge --auto --squash --delete-branch   # auto-merge when checks pass
```

## 7. Complete Workflow Example

```bash
git checkout main && git pull origin main
git checkout -b fix/login-redirect-bug
# (make changes)
git add src/auth/login.py tests/test_login.py
git commit -m "fix: correct redirect URL after login"
git push -u origin HEAD
gh pr create --title "fix: correct redirect URL" --body "..."
gh pr checks --watch
gh pr merge --squash --delete-branch
```
""",

"github-code-review": """---
name: github-code-review
description: "Review PRs: diffs, inline comments via gh or REST."
version: 1.1.0
---

# GitHub Code Review

Perform code reviews on local changes before pushing, or review open PRs on GitHub.

## Prerequisites

- ``gh`` CLI installed and authenticated
- Inside a git repository

## 1. Reviewing Local Changes (Pre-Push)

```bash
git diff main...HEAD --stat     # scope
git diff main...HEAD            # full diff
git diff main...HEAD --name-only  # files changed
```

Check for common issues:
```bash
git diff main...HEAD | grep -n "print(\\|console\\.log\\|TODO\\|FIXME\\|HACK"
git diff main...HEAD | grep -in "password\\|secret\\|api_key\\|token.*="
```

### Output format

```
## Code Review Summary

### Critical
- **file:line** — Description. Suggestion.

### Warnings
- **file:line** — Description.

### Suggestions
- **file:line** — Description.

### Looks Good
- Positive observations
```

## 2. Reviewing a PR on GitHub

```bash
gh pr view 123
gh pr diff 123
gh pr diff 123 --name-only
```

Check out locally:
```bash
gh pr checkout 123
# or: git fetch origin pull/123/head:pr-123 && git checkout pr-123
```

### Leave inline comments

```bash
HEAD_SHA=$(gh pr view 123 --json headRefOid --jq '.headRefOid')

gh api repos/OWNER/REPO/pulls/123/comments \\
  --method POST \\
  -f body="This could be simplified with a list comprehension." \\
  -f path="src/auth/login.py" \\
  -f commit_id="$HEAD_SHA" \\
  -f line=45 \\
  -f side="RIGHT"
```

### Submit a formal review

```bash
gh pr review 123 --approve --body "LGTM!"
gh pr review 123 --request-changes --body "See inline comments."
gh pr review 123 --comment --body "Some suggestions, nothing blocking."
```

## 3. Review Checklist

### Correctness
- Does the code do what it claims?
- Edge cases handled? Error paths graceful?

### Security
- No hardcoded secrets
- Input validation, no SQL injection/XSS/path traversal

### Code Quality
- Clear naming, no unnecessary complexity, DRY, single responsibility

### Testing
- New code paths tested? Happy path and error cases?

## 4. Pre-Push Review Workflow

1. ``git diff main...HEAD --stat`` — scope
2. ``git diff main...HEAD`` — full diff
3. Read changed files for context
4. Apply checklist
5. Present findings
6. Fix critical issues before pushing
""",

"spike": """---
name: spike
description: "Throwaway experiments to validate an idea before build."
version: 1.0.0
---

# Spike

Use this skill when the user wants to **feel out an idea** before committing to a real build — validating feasibility, comparing approaches, or surfacing unknowns.

Load this when the user says things like "let me try this", "I want to see if X works", "spike this out", "quick prototype of Z", "is this even possible?", or "compare A vs B".

## When NOT to use

- The answer is knowable from docs or reading code — just do research
- The work is production path — use the ``plan`` skill instead
- The idea is already validated — jump straight to implementation

## Core method

```
decompose  →  research  →  build  →  verdict
   ↑__________________________________________↓
                  iterate on findings
```

### 1. Decompose

Break the idea into 2-5 independent feasibility questions. Each question is one spike.

| # | Spike | Validates | Risk |
|---|-------|-----------|------|
| 001 | websocket-streaming | Given a WS connection, when LLM streams, then client receives chunks < 100ms | High |
| 002 | pdf-parse | Given a multi-page PDF, when parsed, then text is extractable | Medium |

**Order by risk.** The spike most likely to kill the idea runs first.

### 2. Research (per spike)

1. Brief it: 2-3 sentences — what, why, risk.
2. Surface competing approaches if there's real choice.
3. Pick one and state why.

### 3. Build

One directory per spike. Keep it standalone.

```
spikes/
├── 001-websocket-streaming/
│   ├── README.md
│   └── main.py
└── 002-pdf-parse/
    ├── README.md
    └── parse.py
```

**Bias toward something the user can interact with:**
1. A runnable CLI
2. A minimal HTML page
3. A small web server with one endpoint
4. A unit test with recognizable assertions

Avoid unless necessary: complex package management, Docker, env files. Hardcode everything — it's a spike.

### 4. Verdict

Each spike's ``README.md`` closes with:

```
## Verdict: VALIDATED | PARTIAL | INVALIDATED

### What worked
- ...

### What didn't
- ...

### Recommendation for the real build
- ...
```

**VALIDATED** = core question answered yes, with evidence.
**PARTIAL** = works under constraints X, Y, Z — document them.
**INVALIDATED** = doesn't work. This is a successful spike.
""",

}


def install_builtin_skills(force: bool = False) -> int:
    """Copy every bundled skill into the user's skills directory.

    Returns the number of skills newly installed. Existing files are
    left alone unless ``force=True`` — we never overwrite a user's
    edits to a built-in skill.
    """
    dest = get_skills_dir()
    installed = 0
    for name, body in SKILLS.items():
        target = dest / name
        skill_md = target / "SKILL.md"
        if skill_md.exists() and not force:
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            skill_md.write_text(body, encoding="utf-8")
            installed += 1
        except OSError as exc:
            logger.warning("skills: cannot install %s: %s", name, exc)
    if installed:
        logger.info("skills: installed %d built-in skill(s) to %s", installed, dest)
    return installed
