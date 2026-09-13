"""Every dependency admits exactly one breaking line.

A range that spans two majors claims compatibility with a major nobody tested,
and the day a resolve picks it, the failure surfaces at run time instead of at
resolve time. For an application with a lockfile there is no downstream
consumer who needs a wide range, so the range should say what is tested:
floored at what is locked, capped below the next line that may break.

For 0.x, that line is the next *minor*: `<1` on a 0.x package is as loose as
no ceiling at all.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
LOCKED = {
    package["name"]: package["version"]
    for package in tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
}
SELF = PYPROJECT["project"]["name"]


def all_requirements() -> list[str]:
    found = list(PYPROJECT["project"]["dependencies"])
    for group in PYPROJECT["project"].get("optional-dependencies", {}).values():
        found += group
    for group in PYPROJECT.get("dependency-groups", {}).values():
        found += [item for item in group if isinstance(item, str)]
    return [item for item in found if Requirement(item).name != SELF]


REQUIREMENTS = all_requirements()


def bounds(requirement: Requirement) -> tuple[Version | None, Version | None]:
    lower = upper = None
    for spec in requirement.specifier:
        if spec.operator in (">=", ">", "=="):
            lower = Version(spec.version)
        if spec.operator in ("<", "<="):
            upper = Version(spec.version)
    return lower, upper


@pytest.mark.parametrize("raw", REQUIREMENTS)
def test_every_dependency_has_a_ceiling(raw: str) -> None:
    """`uv add` writes a bare `>=X` by default; this is what catches it."""
    _, upper = bounds(Requirement(raw))
    assert upper is not None, f"{raw} has no upper bound"


@pytest.mark.parametrize("raw", REQUIREMENTS)
def test_the_range_spans_one_breaking_line(raw: str) -> None:
    lower, upper = bounds(Requirement(raw))
    assert lower is not None and upper is not None, f"{raw} needs both bounds"

    if lower.major == 0:
        allowed = Version(f"0.{lower.minor + 1}")
        line = "minor (0.x)"
    else:
        allowed = Version(f"{lower.major + 1}")
        line = "major"
    assert upper <= allowed, f"{raw} spans more than one {line}: expected <{allowed}"


@pytest.mark.parametrize("raw", REQUIREMENTS)
def test_the_locked_version_sits_inside_the_declared_range(raw: str) -> None:
    """The range must describe what the suite actually runs against."""
    requirement = Requirement(raw)
    locked = LOCKED.get(requirement.name.lower())
    assert locked is not None, f"{requirement.name} is not in uv.lock"
    assert requirement.specifier.contains(locked, prereleases=True), (
        f"uv.lock pins {requirement.name} {locked}, outside the declared {requirement.specifier}"
    )


def test_uv_writes_bounded_ranges_on_add() -> None:
    assert PYPROJECT["tool"]["uv"].get("add-bounds") == "major"
