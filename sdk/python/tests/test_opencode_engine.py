"""Tests for OpenCodeEngineAdapter (Phase 5: OpenCode as default Forge
engine) and the engine-selection helpers in vouchstone_sdk.forge.

The real `opencode` CLI is NOT installed in this environment (confirmed:
`shutil.which("opencode")` returns nothing here) -- the plan explicitly
requires that this produce a real, honest EngineUnavailableError rather
than a silent fallback or a fabricated diff. These tests therefore prove
two independent things without ever needing the real binary:

1. The absent-binary path is real and honest (PATH-manipulation +
   explicit-mock variants of "opencode isn't there").
2. The file-materialization/diffing mechanics work correctly against a
   trivial STUB SCRIPT that stands in for "a minimal CLI that accepts
   `run <instruction> --dir <dir>` and `--version`" -- this proves the
   adapter's own logic (snapshot -> invoke -> re-diff). The real binary's
   contract (`--dir`, not `--cwd`; no `--non-interactive` flag -- it's the
   default) has since been verified end-to-end against opencode 1.18.15
   in tests/test_opencode_integration.py; this stub mirrors that verified
   contract, not a guess.
"""
from __future__ import annotations

import stat
import textwrap
from pathlib import Path

import pytest

from vouchstone_sdk import (
    ClaudeEngineAdapter,
    EchoEngineAdapter,
    EngineExecutionError,
    EngineUnavailableError,
    OpenCodeEngineAdapter,
    describe_forge_engine,
    get_default_engine_adapter,
)

# ============================================================
# Stub "opencode" CLI -- a shell script standing in for a minimal CLI
# contract, clearly NOT a claim about the real binary. Behavior is
# selected via the OPENCODE_STUB_MODE env var so one script covers every
# scenario below.
# ============================================================

_STUB_SCRIPT = textwrap.dedent(
    """\
    #!/bin/sh
    if [ "$1" = "--version" ]; then
      echo "opencode-stub 0.0.1-test"
      exit 0
    fi

    mode="${OPENCODE_STUB_MODE:-success}"

    if [ "$1" = "run" ]; then
      shift
      instruction="$1"
      shift
      cwd=""
      while [ "$#" -gt 0 ]; do
        case "$1" in
          --dir)
            shift
            cwd="$1"
            ;;
        esac
        shift
      done

      case "$mode" in
        success)
          echo "print('patched by stub')" > "$cwd/handler.py"
          exit 0
          ;;
        noop)
          exit 0
          ;;
        delete)
          rm -f "$cwd/obsolete.py"
          exit 0
          ;;
        fail)
          echo "stub: simulated failure" >&2
          exit 1
          ;;
        timeout)
          sleep 5
          exit 0
          ;;
      esac
    fi

    echo "stub: unrecognized invocation: $*" >&2
    exit 2
    """
)


@pytest.fixture
def stub_opencode(tmp_path: Path) -> Path:
    script = tmp_path / "opencode"
    script.write_text(_STUB_SCRIPT)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ============================================================
# Absent-binary path -- must be real, never a silent fallback
# ============================================================

async def test_binary_absent_from_path_raises_engine_unavailable_error(monkeypatch, tmp_path):
    empty_bin_dir = tmp_path / "empty_bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))
    monkeypatch.delenv("VOUCHSTONE_OPENCODE_PATH", raising=False)

    adapter = OpenCodeEngineAdapter()
    with pytest.raises(EngineUnavailableError) as exc_info:
        await adapter.propose_change("do something", {"files": {}})

    assert "not found on PATH" in str(exc_info.value)
    assert "VOUCHSTONE_OPENCODE_PATH" in str(exc_info.value)


async def test_binary_absent_raises_before_producing_any_diff(monkeypatch, tmp_path):
    empty_bin_dir = tmp_path / "empty_bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))
    monkeypatch.delenv("VOUCHSTONE_OPENCODE_PATH", raising=False)

    adapter = OpenCodeEngineAdapter()
    with pytest.raises(EngineUnavailableError):
        await adapter.propose_change("do something", {"files": {"a.py": "x = 1"}})
    # No Diff object is ever constructed/returned on this path -- the
    # exception itself is the only outcome, never a fabricated Diff.


async def test_shutil_which_mocked_to_none_raises_engine_unavailable_error(monkeypatch):
    import vouchstone_sdk.forge as forge_module

    monkeypatch.delenv("VOUCHSTONE_OPENCODE_PATH", raising=False)
    monkeypatch.setattr(forge_module.shutil, "which", lambda name: None)

    adapter = OpenCodeEngineAdapter()
    with pytest.raises(EngineUnavailableError, match="not found on PATH"):
        await adapter.propose_change("do something", {"files": {}})


async def test_configured_binary_path_that_does_not_exist_raises_engine_unavailable_error(tmp_path):
    adapter = OpenCodeEngineAdapter(binary_path=str(tmp_path / "nonexistent-opencode"))
    with pytest.raises(EngineUnavailableError, match="does not exist or is not executable"):
        await adapter.propose_change("do something", {"files": {}})


async def test_env_var_configured_binary_path_that_does_not_exist_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("VOUCHSTONE_OPENCODE_PATH", str(tmp_path / "also-nonexistent"))
    adapter = OpenCodeEngineAdapter()
    with pytest.raises(EngineUnavailableError, match="does not exist or is not executable"):
        await adapter.propose_change("do something", {"files": {}})


# ============================================================
# File-materialization/diffing mechanics against the stub script
# ============================================================

