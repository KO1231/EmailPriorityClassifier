"""The import boundaries the deployment story depends on.

`actions/` and `dispatch/` exist so the apply worker can run without an LLM SDK
or a MIME parser. That is a property of the import graph, so it is checked as
one — a convention nobody can verify is a convention that erodes.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "epc"


def imports_of(package: str) -> set[str]:
    """Every `epc.*` module imported anywhere under `package`, transitively-blind."""
    found: set[str] = set()
    for path in (SRC / package).rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("epc"):
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names if alias.name.startswith("epc"))
    return found


@pytest.mark.parametrize("package", ["actions", "dispatch"])
def test_the_apply_side_never_imports_the_classifier(package: str) -> None:
    """The payoff: its deployment needs google-api-python-client and boto3, and
    nothing else."""
    offenders = {module for module in imports_of(package) if module.startswith("epc.classify")}
    assert not offenders, f"epc.{package} must not import {offenders}"


@pytest.mark.parametrize("package", ["actions", "dispatch"])
def test_the_apply_side_never_imports_the_mime_parser(package: str) -> None:
    assert "epc.gmail.mime" not in imports_of(package)


def test_the_classifier_never_imports_the_action_layer() -> None:
    """Classification decides what a thread is, never what to do about it."""
    offenders = {module for module in imports_of("classify") if module.startswith(("epc.actions", "epc.dispatch"))}
    assert not offenders, f"epc.classify must not import {offenders}"


def test_the_security_package_stands_alone() -> None:
    """Sanitisation must be usable from anywhere, including a future apply-side
    check, without dragging the rest of the package in."""
    allowed = {"epc.security", "epc.security.sanitize", "epc.security.detect"}
    assert imports_of("security") <= allowed
