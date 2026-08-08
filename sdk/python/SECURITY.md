# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email **admin@vouchstone.ai** with:

- A description of the issue and its impact
- Steps to reproduce (a minimal proof of concept helps)
- Affected version(s) — `vouchstone_sdk.__version__`

You will receive an acknowledgement within 3 business days. We ask for a
reasonable disclosure window while a fix is prepared and released.

## Supported versions

Only the latest released minor version receives security fixes.

## Scope notes for deployers

- `SubprocessSandboxRunner` is **not isolated** — it executes proposed code
  with the caller's own filesystem/network/privileges, and its docstring says
  so. Production deployments that sandbox untrusted, LLM-proposed code must
  supply a container-isolated `SandboxRunner` implementation.
- The SDK never phones home. All network calls go to endpoints you configure
  (your control plane, your vendor APIs for ingestion, your LLM provider).
- Dependency audit runs in CI via `pip-audit`; run `pip-audit` locally against
  your resolved environment before deploying.