async def test_propose_change_diffs_stub_output_against_materialized_files(stub_opencode, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "success")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode))

    current_files = {
        "handler.py": "def handle(): pass\n",
        "readme.md": "# unrelated, unchanged file\n",
    }
    diff = await adapter.propose_change("patch the handler", {"files": current_files})

    assert diff.engine_name == "opencode"
    assert diff.description == "patch the handler"
    # Only the file the stub actually touched shows up -- readme.md is
    # untouched and must be omitted entirely, not included as a no-op change.
    assert len(diff.changes) == 1
    change = diff.changes[0]
    assert change.file_path == "handler.py"
    assert change.original_content == "def handle(): pass\n"
    assert "patched by stub" in change.new_content
    assert diff.metadata["exit_code"] == 0
    assert diff.metadata["engine_version"] == "opencode-stub 0.0.1-test"


async def test_propose_change_returns_empty_changes_when_nothing_changed(stub_opencode, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "noop")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode))

    diff = await adapter.propose_change("do nothing", {"files": {"handler.py": "def handle(): pass\n"}})

    # Honest empty diff, NOT an error: the engine ran successfully (exit 0)
    # and genuinely made no changes.
    assert diff.changes == []
    assert diff.metadata["exit_code"] == 0


async def test_propose_change_raises_engine_execution_error_on_nonzero_exit(stub_opencode, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "fail")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode))

    with pytest.raises(EngineExecutionError) as exc_info:
        await adapter.propose_change("this will fail", {"files": {"handler.py": "def handle(): pass\n"}})

    assert "simulated failure" in str(exc_info.value)
    assert "exited with code" in str(exc_info.value)


async def test_propose_change_raises_engine_execution_error_on_timeout(stub_opencode, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "timeout")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode), timeout_seconds=0.3)

    with pytest.raises(EngineExecutionError, match="timed out"):
        await adapter.propose_change("this will hang", {"files": {"handler.py": "def handle(): pass\n"}})


async def test_propose_change_materializes_nested_paths(stub_opencode, monkeypatch):
    monkeypatch.setenv("OPENCODE_STUB_MODE", "noop")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode))

    # Proves materialization creates parent directories for nested paths
    # rather than failing on write (a FileNotFoundError writing
    # pkg/sub/module.py without mkdir(parents=True) would propagate as an
    # exception before we ever got a Diff back).
    diff = await adapter.propose_change(
        "no-op over nested files", {"files": {"pkg/sub/module.py": "x = 1\n"}},
    )
    assert diff.changes == []  # noop mode touches nothing


# ============================================================
# get_default_engine_adapter() / VOUCHSTONE_FORGE_ENGINE
# ============================================================

def test_get_default_engine_adapter_defaults_to_opencode(monkeypatch):
    monkeypatch.delenv("VOUCHSTONE_FORGE_ENGINE", raising=False)
    adapter = get_default_engine_adapter()
    assert isinstance(adapter, OpenCodeEngineAdapter)
    assert adapter.engine_name == "opencode"


def test_get_default_engine_adapter_respects_env_var_override(monkeypatch):
    monkeypatch.setenv("VOUCHSTONE_FORGE_ENGINE", "claude")
    adapter = get_default_engine_adapter(api_key="test-key")
    assert isinstance(adapter, ClaudeEngineAdapter)


def test_get_default_engine_adapter_echo_still_works_explicitly(monkeypatch):
    monkeypatch.setenv("VOUCHSTONE_FORGE_ENGINE", "echo")
    adapter = get_default_engine_adapter(transform=lambda instruction, files: None)
    assert isinstance(adapter, EchoEngineAdapter)


# ============================================================
# describe_forge_engine() -- the harness_cli `status` reporting helper
# ============================================================

def test_describe_forge_engine_reports_absence(monkeypatch, tmp_path):
    empty_bin_dir = tmp_path / "empty_bin"
    empty_bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin_dir))
    monkeypatch.delenv("VOUCHSTONE_OPENCODE_PATH", raising=False)
    monkeypatch.delenv("VOUCHSTONE_FORGE_ENGINE", raising=False)

    info = describe_forge_engine()
    assert info["configured_engine"] == "opencode"
    assert info["opencode_binary_present"] is False
    assert info["opencode_binary_path"] is None
    assert info["opencode_version"] is None


def test_describe_forge_engine_reports_presence_and_version_via_stub(stub_opencode, monkeypatch):
    monkeypatch.setenv("VOUCHSTONE_OPENCODE_PATH", str(stub_opencode))
    monkeypatch.delenv("VOUCHSTONE_FORGE_ENGINE", raising=False)

    info = describe_forge_engine()
    assert info["configured_engine"] == "opencode"
    assert info["opencode_binary_present"] is True
    assert info["opencode_binary_path"] == str(stub_opencode)
    assert info["opencode_version"] == "opencode-stub 0.0.1-test"


# ============================================================
# Shared-import sanity: ClaudeEngineAdapter/EchoEngineAdapter unaffected
# ============================================================

def test_claude_and_echo_adapters_still_import_and_instantiate():
    claude = ClaudeEngineAdapter(api_key="x")
    assert claude.engine_name == "claude-direct"

    echo = EchoEngineAdapter(transform=lambda instruction, files: None)
    assert echo.engine_name == "echo-reference"


async def test_deleted_files_appear_in_diff_as_empty_new_content(
    stub_opencode, monkeypatch,
):
    """A file the engine removes must show up as a FileChange with
    new_content="" -- deletions were previously invisible in the diff."""
    monkeypatch.setenv("OPENCODE_STUB_MODE", "delete")
    adapter = OpenCodeEngineAdapter(binary_path=str(stub_opencode))
    diff = await adapter.propose_change(
        "remove the obsolete module",
        {"files": {"keep.py": "x = 1\n", "obsolete.py": "old = True\n"}},
    )
    deletions = [c for c in diff.changes if c.new_content == ""]
    assert [c.file_path for c in deletions] == ["obsolete.py"]
    assert deletions[0].original_content == "old = True\n"
