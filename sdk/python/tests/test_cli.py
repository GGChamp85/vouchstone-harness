"""``vouchstone`` CLI -- drives cli.main() with real argv through the real
kg pipeline on a temp tree (no subprocess, no mocks of our own code)."""
from __future__ import annotations

import json
from pathlib import Path

from vouchstone_sdk.cli import main


def _tree(root: Path) -> None:
    pkg = root / "acme"
    pkg.mkdir()
    (pkg / "core.py").write_text(
        "class Engine:\n    def start(self):\n        pass\n"
    )


def test_kg_build_verify_agents_flow(tmp_path: Path, capsys):
    _tree(tmp_path)
    out = tmp_path / "kg.json"

    assert main(["kg", "build", str(tmp_path), "-o", str(out)]) == 0
    built = capsys.readouterr().out
    assert "signature:" in built
    assert out.exists()

    assert main(["kg", "verify", str(out)]) == 0
    assert "VALID" in capsys.readouterr().out

    assert main(["kg", "agents", str(out), "--max-candidates", "3", "--json"]) == 0
    candidates = json.loads(capsys.readouterr().out)
    assert candidates and candidates[0]["agent_config"]["scoped_domains"]


def test_kg_verify_fails_on_tampered_artifact(tmp_path: Path, capsys):
    _tree(tmp_path)
    out = tmp_path / "kg.json"
    main(["kg", "build", str(tmp_path), "-o", str(out)])
    capsys.readouterr()

    data = json.loads(out.read_text())
    data["graph"]["entities"][0]["entity_key"] = "forged.py"
    out.write_text(json.dumps(data))

    assert main(["kg", "verify", str(out)]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_kg_diff_reports_changes(tmp_path: Path, capsys):
    _tree(tmp_path)
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    main(["kg", "build", str(tmp_path), "-o", str(old)])
    (tmp_path / "acme" / "extra.py").write_text("def helper():\n    pass\n")
    main(["kg", "build", str(tmp_path), "-o", str(new)])
    capsys.readouterr()

    assert main(["kg", "diff", str(old), str(new)]) == 0
    output = capsys.readouterr().out
    assert "files added (1):" in output
    assert "acme/extra.py" in output


def test_kg_incremental_flag_reuses_existing_output(tmp_path: Path, capsys):
    _tree(tmp_path)
    out = tmp_path / "kg.json"
    main(["kg", "build", str(tmp_path), "-o", str(out)])
    first_sig = json.loads(out.read_text())["signature"]
    capsys.readouterr()

    assert main(["kg", "build", str(tmp_path), "-o", str(out), "--incremental"]) == 0
    output = capsys.readouterr().out
    assert "incremental: reusing unchanged files" in output
    assert json.loads(out.read_text())["signature"] == first_sig
