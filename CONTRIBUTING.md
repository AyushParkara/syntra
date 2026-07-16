# Contributing to Syntra

Thanks for wanting to improve Syntra.

Syntra is a TUI-first multi-model coding assistant. The most helpful contributions are focused, well-explained, and easy to verify.

Before opening anything, read [SUPPORT.md](SUPPORT.md) to choose the right route
for a bug, question, routing/provider report, documentation feedback, or private
security report.

## Before You Start

- Open an issue first for large changes, safety behavior, routing changes, provider behavior, or UX redesigns.
- Keep pull requests small when possible.
- Do not include API keys, private provider configs, screenshots with secrets, or personal `.syntra/` state.
- If behavior changes, update the relevant docs.

## Local Setup

```bash
git clone https://github.com/AyushParkara/syntra.git
cd syntra
python3 -m pip install -e .
```

Useful checks:

```bash
python3 -m compileall -q syntra
python3 -c "import syntra, syntra.cli.main, syntra.cli.tui2; print('import OK')"
uvx ruff check syntra
```

The full private development test suite is not shipped in this public repo yet. Please include a clear manual validation note in your PR.

## Pull Request Checklist

- [ ] The change is focused and explained.
- [ ] `python3 -m compileall -q syntra` passes.
- [ ] Import check passes.
- [ ] Docs are updated if user-visible behavior changed.
- [ ] No secrets, local state, screenshots, or private files are included.

## Safety-Sensitive Changes

Be extra careful with changes touching:

- shell command classification or sandboxing;
- file write/edit/apply-patch paths;
- provider key handling;
- MCP, browser, web, or repo-local plugin surfaces;
- approval/access modes.

For these, explain what can now run, what still asks, and what remains blocked.
