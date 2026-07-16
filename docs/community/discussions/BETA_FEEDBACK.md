# Known beta boundaries and useful feedback

Syntra is an early public beta. It is usable, but it is still being hardened.

## Expected beta rough edges

- Model/provider availability and pricing change outside Syntra's control.
- The bundled model catalog is an approximate seed snapshot, not a guarantee of
  live benchmark data or ideal routing for every workload.
- Provider configuration, OAuth, browser, MCP, local-model, and terminal behavior
  can vary across operating systems and environments.
- Safety boundaries are layered but are not a substitute for reviewing approvals,
  using trusted workspaces, and selecting `sandbox=require` where appropriate.

## Feedback that changes the product

Please include a sanitized example when reporting:

- a route that felt wrong and what you expected instead;
- a fallback that did not recover cleanly;
- unexpected spend or a misleading cost estimate;
- a failed command, edit, verification, or resume path;
- a confusing first-run, provider, or permission prompt; or
- a documentation page that did not answer the question you had.

For model-routing feedback, the `Routing or provider feedback` issue form is the
best route. For normal ideas or questions, use the relevant Discussion category.
