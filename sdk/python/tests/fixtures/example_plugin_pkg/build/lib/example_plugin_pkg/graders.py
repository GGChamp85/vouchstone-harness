"""A real third-party eval grader plugin, discovered via entry_points
under the ``vouchstone.eval_graders`` group (see this package's
pyproject.toml)."""

from vouchstone_sdk import GradeResult


def exact_match_grader(actual: str, expected):
    passed = actual == expected
    return GradeResult(score=1.0 if passed else 0.0, passed=passed, reason="exact_match plugin")
