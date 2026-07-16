# Public repository setup

Complete these settings in GitHub after creating `AyushParkara/syntra`.
They need maintainer account access, so they cannot be set from this checkout.

## Required before the public beta announcement

1. **General** — make the repository public and confirm Actions are enabled.
2. **Branches** — add a protection rule for `main`:
   - require a pull request before merging;
   - require the `ci / build` and `ci / lint` checks to pass;
   - require code-owner review if collaborators can merge;
   - do not allow force pushes or branch deletion.
3. **Security → Code security and analysis**:
   - enable private vulnerability reporting;
   - enable Dependabot alerts and Dependabot security updates;
   - enable secret scanning and push protection when available;
   - confirm CodeQL results appear after the first `main` push.
4. **Environments** — create the protected `pypi` environment described in
   [`RELEASING.md`](RELEASING.md) before the first PyPI release.
5. **Issues** — create and apply these labels before asking for contributions:

   ```text
   bug
   documentation
   enhancement
   good first issue
   help wanted
   provider
   routing
   safety
   TUI
   benchmark
   ```

## Recommended after the first public release

- Enable GitHub Discussions and add a pinned welcome post with contribution
  rules, support expectations, and links to the security policy.
- Add the GitHub repository social preview and a short terminal demo to the
  README.
- Create at least five genuinely small, clearly scoped `good first issue`s
  before inviting outside contributors.
- Review the Actions dependency pull requests created by Dependabot. They are
  proposals, not automatic merges.
