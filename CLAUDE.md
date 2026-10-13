## Agent skills

### Issue tracker

Issues live as local markdown files under `.scratch/<feature>/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five labels (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix), recorded as a `Status:` line. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Environment

- Python: the main checkout's `.venv\Scripts\python.exe` (worktrees have no `.venv`). Run from the repo root; a script kept elsewhere needs `PYTHONPATH=<repo root>` to import `gcws`.
- Line endings: LF everywhere, CRLF only in `.cmd`/`.bat` (`.gitattributes`). Merge a branch that predates this rule with `git merge -X renormalize`.
