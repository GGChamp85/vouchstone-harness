"""Tests for sovereign deployment mode (C7b).

The reachability check is proven both ways with a real socket, not a
mocked one: a real local TCP listener makes a host genuinely reachable
(must raise), and a hostname that doesn't resolve makes it genuinely
unreachable (must not raise). The full `enforce_sovereign_mode` default
host list (real production LLM API hosts) is deliberately never
network-probed in these tests -- CI runners typically have open internet
egress, so a "should pass" assertion against real external hosts would
be flaky and would generate unwanted real traffic to third-party
services on every test run. The underlying mechanism is proven directly
against controllable hosts/ports instead (tests below), which is what
actually needs proving.
"""
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Settings
from src.sovereign import (
    SovereignModeViolation, check_no_external_endpoints_configured,
    check_no_external_endpoints_reachable, enforce_sovereign_mode,
)


def _settings(**overrides) -> Settings:
    return Settings(
        CONTROL_PLANE_URL="http://localhost:8000", TENANT_ID="t1",
        EXECUTION_STORE_PATH="/tmp/does-not-matter.db",
        **overrides,
    )


# ---------------------------------------------------------------------
# Static configuration check
# ---------------------------------------------------------------------

def test_openai_key_without_local_override_violates():
    settings = _settings(SOVEREIGN_MODE=True, OPENAI_API_KEY="sk-real-key")
    with pytest.raises(SovereignModeViolation, match="OPENAI_API_KEY"):
        check_no_external_endpoints_configured(settings)


def test_anthropic_key_without_local_override_violates():
    settings = _settings(SOVEREIGN_MODE=True, ANTHROPIC_API_KEY="sk-ant-real-key")
    with pytest.raises(SovereignModeViolation, match="ANTHROPIC_API_KEY"):
        check_no_external_endpoints_configured(settings)


def test_both_keys_configured_lists_both_violations():
    settings = _settings(SOVEREIGN_MODE=True, OPENAI_API_KEY="sk-x", ANTHROPIC_API_KEY="sk-ant-x")
    with pytest.raises(SovereignModeViolation) as exc_info:
        check_no_external_endpoints_configured(settings)
    assert "OPENAI_API_KEY" in str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_local_llm_base_url_permits_keys_to_remain_set():
    # A key might still be set (e.g. for an unrelated fallback path) but
    # LOCAL_LLM_BASE_URL being configured means the runtime isn't
    # actually calling the cloud provider -- not a violation.
    settings = _settings(
        SOVEREIGN_MODE=True, OPENAI_API_KEY="sk-x",
        LOCAL_LLM_BASE_URL="http://localhost:8000/v1",
    )
    check_no_external_endpoints_configured(settings)  # must not raise


def test_no_keys_configured_passes_static_check():
    settings = _settings(SOVEREIGN_MODE=True)
    check_no_external_endpoints_configured(settings)  # must not raise


# ---------------------------------------------------------------------
# Dynamic reachability check -- real sockets both directions
# ---------------------------------------------------------------------

def test_reachability_check_passes_when_host_does_not_resolve():
    check_no_external_endpoints_reachable(
        hosts=["definitely-not-a-real-host-xyz123.invalid"], timeout=1.0,
    )  # must not raise -- DNS failure is exactly "not reachable"


def test_reachability_check_raises_when_a_host_is_actually_reachable():
    # Real local TCP listener -- proves the check genuinely detects
    # reachability over the network, not just that it "usually" would.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen(1)

    def _accept_loop():
        try:
            server.settimeout(3.0)
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    try:
        with pytest.raises(SovereignModeViolation, match="127.0.0.1"):
            check_no_external_endpoints_reachable(hosts=["127.0.0.1"], port=port, timeout=2.0)
    finally:
        server.close()
        thread.join(timeout=3.0)


def test_reachability_check_probes_multiple_unreachable_hosts_without_raising():
    check_no_external_endpoints_reachable(
        hosts=[
            "definitely-not-a-real-host-xyz123.invalid",
            "also-not-a-real-host-abc456.invalid",
        ],
        timeout=1.0,
    )


# ---------------------------------------------------------------------
# enforce_sovereign_mode -- the async entry point runtime.initialize() calls
# ---------------------------------------------------------------------

async def test_enforce_sovereign_mode_is_a_full_noop_when_disabled(monkeypatch):
    settings = _settings(SOVEREIGN_MODE=False, OPENAI_API_KEY="sk-x")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("reachability check must never run when sovereign mode is disabled")

    import src.sovereign as sovereign_module
    monkeypatch.setattr(sovereign_module, "check_no_external_endpoints_reachable", _fail_if_called)

    await enforce_sovereign_mode(settings)  # must not raise, must not call the patched function


async def test_enforce_sovereign_mode_raises_from_static_check_without_touching_network(monkeypatch):
    settings = _settings(SOVEREIGN_MODE=True, OPENAI_API_KEY="sk-x")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("reachability check must never run once the static check already violated")

    import src.sovereign as sovereign_module
    monkeypatch.setattr(sovereign_module, "check_no_external_endpoints_reachable", _fail_if_called)

    with pytest.raises(SovereignModeViolation, match="OPENAI_API_KEY"):
        await enforce_sovereign_mode(settings)


async def test_enforce_sovereign_mode_check_reachability_false_skips_network_entirely(monkeypatch):
    settings = _settings(SOVEREIGN_MODE=True)  # passes static check

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("must not run when check_reachability=False")

    import src.sovereign as sovereign_module
    monkeypatch.setattr(sovereign_module, "check_no_external_endpoints_reachable", _fail_if_called)

    await enforce_sovereign_mode(settings, check_reachability=False)  # must not raise
