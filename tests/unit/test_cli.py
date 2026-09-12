"""Smoke tests: the package imports and the entrypoint runs.

These exist so the toolchain (uv, ruff, mypy, pytest, CI) is proven end to end
before any real code depends on it.
"""

import pytest

from epc import __version__
from epc.cli import build_parser, main


def test_version_is_populated() -> None:
    assert __version__
    assert __version__ != "0.0.0.dev0", "package should be installed, not run from a bare source tree"


def test_main_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "epc" in capsys.readouterr().out


def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
