"""`dry_run` overrides dispatch, wherever dispatch is pointed."""

from pathlib import Path

import pytest

from epc.settings import load_settings

BASE = 'labels: {p1: "a", p2: "b", p3: "c"}\n'


def settings_for(tmp_path: Path, extra: str):  # type: ignore[no-untyped-def]
    path = tmp_path / "config.yml"
    path.write_text(BASE + extra, encoding="utf-8")
    return load_settings(path)


def test_direct_dispatch_writes_by_default(tmp_path: Path) -> None:
    assert settings_for(tmp_path, "").resolve_sink() == "direct"


def test_dry_run_in_the_config_overrides_direct_dispatch(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "dry_run: true\ndispatch: {sink: direct}\n")
    assert settings.resolve_sink() == "jsonl"


def test_the_command_line_flag_overrides_the_config(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "dry_run: false\n")
    assert settings.resolve_sink(force_dry_run=True) == "jsonl"


def test_a_configured_jsonl_sink_needs_no_dry_run_flag(tmp_path: Path) -> None:
    assert settings_for(tmp_path, "dispatch: {sink: jsonl}\n").resolve_sink() == "jsonl"


@pytest.mark.parametrize("sink", ["direct", "jsonl"])
def test_dry_run_wins_whatever_dispatch_says(tmp_path: Path, sink: str) -> None:
    """A safety switch that only worked for some dispatch settings would be
    worse than none — this is the test that keeps it true as sinks are added."""
    settings = settings_for(tmp_path, f"dry_run: true\ndispatch: {{sink: {sink}}}\n")
    assert settings.resolve_sink() != "direct"


def test_the_state_section_stands_on_its_own(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, "state: {backend: local, file: /tmp/x.json}\n")
    assert settings.state.backend == "local"
    assert str(settings.state.file) == "/tmp/x.json"
