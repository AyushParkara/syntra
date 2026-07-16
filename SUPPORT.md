# Support policy

Syntra is an early public beta. Support is best-effort and community-oriented;
there is no guaranteed response time or commercial support commitment.

## Where to ask or report something

| Need | Use this route |
| --- | --- |
| A reproducible defect | GitHub **Bug report** issue form |
| A feature or workflow improvement | GitHub **Feature request** issue form |
| Incorrect, confusing, or stale documentation | GitHub **Documentation feedback** issue form |
| Unexpected route, provider, cost, or failover result | GitHub **Routing or provider feedback** issue form |
| Installation/configuration/usage question | GitHub **Question or support request** issue form; use Discussions once they are enabled |
| Security vulnerability, secret exposure, sandbox escape, approval bypass, or unexpected command execution | **Do not open a public issue.** Follow [SECURITY.md](SECURITY.md) and use GitHub private vulnerability reporting when it is enabled. |

## Before opening an issue

1. Run `syntra --version` and include the output.
2. Run the narrowest command that reproduces the problem.
3. Remove API keys, access tokens, provider config contents, private project paths,
   private source code, and `.syntra/` state from the report.
4. State what you expected and what actually happened.

## Beta expectations

- Interfaces, routing heuristics, catalog data, provider support, and configuration
  may change between beta releases.
- A route is an inspectable recommendation, not a guarantee of model quality,
  price, availability, or task success.
- The full internal behavioral/security test suite is not included in this public
  repository. Public changes still require the documented compile, import, lint,
  and manual validation evidence.
