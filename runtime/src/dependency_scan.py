"""Dependency vulnerability scanning for harness bundles (C8).

A bundle pins an SDK version (``BundleManifest.sdk_version``) that will
run unattended, often air-gapped, against a customer's own data. Wraps
the real ``pip-audit`` CLI (queries the OSV/PyPI advisory database) --
not a hand-rolled or fake vulnerability list -- and parses its actual
``--format=json`` output schema.

Distinguishes two states that must never be conflated: "scanned, zero
vulnerabilities found" vs. "couldn't scan at all" (pip-audit not
installed). Silently treating the second as the first would be exactly
the kind of false assurance this SDK's "no stubs, no silent fallbacks"
bar exists to prevent -- callers get a distinct exception instead.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


class PipAuditNotAvailableError(Exception):
    """Raised when the ``pip-audit`` executable isn't installed/on PATH.
    Callers must treat this differently from a clean scan result --
    "not scanned" is not the same claim as "scanned, found nothing."""


class DependencyScanError(Exception):
    """Raised when pip-audit ran but exited in a way that isn't the
    documented 0 (clean) / 1 (vulnerabilities found) contract -- e.g. a
    malformed requirement file. Surfacing this beats silently treating an
    unexpected failure as either a clean or vulnerable result."""


@dataclass
class Vulnerability:
    id: str
    fix_versions: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)


@dataclass
class PackageVulnerabilities:
    name: str
    version: str
    vulnerabilities: List[Vulnerability] = field(default_factory=list)


@dataclass
class ScanResult:
    tool: str
    tool_version: str
    scanned_package_count: int
    vulnerable_packages: List[PackageVulnerabilities] = field(default_factory=list)

    @property
    def total_vulnerability_count(self) -> int:
        return sum(len(p.vulnerabilities) for p in self.vulnerable_packages)

    @property
    def has_vulnerabilities(self) -> bool:
        return len(self.vulnerable_packages) > 0

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "tool_version": self.tool_version,
            "scanned_package_count": self.scanned_package_count,
            "vulnerable_packages": [
                {
                    "name": p.name,
                    "version": p.version,
                    "vulnerabilities": [
                        {"id": v.id, "fix_versions": v.fix_versions, "aliases": v.aliases}
                        for v in p.vulnerabilities
                    ],
                }
                for p in self.vulnerable_packages
            ],
            "total_vulnerability_count": self.total_vulnerability_count,
        }


def _pip_audit_version() -> str:
    try:
        proc = subprocess.run(["pip-audit", "--version"], capture_output=True, text=True, timeout=30)
        return proc.stdout.strip() or proc.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def run_dependency_scan(requirement_path: Optional[str] = None) -> ScanResult:
    """Runs the real ``pip-audit`` CLI and parses its JSON output.

    Without ``requirement_path``, scans the currently active Python
    environment (i.e. whatever ``pip-audit`` is invoked from) -- the
    right default for scanning the environment that will actually run
    the bundle. Pass ``requirement_path`` to scan a specific pinned
    requirements file instead (e.g. the SDK's own pinned dependency set)
    without touching the caller's live environment.
    """
    cmd = ["pip-audit", "--format=json"]
    if requirement_path is not None:
        cmd += ["-r", requirement_path]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        raise PipAuditNotAvailableError(
            "pip-audit is not installed or not on PATH. Install with `pip install pip-audit` "
            "to enable dependency scanning -- this is a genuine 'not scanned' state, not a clean result."
        )
    except subprocess.TimeoutExpired:
        raise DependencyScanError("pip-audit timed out after 300s")

    # pip-audit's documented exit codes: 0 = no vulnerabilities, 1 = vulnerabilities found.
    # Any other code (e.g. 2 = bad invocation) means we can't trust stdout as valid JSON.
    if proc.returncode not in (0, 1):
        raise DependencyScanError(
            f"pip-audit exited with unexpected code {proc.returncode}: {proc.stderr.strip()[:500]}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise DependencyScanError(f"pip-audit produced non-JSON output: {e}")

    dependencies = payload.get("dependencies", [])
    vulnerable_packages = []
    for dep in dependencies:
        vulns = dep.get("vulns", [])
        if not vulns:
            continue
        vulnerable_packages.append(PackageVulnerabilities(
            name=dep["name"], version=dep["version"],
            vulnerabilities=[
                Vulnerability(id=v["id"], fix_versions=v.get("fix_versions", []), aliases=v.get("aliases", []))
                for v in vulns
            ],
        ))

    return ScanResult(
        tool="pip-audit", tool_version=_pip_audit_version(),
        scanned_package_count=len(dependencies),
        vulnerable_packages=vulnerable_packages,
    )
