"""Configuration loading: precedence, validation, and the failures it must catch."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from epc.priority import Priority
from epc.settings import Settings, load_settings

MINIMAL = """
labels:
  p1: "#/P1"
  p2: "#/P2"
  p3: "#/P3"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL, encoding="utf-8")
    return path


def test_defaults_fill_in_every_omitted_section(config_file: Path) -> None:
    settings = load_settings(config_file)
    assert settings.gmail.max_threads == 1500
    assert settings.llm.backend == "openai"
    assert settings.llm.budget.thread_tokens == 4000
    assert settings.security.on_suspected_injection == "downgrade_and_flag"
    assert settings.run.dry_run is False


def test_file_values_override_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\ngmail: {max_threads: 42}\n", encoding="utf-8")
    assert load_settings(path).gmail.max_threads == 42


def test_a_partially_specified_section_keeps_its_other_defaults(tmp_path: Path) -> None:
    """Setting one key of `llm` must not blank out the rest of it."""
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\nllm: {concurrency: 3}\n", encoding="utf-8")
    settings = load_settings(path)
    assert settings.llm.concurrency == 3
    assert settings.llm.requests_per_min == 120


def test_environment_beats_the_file(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EPC__GMAIL__MAX_THREADS", "7")
    assert load_settings(config_file).gmail.max_threads == 7


def test_explicit_overrides_beat_the_environment(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The precedence the CLI relies on: flags win over everything."""
    monkeypatch.setenv("EPC__GMAIL__MAX_THREADS", "7")
    assert load_settings(config_file, gmail={"max_threads": 3}).gmail.max_threads == 3


def test_labels_are_exposed_keyed_by_priority(config_file: Path) -> None:
    assert load_settings(config_file).labels.by_priority == {
        Priority.P1: "#/P1",
        Priority.P2: "#/P2",
        Priority.P3: "#/P3",
    }


def test_extra_query_is_appended_to_the_search(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + '\ngmail: {extra_query: "newer_than:7d"}\n', encoding="utf-8")
    assert load_settings(path).search_query == "in:inbox newer_than:7d"


def test_search_query_is_unchanged_without_an_extra_query(config_file: Path) -> None:
    assert load_settings(config_file).search_query == "in:inbox"


# --------------------------------------------------------------------------
# Failures that must be loud
# --------------------------------------------------------------------------


def test_a_missing_labels_section_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("gmail: {max_threads: 10}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="labels"):
        load_settings(path)


def test_duplicate_label_names_are_rejected(tmp_path: Path) -> None:
    """Two priorities sharing a label is the silent mislabelling bug, caught at startup."""
    path = tmp_path / "config.yml"
    path.write_text('labels: {p1: "#/X", p2: "#/X", p3: "#/P3"}\n', encoding="utf-8")
    with pytest.raises(ValidationError, match="distinct"):
        load_settings(path)


def test_an_unknown_key_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    """A typo that silently does nothing is what this schema exists to prevent."""
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\ngmail: {max_thread: 10}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="max_thread"):
        load_settings(path)


def test_an_unknown_top_level_section_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\nlabelID: {P1: Label_1}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="labelID"):
        load_settings(path)


@pytest.mark.parametrize(
    "section",
    ["gmail: {max_threads: 0}", "llm: {concurrency: 0}", "llm: {requests_per_min: -1}"],
)
def test_non_positive_limits_are_rejected(tmp_path: Path, section: str) -> None:
    path = tmp_path / "config.yml"
    path.write_text(f"{MINIMAL}\n{section}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_an_invalid_backend_name_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\nllm: {backend: anthropic-direct}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="backend"):
        load_settings(path)


def test_remote_credential_backends_require_a_location(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text(MINIMAL + "\ncredentials: {backend: ssm}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="parameter_name"):
        load_settings(path)


def test_the_local_credential_backend_needs_no_location(config_file: Path) -> None:
    assert load_settings(config_file).credentials.backend == "local"


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


def test_loading_one_file_does_not_affect_the_next(tmp_path: Path) -> None:
    """`load_settings` scopes the YAML path per call; leaking it between calls
    would make test order significant and surprise anyone passing --config."""
    first = tmp_path / "a.yml"
    first.write_text(MINIMAL + "\ngmail: {max_threads: 11}\n", encoding="utf-8")
    second = tmp_path / "b.yml"
    second.write_text(MINIMAL + "\ngmail: {max_threads: 22}\n", encoding="utf-8")

    assert load_settings(first).gmail.max_threads == 11
    assert load_settings(second).gmail.max_threads == 22
    assert load_settings(first).gmail.max_threads == 11
    assert Settings.model_config["yaml_file"] == "config.yml"


def test_sections_are_immutable(config_file: Path) -> None:
    """Settings are read once at startup; nothing may mutate them mid-run."""
    settings = load_settings(config_file)
    with pytest.raises(ValidationError):
        settings.gmail.max_threads = 1
