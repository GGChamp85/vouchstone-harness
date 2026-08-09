# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities in
any of this repo's three components (`sdk/python/`, `runtime/`, `mcp-server/`).

Email **admin@vouchstone.ai** with:

- A description of the issue and its impact
- Steps to reproduce (a minimal proof of concept helps)
- Affected component and version (`vouchstone_sdk.__version__` for the SDK;
  commit SHA for `runtime/`/`mcp-server/`, which aren't independently
  versioned yet)

You will receive an acknowledgement within 3 business days. We ask for a
reasonable disclosure window while a fix is prepared and released.

## Supported versions

- **`sdk/python/`** — only the latest released minor version on
  [PyPI](https://pypi.org/project/vouchstone-sdk/) receives security fixes.
- **`runtime/`** and **`mcp-server/`** — track `main`; there's no separate
  release/support-window process for these two yet.

## Scope notes for deployers

- **`SubprocessSandboxRunner`** (SDK, `Forge`) is **not isolated** — it
  executes proposed code with the caller's own filesystem/network/privileges,
  and its docstring says so. Production deployments that sandbox untrusted,
  LLM-proposed code must supply a container-isolated `SandboxRunner`
  implementation.
- **Multi-tenant isolation is the control plane's responsibility, not this
  repo's.** Everything here (SDK, runtime, MCP server) is a client of a
  control plane you operate; it doesn't itself enforce tenant boundaries.
  If you're running your own control plane, its own multi-tenant query
  scoping is the trust boundary — audit it accordingly.
- **`runtime/`'s sovereign mode** (`VOUCHSTONE_SOVEREIGN_MODE=true`) is a
  real, independently-verifiable network-egress guard (see
  [`runtime/README.md`](runtime/README.md)), not a policy statement — but it
  only covers the runtime process itself, not anything else in your
  deployment.
- **Nothing in this repo phones home.** All network calls go to endpoints you
  configure (your control plane, your vendor APIs for ingestion/connectors,
  your LLM provider). The MCP server only ever talks to the control plane URL
  you set via `VOUCHSTONE_API_URL`.
- **Dependency scanning**: `sdk/python`'s CI runs `pip-audit`; `runtime/`
  ships a real `pip-audit` wrapper as a CI/CD gate (`harness scan`, see
  `runtime/README.md`); `mcp-server/` is checked with `npm audit`. Run the
  relevant one locally against your resolved environment before deploying,
  and check the [CHANGELOG](CHANGELOG.md) for any currently-tracked,
  upstream-unpatched CVEs (documented rather than silently ignored when a fix
  isn't available yet).
