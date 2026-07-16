# Browser (Playwright) setup — one global Chromium, no dependency mess

Syntra's browser tools are **optional**. Syntra runs fine without them — they simply don't
register if Playwright isn't installed (graceful degradation in
`core/browser.py: playwright_available()`). Install them only if you want `/browse` and the
`browser_navigate/text/screenshot/click/fill` tools.

## Why `playwright install chromium` if `uv`/`pip` already ran?

They install **two different things**:

| Command | Installs | Where |
|---|---|---|
| `uv pip install playwright` (or pip) | the **Python `playwright` package** — the library that *drives* a browser | the venv's `site-packages` |
| `playwright install chromium` | the **actual Chromium browser binary** the library controls (~150 MB) | a **shared global cache** `~/.cache/ms-playwright/` |

Python packaging can't ship a 150 MB browser inside a wheel, so Playwright downloads the
binary separately into a cache that is **shared across every project and venv** — one global
copy, not one-per-project. Leave `PLAYWRIGHT_BROWSERS_PATH` unset so every project reuses the
same cache (setting it per-project fragments the cache).

## Clean install (isolated venv, no system-Python risk)

Many systems ship an "externally managed" `python3` (PEP 668) where installing packages
system-wide is discouraged or blocked. Use an isolated venv instead:

```bash
cd syntra
uv venv .venv                      # isolated env
source .venv/bin/activate
uv pip install playwright          # the library (a few small pure-Python wheels)
uv pip install -e .                # syntra, editable
playwright install chromium        # downloads Chromium once into the shared global cache
syntra                             # /browse <url> now works
```

If Chromium is already in `~/.cache/ms-playwright/` from another Playwright project, the last
`playwright install chromium` step is a no-op — nothing re-downloads.

## Notes

- Syntra's base install has **zero required runtime dependencies** (`pyproject.toml: dependencies = []`).
  Optional integrations such as Playwright, Pillow-backed image rendering, or richer schema/version
  helpers are additive — Syntra degrades gracefully when they are absent.
- Browser cache directories left by *other* tools (e.g. a manual Chromium download, or another
  browser-automation library's cache) are unrelated to Playwright's `~/.cache/ms-playwright/`
  and don't affect Syntra.
