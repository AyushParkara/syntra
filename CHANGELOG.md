# Changelog

All notable public changes to Syntra will be documented in this file.

This project follows semantic versioning once public releases are tagged.

## [0.1.0] - 2026-07-16

### Added

- Initial public beta of Syntra.
- TUI-first cockpit for multi-model coding workflows.
- Capability-aware model routing for planner, executor, reviewer, and analyzer roles.
- Structured task state on disk instead of one large chat log.
- Provider configuration with multi-key failover.
- MCP stdio/HTTP support for external tools.
- Safety-gated file, shell, browser, web, and network tool surfaces.
- Optional browser, image, YouTube transcript, LSP, and local-model integrations.
- Plain-English docs for setup, commands, config, security, model selection, reliability, review, providers, MCP, skills, and tools.

### Security

- Hardened shell command classification, including command-substitution and pipe-to-interpreter cases.
- Hardened path handling for task IDs, browser screenshots, and workspace writes.
- Removed bundled YouTube InnerTube key; users configure their own key for transcript/watch support.
