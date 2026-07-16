# Releasing Syntra

This is the maintainer checklist for publishing Syntra to PyPI.

See [PUBLIC_BETA_CHECKLIST.md](PUBLIC_BETA_CHECKLIST.md) for the wider GitHub
community/security setup and [RELEASE_TEMPLATE.md](RELEASE_TEMPLATE.md) for the
copy-paste GitHub Release notes format.

## Fixed public coordinates

| Setting | Value |
| --- | --- |
| GitHub repository | `AyushParkara/syntra` |
| PyPI project | `syntra` |
| Release workflow | `.github/workflows/release.yml` |
| GitHub Actions environment | `pypi` |

The user-facing brand is **Syntra**. Use lowercase `syntra` for the Python
package, terminal command, PyPI project, and GitHub repository path.

## One-time account setup

These steps require the maintainer's GitHub and PyPI accounts; they cannot be
performed from a source checkout.

1. Create the public GitHub repository at `https://github.com/AyushParkara/syntra`.
2. Push the `main` branch, including `.github/workflows/release.yml`.
3. In the GitHub repository, create an Actions environment named `pypi`.
   Configure a required reviewer before the first release if you want each
   publish to require human approval.
4. In PyPI account settings, add a **pending GitHub Actions Trusted Publisher**
   with these exact values:

   ```text
   PyPI project name: syntra
   GitHub owner: AyushParkara
   GitHub repository: syntra
   Workflow filename: release.yml
   Environment name: pypi
   ```

5. Do not add a `PYPI_API_TOKEN` GitHub secret. The release workflow uses PyPI
   Trusted Publishing and GitHub's short-lived OIDC identity instead.

> A PyPI pending publisher does not reserve the `syntra` name. Publish the first
> release promptly after completing the configuration.

## Every release

1. Set the intended version in `pyproject.toml` and `syntra/__init__.py`.
2. Add the release notes to `CHANGELOG.md`.
3. Run the checks that are safe to execute from the public checkout:

   ```bash
   python3 -m compileall -q syntra
   python3 -c "import syntra, syntra.cli.main, syntra.cli.tui2; print('import OK')"
   python3 -m syntra --version
   uvx ruff check syntra
   python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/syntra-wheel .
   ```

4. Run the private test suite in the private development environment.
5. Commit and push the release version to `main`.
6. Create and publish a GitHub Release from the matching tag, for example
   `v0.1.0`. Mark beta releases as **pre-releases**.
7. Review and approve the `pypi` environment deployment when GitHub Actions
   pauses the publish job.
8. Verify the published artifact in a fresh environment:

   ```bash
   python3 -m venv /tmp/syntra-smoke
   /tmp/syntra-smoke/bin/python -m pip install --upgrade syntra
   /tmp/syntra-smoke/bin/syntra --version
   /tmp/syntra-smoke/bin/syntra --help
   ```

9. Confirm the GitHub Release links to the matching PyPI version, then publish
   the public announcement.

## Failure handling

- If PyPI reports that `syntra` is already taken, stop and do not publish under
  a lookalike name without deciding on the new package/repository identity.
- PyPI releases cannot be replaced with a different file under the same version.
  Fix the issue, increment the version, and publish a new release.
- If Trusted Publishing fails, compare every pending-publisher value above with
  the repository and workflow exactly; owner, repository, workflow filename,
  and environment are all part of the authorization boundary.
