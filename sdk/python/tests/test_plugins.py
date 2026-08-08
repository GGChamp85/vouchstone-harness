"""Tests for the plugin model (C8).

Real ``importlib.metadata`` entry_points discovery is proven against a
real, separately installable package (``tests/fixtures/example_plugin_pkg``)
-- not a mocked ``importlib.metadata.entry_points`` call. The fixture
package is installed (regular, non-editable, local path, no network) once
per test session; if that install itself fails for environment reasons,
the discovery-dependent tests skip rather than silently passing on a
mock. Deliberately NOT an editable (`-e`) install -- editable installs
rely on a generated .pth/finder shim whose behavior is more
platform/pip-version-sensitive than a real package copy, and this
package only needs to be importable once, read-only, for this file.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from vouchstone_sdk import (
    ENGINE_ADAPTERS,
    EVAL_GRADERS,
    EXTRACTION_STRATEGIES,
    PluginLoadError,
    PluginRegistry,
)

FIXTURE_PKG_DIR = Path(__file__).resolve().parent / "fixtures" / "example_plugin_pkg"


@pytest.fixture(scope="session", autouse=True)
def _install_fixture_plugin_package():
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", str(FIXTURE_PKG_DIR), "-q"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"could not install fixture plugin package: {result.stderr[:500]}")
    yield


def _fixture_installed() -> bool:
    import importlib.metadata
    return any(d.name == "vouchstone-example-plugin" for d in importlib.metadata.distributions())


pytestmark = pytest.mark.skipif(
    not FIXTURE_PKG_DIR.exists(), reason="fixture plugin package directory missing"
)


def test_builtin_registries_have_real_pre_registered_members():
    assert "echo" in ENGINE_ADAPTERS.names()
    assert "claude" in ENGINE_ADAPTERS.names()
    assert "default" in EVAL_GRADERS.names()

    from vouchstone_sdk import EchoEngineAdapter
    assert ENGINE_ADAPTERS.get("echo") is EchoEngineAdapter


def test_extraction_strategies_registry_ships_builtins_and_accepts_registration():
    # Two built-in strategies ship (registered by vouchstone_sdk.ingestion
    # .base): "llm" (the default batched LLM extraction) and
    # "deterministic" (zero-LLM, air-gap-safe). Third parties extend the
    # same registry.
    import vouchstone_sdk.ingestion  # noqa: F401  -- registers built-ins
    assert {"llm", "deterministic"} <= set(EXTRACTION_STRATEGIES.names())

    def my_strategy(raw_content, source_metadata):
        return {"entities": [], "edges": []}

    EXTRACTION_STRATEGIES.register("noop", my_strategy)
    try:
        assert "noop" in EXTRACTION_STRATEGIES.names()
        assert EXTRACTION_STRATEGIES.get("noop") is my_strategy
    finally:
        EXTRACTION_STRATEGIES.unregister("noop")


def test_manual_registration_and_retrieval():
    registry = PluginRegistry("vouchstone.test_group_manual")
    registry.register("my_plugin", lambda: 42)

    assert "my_plugin" in registry.names()
    plugin = registry.get("my_plugin")
    assert plugin() == 42


def test_get_unknown_plugin_raises_with_available_names_listed():
    registry = PluginRegistry("vouchstone.test_group_unknown")
    registry.register("known", object())

    with pytest.raises(KeyError, match="known"):
        registry.get("nonexistent")


def test_unregister_removes_manual_plugin():
    registry = PluginRegistry("vouchstone.test_group_unregister")
    registry.register("temp", object())
    assert "temp" in registry.names()

    registry.unregister("temp")
    assert "temp" not in registry.names()


def test_real_entry_points_discovery_against_installed_fixture_package():
    if not _fixture_installed():
        pytest.skip("fixture plugin package did not install")

    # A dedicated registry for the "exact_match" entry point only, to keep
    # this test independent of EVAL_GRADERS' "broken" entry point (tested
    # separately below).
    registry = PluginRegistry("vouchstone.eval_graders")
    discovered = registry.discover()

    assert "exact_match" in discovered
    grader_fn = discovered["exact_match"]

    result = grader_fn("hello", "hello")
    assert result.passed is True
    assert result.reason == "exact_match plugin"

    result_mismatch = grader_fn("hello", "world")
    assert result_mismatch.passed is False


def test_broken_entry_point_raises_plugin_load_error_not_silently_skipped():
    if not _fixture_installed():
        pytest.skip("fixture plugin package did not install")

    # Dedicated group for this fixture's deliberately-broken entry point
    # (see fixtures/example_plugin_pkg/pyproject.toml) -- kept separate
    # from "vouchstone.eval_graders" so this failure doesn't leak into
    # every other test that touches the real EVAL_GRADERS registry.
    registry = PluginRegistry("vouchstone.test_broken_plugin_group")
    with pytest.raises(PluginLoadError, match="broken"):
        registry.discover()


def test_manual_registration_overrides_discovered_plugin_of_same_name():
    if not _fixture_installed():
        pytest.skip("fixture plugin package did not install")

    registry = PluginRegistry("vouchstone.eval_graders")

    def override_grader(actual, expected):
        raise AssertionError("should never be called by this test -- just proving override wins")

    registry.register("exact_match", override_grader)
    all_plugins = registry.all()
    assert all_plugins["exact_match"] is override_grader


def test_eval_graders_registry_module_level_reflects_real_installed_plugin():
    if not _fixture_installed():
        pytest.skip("fixture plugin package did not install")

    # EVAL_GRADERS is the actual module-level registry consumers import --
    # proves the SDK's own pre-wired registry, not just a fresh one built
    # in this test, sees the real third-party plugin once installed.
    assert "exact_match" in EVAL_GRADERS.names()
    assert "default" in EVAL_GRADERS.names()  # built-in still present alongside it
