"""CLI behaviour that does not touch the network."""

from pathlib import Path

import pytest

from epc import __version__
from epc.cli import EXIT_FATAL, EXIT_OK, build_parser, main

VALID_CONFIG = """
labels:
  p1: "#/P1"
  p2: "#/P2"
  p3: "#/P3"
gmail:
  extra_query: "newer_than:7d"
  max_threads: 250
"""


def test_version_is_populated() -> None:
    assert __version__
    assert __version__ != "0.0.0.dev0", "package should be installed, not run from a bare source tree"


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_OK
    assert "epc" in capsys.readouterr().out


def test_version_flag_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_config_validate_reports_what_it_resolved(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.yml"
    path.write_text(VALID_CONFIG, encoding="utf-8")

    assert main(["config", "validate", "--config", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "valid" in out
    assert "in:inbox newer_than:7d" in out
    assert "250" in out
    assert "#/P1" in out


def test_config_validate_rejects_an_invalid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "config.yml"
    path.write_text('labels: {p1: "x", p2: "x", p3: "y"}\n', encoding="utf-8")

    assert main(["config", "validate", "--config", str(path)]) == EXIT_FATAL
    assert "distinct" in capsys.readouterr().err


def test_a_missing_config_file_points_at_the_example(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "validate", "--config", str(tmp_path / "absent.yml")]) == EXIT_FATAL
    assert "config.yml.example" in capsys.readouterr().err


def test_the_shipped_example_config_is_valid(capsys: pytest.CaptureFixture[str]) -> None:
    """The example is the thing users copy; it must parse under the real schema."""
    example = Path(__file__).resolve().parents[2] / "config.yml.example"
    assert main(["config", "validate", "--config", str(example)]) == EXIT_OK
    assert "valid" in capsys.readouterr().out
