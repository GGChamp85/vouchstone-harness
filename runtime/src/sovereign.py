"""Sovereign deployment mode (C7b) -- hard network-egress guard.

C7b's full acceptance criteria (turnkey vLLM/TGI deployment via
`vouchstone harness init --sovereign --model=<name>`, benchmarking
open-weight models to confirm they're viable for production use) needs
real GPU/infra this environment doesn't have -- that part stays
explicitly deferred, tracked separately, not faked here.

What's built here is the one piece that's genuinely testable without
infra, and the part government/defense/EU-sovereignty-mandated buyers
actually need to be able to independently verify: an explicit mode that
hard-refuses to start at all if any known external LLM API dependency is
either *configured* (an API key is set with no local override) or
*actually reachable* over the network (egress isn't blocked at the
network/firewall level, regardless of what this process has configured).
Both checks are real -- no configuration flag is trusted at face value
without the reachability probe backing it up, since a customer relying
on sovereign mode is trusting that egress genuinely cannot happen, not
just that nothing in this process happens to try.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

# Well-known external LLM/embedding provider API hosts. Not exhaustive by
# construction (a customer can extend via `hosts=` on the reachability
# check) -- covers the providers this codebase's own integrations
# actually call (openai.py, anthropic usage across the SDK) plus other
# major hosted providers, since sovereign mode must catch a
# misconfiguration pointing at any of them, not just the two the SDK
# ships first-class clients for.
KNOWN_EXTERNAL_LLM_HOSTS: List[str] = [
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "api.cohere.ai",
    "api.mistral.ai",
    "api-inference.huggingface.co",
    "api.together.xyz",
    "api.groq.com",
]


class SovereignModeViolation(Exception):
    """Raised when sovereign mode is enabled but an external LLM
    dependency is configured or actually reachable. This is a hard
    startup failure, never a logged warning -- a warning can be missed;
    a process that refuses to start cannot be."""


def check_no_external_endpoints_configured(settings) -> None:
    """Static check: an external provider API key set with no
    LOCAL_LLM_BASE_URL override means the OpenAI/Anthropic SDKs will call
    their real hosted endpoints directly -- that's a configuration
    mistake sovereign mode exists to catch before it happens, not after."""
    violations = []
    if getattr(settings, "OPENAI_API_KEY", None) and not getattr(settings, "LOCAL_LLM_BASE_URL", None):
        violations.append(
            "OPENAI_API_KEY is set with no LOCAL_LLM_BASE_URL override -- "
            "the OpenAI SDK will call api.openai.com directly"
        )
    if getattr(settings, "ANTHROPIC_API_KEY", None) and not getattr(settings, "LOCAL_LLM_BASE_URL", None):
        violations.append(
            "ANTHROPIC_API_KEY is set with no LOCAL_LLM_BASE_URL override -- "
            "the Anthropic SDK will call api.anthropic.com directly"
        )
    if violations:
        raise SovereignModeViolation(
            "Sovereign mode is enabled but external LLM provider(s) are configured:\n"
            + "\n".join(f"  - {v}" for v in violations)
            + "\nRefusing to start. Unset these, or point LOCAL_LLM_BASE_URL at a "
              "self-hosted inference server (vLLM/TGI) if one is running."
        )


def check_no_external_endpoints_reachable(
    hosts: Optional[List[str]] = None, timeout: float = 2.0, port: int = 443,
) -> None:
    """Dynamic check: a real TCP connect probe (port 443 by default, no
    TLS handshake or HTTP request needed to prove reachability) against
    every known external LLM host. Proves egress is actually blocked at
    the network level -- independent of, and not trusting, this process's
    own configuration. A security reviewer can run this exact function
    standalone to verify the claim. `port` is overridable for testing
    against a local listener without requiring root (binding 443
    typically does); production callers should never override it.

    Runs synchronously (callers on an event loop should run this via
    asyncio.to_thread, as enforce_sovereign_mode does) but probes all
    hosts concurrently via a thread pool -- sequential probing at
    `timeout` seconds each would add hosts*timeout to every sovereign-mode
    startup (the expected-success case is every host timing out, since
    that's what "egress blocked" looks like), which is real latency worth
    avoiding, not just a style preference.
    """
    hosts = hosts if hosts is not None else KNOWN_EXTERNAL_LLM_HOSTS

    def _probe(host: str) -> Optional[str]:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return host
        except OSError:
            return None  # unreachable/blocked/DNS failure -- exactly what sovereign mode requires

    with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        reachable = [host for host in pool.map(_probe, hosts) if host is not None]

    if reachable:
        raise SovereignModeViolation(
            "Sovereign mode is enabled but the following external LLM endpoints "
            f"are reachable over the network: {', '.join(reachable)}. Refusing to "
            "start -- egress to these hosts must be blocked at the network/firewall "
            "level for sovereign mode to be a real guarantee, not just an unused "
            "configuration flag."
        )


async def enforce_sovereign_mode(settings, *, check_reachability: bool = True) -> None:
    """Call once at runtime startup, before anything else initializes.
    No-op if sovereign mode isn't enabled. Raises SovereignModeViolation
    (never silently warns or degrades) if either check fails.

    check_reachability defaults True; set False only for the static
    configuration check in isolation (e.g. fast unit tests) -- production
    startup must always run both.
    """
    if not getattr(settings, "SOVEREIGN_MODE", False):
        return
    check_no_external_endpoints_configured(settings)
    if check_reachability:
        import asyncio
        await asyncio.to_thread(check_no_external_endpoints_reachable)
