"""Tests for dependency vulnerability scanning (C8).

These invoke the real ``pip-audit`` CLI as a subprocess against a real
requirements file -- not a mocked/faked scan result -- pinning a package
version with well-known, long-published CVEs (urllib3 1.24.1) so the
"vulnerabilities found" path is proven against real advisory data, not
an assumption about pip-audit's behavior. Requires network access to
query the OSV database, same as a real CI run would need.
"""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dependency_scan import (
    DependencyScanError, PipAuditNotAvailableError, ScanResult, run_dependency_scan,
)

pytestmark = pytest.mark.skipif(
    shutil.which("pip-audit") is None,
    reason="pip-audit not installed in this environment -- install with `pip install pip-audit` to run these",
)


def test_scan_clean_requirement_file_reports_no_vulnerabilities(tmp_path):
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("certifi==2024.7.4\n")

    result = run_dependency_scan(requirement_path=str(req_file))

    assert isinstance(result, ScanResult)
    assert result.tool == "pip-audit"
    assert result.has_vulnerabilities is False
    assert result.total_vulnerability_count == 0
    assert result.scanned_package_count >= 1


def test_scan_known_vulnerable_pin_reports_real_vulnerabilities(tmp_path):
    # urllib3 1.24.1 has multiple long-published, well-known CVEs
    # (PYSEC-2019-132, PYSEC-2019-133, ...) -- a real, stable fixture for
    # proving the "vulnerabilities found" path against real advisory data.
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("urllib3==1.24.1\n")

    result = run_dependency_scan(requirement_path=str(req_file))

    assert result.has_vulnerabilities is True
    assert result.total_vulnerability_count > 0
    vulnerable_names = {p.name for p in result.vulnerable_packages}
    assert "urllib3" in vulnerable_names

    urllib3_result = next(p for p in result.vulnerable_packages if p.name == "urllib3")
    assert urllib3_result.version == "1.24.1"
    assert len(urllib3_result.vulnerabilities) > 0
    assert all(v.id for v in urllib3_result.vulnerabilities)

    as_dict = result.to_dict()
    assert as_dict["total_vulnerability_count"] == result.total_vulnerability_count
    assert as_dict["vulnerable_packages"][0]["name"] in ("urllib3",)


def test_scan_malformed_requirement_file_raises_dependency_scan_error(tmp_path):
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("this is not a valid requirement === specifier\n")

    with pytest.raises(DependencyScanError):
        run_dependency_scan(requirement_path=str(req_file))


def test_pip_audit_not_available_is_a_distinct_error_from_clean_scan(tmp_path, monkeypatch):
    """Proves 'couldn't scan' is never silently reported as 'scanned,
    clean' -- patches subprocess.run to simulate pip-audit missing from
    PATH, the exact FileNotFoundError a real missing-executable would
    raise."""
    import subprocess as subprocess_module

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("pip-audit not found")

    monkeypatch.setattr(subprocess_module, "run", fake_run)

    with pytest.raises(PipAuditNotAvailableError):
        run_dependency_scan(requirement_path=str(tmp_path / "requirements.txt"))
