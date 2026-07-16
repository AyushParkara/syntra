## Summary

<!-- What changed and why? -->

## Validation

- [ ] `python3 -m compileall -q syntra`
- [ ] `python3 -c "import syntra, syntra.cli.main, syntra.cli.tui2; print('import OK')"`
- [ ] `uvx ruff check syntra` (if available)
- [ ] Manual validation described below

## Risk

- [ ] Low — docs, comments, small refactor, no behavior change
- [ ] Medium — user-visible behavior or provider/tool behavior
- [ ] High — safety, sandbox, permissions, secrets, MCP, browser, or file writes

## Notes

<!-- Include screenshots/logs if useful. Redact API keys, provider configs, private paths, and `.syntra/` state. -->
