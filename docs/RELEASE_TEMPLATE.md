# Syntra release-note template

Copy this template into each GitHub Release. Keep the Git tag, package version,
CLI version, and PyPI version aligned as described in [RELEASING.md](RELEASING.md).

~~~~md
## Highlights

-

## Fixed

-

## Changed

-

## Known beta limitations

- This remains an early public beta. List meaningful known limitations, migration
  notes, or provider/model changes here.

## Upgrade

```bash
uv tool upgrade syntra
# or: pipx upgrade syntra
# or: python3 -m pip install --user --upgrade syntra
```

## Security

- State security-relevant changes, or write `No security-specific changes in this release.`
- Do not disclose unpatched vulnerabilities here; use the private vulnerability
  reporting process in `SECURITY.md`.

## Contributors

Thanks to:

- @name — contribution summary
~~~~

## Beta release checklist

Before publishing a beta GitHub Release:

- [ ] `pyproject.toml` and `syntra/__init__.py` have the intended matching version.
- [ ] `CHANGELOG.md` has the user-facing summary.
- [ ] Public compile/import/lint checks pass.
- [ ] Private behavioral/security tests pass in the private development environment.
- [ ] The release tag uses `vX.Y.Z` and matches the package version `X.Y.Z`.
- [ ] The GitHub Release is marked **pre-release** until Syntra reaches stable 1.0.
- [ ] The `pypi` deployment was reviewed and approved.
- [ ] Install verification was performed from the published PyPI artifact.
