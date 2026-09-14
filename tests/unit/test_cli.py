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


# --------------------------------------------------------------------------
# epc failures
# --------------------------------------------------------------------------


def config_with_state(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    state = tmp_path / "state.json"
    path.write_text(VALID_CONFIG + f"state:\n  backend: local\n  file: {state}\n", encoding="utf-8")
    return path


def seed_failures(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from epc.state import LocalFileStateStore, RunState

    now = datetime(2026, 9, 1, tzinfo=UTC)
    state = RunState().after_run(failed={"t1": "1", "t2": "1"}, succeeded={"ok"}, now=now)
    state = state.after_run(failed={"t1": "1"}, succeeded=set(), now=now)
    LocalFileStateStore(tmp_path / "state.json").save(state)


def test_failures_list_shows_what_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = config_with_state(tmp_path)
    seed_failures(tmp_path)

    assert main(["failures", "list", "--config", str(config)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "t1" in out and "skipped" in out
    assert "t2" in out and "retrying (1/2)" in out


def test_failures_list_with_nothing_recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["failures", "list", "--config", str(config_with_state(tmp_path))]) == EXIT_OK
    assert "No recorded failures" in capsys.readouterr().out


def test_failures_clear_forgets_the_named_threads(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from epc.state import LocalFileStateStore

    config = config_with_state(tmp_path)
    seed_failures(tmp_path)

    assert main(["failures", "clear", "t1", "--config", str(config)]) == EXIT_OK
    assert set(LocalFileStateStore(tmp_path / "state.json").load().failures) == {"t2"}
    assert "Forgot 1" in capsys.readouterr().out


def test_failures_clear_with_no_ids_forgets_everything(tmp_path: Path) -> None:
    from epc.state import LocalFileStateStore

    config = config_with_state(tmp_path)
    seed_failures(tmp_path)

    assert main(["failures", "clear", "--config", str(config)]) == EXIT_OK
    assert LocalFileStateStore(tmp_path / "state.json").load().failures == {}


# --------------------------------------------------------------------------
# Configuration delivered as an environment variable
# --------------------------------------------------------------------------


def test_the_config_can_arrive_as_an_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """How ECS gets it: no file in the image, the YAML injected from Parameter
    Store. The task used to exit with "configuration file not found" every run."""
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    assert main(["config", "validate", "--config", str(tmp_path / "absent.yml")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "EPC_CONFIG_YAML: valid" in out
    assert "#/P1" in out


def test_the_environment_variable_wins_over_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.yml"
    path.write_text(VALID_CONFIG.replace("#/P1", "#/from-file"), encoding="utf-8")
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)

    assert main(["config", "validate", "--config", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "#/P1" in out
    assert "#/from-file" not in out


def test_individual_variables_still_override_the_delivered_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    monkeypatch.setenv("EPC__GMAIL__MAX_THREADS", "7")
    assert main(["config", "validate", "--config", str(tmp_path / "absent.yml")]) == EXIT_OK
    assert "max threads         7" in capsys.readouterr().out


@pytest.mark.parametrize("text", ["labels: [unclosed", "- a list\n- not a mapping"])
def test_a_malformed_delivered_config_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    monkeypatch.setenv("EPC_CONFIG_YAML", text)
    assert main(["config", "validate", "--config", str(tmp_path / "absent.yml")]) == EXIT_FATAL
    assert "EPC_CONFIG_YAML" in capsys.readouterr().err


def test_policy_can_arrive_as_an_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    monkeypatch.setenv("EPC_POLICY_YAML", "guidance:\n  - Mail from the landlord is always P1.\n")

    code = main(["prompt", "render", "--config", str(tmp_path / "absent.yml"), "--prompts", str(repo / "prompts")])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "# policy: EPC_POLICY_YAML" in out
    assert "Mail from the landlord is always P1." in out
