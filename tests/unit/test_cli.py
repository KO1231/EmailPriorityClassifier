"""CLI behaviour that does not touch the network."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from epc import __version__
from epc.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL, build_parser, main

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


def seed_failures(tmp_path: Path, *, classifier: str | None = None, now: datetime | None = None) -> None:
    from epc.state import LocalFileStateStore, RunState

    now = now or datetime.now(UTC).replace(microsecond=0)
    state = RunState(classifier=classifier).after_run(failed={"t1": "1", "t2": "1"}, succeeded={"ok"}, now=now)
    state = state.after_run(failed={"t1": "1"}, succeeded=set(), now=now)
    LocalFileStateStore(tmp_path / "state.json").save(state)


REPO = Path(__file__).resolve().parents[2]


def current_classifier(config: Path) -> str:
    from epc.classify.prompt import PromptRenderer
    from epc.cli import _classifier_version
    from epc.settings import load_settings

    return _classifier_version(load_settings(config), PromptRenderer.load(REPO / "prompts", REPO / "absent.yml"))


def failures_list(config: Path) -> list[str]:
    return ["failures", "list", "--config", str(config), "--prompts", str(REPO / "prompts"), "--policy", "absent.yml"]


def test_failures_list_shows_what_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = config_with_state(tmp_path)
    seed_failures(tmp_path, classifier=current_classifier(config))

    assert main(failures_list(config)) == EXIT_OK
    out = capsys.readouterr().out
    assert "t1" in out and "skipped" in out
    assert "t2" in out and "retrying (1/2)" in out


def test_failures_list_with_nothing_recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(failures_list(config_with_state(tmp_path))) == EXIT_OK
    assert "No recorded failures" in capsys.readouterr().out


def test_failures_list_says_when_a_change_will_retry_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It used to list these as skipped while the next run retried them all."""
    config = config_with_state(tmp_path)
    seed_failures(tmp_path, classifier="openai/an-older-model/000000000000")

    assert main(failures_list(config)) == EXIT_OK
    out = capsys.readouterr().out
    assert "retried next run" in out
    assert "skipped" not in out


def test_failures_list_leaves_out_what_has_expired(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = config_with_state(tmp_path)
    seed_failures(tmp_path, classifier=current_classifier(config), now=datetime(2020, 1, 1, tzinfo=UTC))

    assert main(failures_list(config)) == EXIT_OK
    assert "past the 30-day limit" in capsys.readouterr().out


@pytest.mark.parametrize(
    "change",
    [{"max_output_tokens": 256}, {"reasoning_effort": "high"}, {"budget": {"thread_tokens": 1000}}],
    ids=["output-limit", "reasoning", "budget"],
)
def test_a_change_to_how_requests_are_made_changes_the_classifier_version(
    tmp_path: Path, change: dict[str, object]
) -> None:
    """Each of these can cause a refusal or a cut answer as surely as the model."""
    from epc.classify.prompt import PromptRenderer
    from epc.cli import _classifier_version
    from epc.settings import load_settings

    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    renderer = PromptRenderer.load(REPO / "prompts", REPO / "absent.yml")
    before = _classifier_version(load_settings(config), renderer)
    after = _classifier_version(load_settings(config, llm=change), renderer)
    assert before != after


def test_pacing_does_not_change_the_classifier_version(tmp_path: Path) -> None:
    from epc.classify.prompt import PromptRenderer
    from epc.cli import _classifier_version
    from epc.settings import load_settings

    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    renderer = PromptRenderer.load(REPO / "prompts", REPO / "absent.yml")
    assert _classifier_version(load_settings(config), renderer) == _classifier_version(
        load_settings(config, llm={"concurrency": 1, "requests_per_min": 5}), renderer
    )


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


@pytest.fixture
def no_config_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with no config.yml, as in the image on ECS."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_config_can_arrive_as_an_environment_variable(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """How ECS gets it: no file in the image, the YAML injected from Parameter
    Store. The task used to exit with "configuration file not found" every run."""
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    assert main(["config", "validate"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "EPC_CONFIG_YAML: valid" in out
    assert "#/P1" in out


def test_the_variable_wins_over_the_default_file(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stray config.yml in the working directory must not replace the one the
    deployment delivered."""
    (no_config_here / "config.yml").write_text(VALID_CONFIG.replace("#/P1", "#/stray"), encoding="utf-8")
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)

    assert main(["config", "validate"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "#/P1" in out
    assert "#/stray" not in out


def test_a_file_named_on_the_command_line_wins_over_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The variable used to win even here — so one picked up from `--env-file`
    silently ran a different configuration from the one asked for."""
    path = tmp_path / "chosen.yml"
    path.write_text(VALID_CONFIG.replace("#/P1", "#/chosen"), encoding="utf-8")
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)

    assert main(["config", "validate", "--config", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"{path}: valid" in out
    assert "#/chosen" in out


def test_individual_variables_still_override_the_delivered_config(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    monkeypatch.setenv("EPC__GMAIL__MAX_THREADS", "7")
    assert main(["config", "validate"]) == EXIT_OK
    assert "max threads         7" in capsys.readouterr().out


@pytest.mark.parametrize("text", ["labels: [unclosed", "- a list\n- not a mapping"])
def test_a_malformed_delivered_config_is_named(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    monkeypatch.setenv("EPC_CONFIG_YAML", text)
    assert main(["config", "validate"]) == EXIT_FATAL
    err = capsys.readouterr().err
    assert "EPC_CONFIG_YAML" in err
    assert "not found" not in err


def test_policy_can_arrive_as_an_environment_variable(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    monkeypatch.setenv("EPC_POLICY_YAML", "guidance:\n  - Mail from the landlord is always P1.\n")

    assert main(["prompt", "render", "--prompts", str(REPO / "prompts")]) == EXIT_OK
    out = capsys.readouterr().out
    assert "# policy: EPC_POLICY_YAML" in out
    assert "Mail from the landlord is always P1." in out


def test_a_policy_named_on_the_command_line_wins_over_the_variable(
    no_config_here: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = no_config_here / "mine.yml"
    policy.write_text("guidance:\n  - From the file.\n", encoding="utf-8")
    monkeypatch.setenv("EPC_CONFIG_YAML", VALID_CONFIG)
    monkeypatch.setenv("EPC_POLICY_YAML", "guidance:\n  - From the variable.\n")

    assert main(["prompt", "render", "--prompts", str(REPO / "prompts"), "--policy", str(policy)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "From the file." in out
    assert "From the variable." not in out


# --------------------------------------------------------------------------
# Commands that talk to Gmail, against a fake
# --------------------------------------------------------------------------


class FakeGmailClient:
    def __init__(self, labels: dict[str, str], thread_labels: dict[str, set[str]] | None = None) -> None:
        self.labels = dict(labels)  # name -> id
        self.thread_labels = thread_labels or {}
        self.created: list[str] = []
        self.writes: list[tuple[list[str], list[str]]] = []

    def list_labels(self) -> list[object]:
        from epc.gmail.labels import GmailLabel

        return [GmailLabel(id=label_id, name=name) for name, label_id in self.labels.items()]

    def create_label(self, name: str) -> object:
        self.created.append(name)
        self.labels[name] = f"Label_new_{len(self.created)}"
        return None

    def thread_label_ids(self, thread_id: str) -> set[str]:
        from epc.errors import GmailError

        if thread_id not in self.thread_labels:
            raise GmailError("thread not found")
        return self.thread_labels[thread_id]

    def batch_modify(self, message_ids: object, *, add_label_ids: object = (), remove_label_ids: object = ()) -> None:
        self.writes.append((list(message_ids), list(add_label_ids)))  # type: ignore[call-overload]


PRIORITY_LABELS = {"#/P1": "Label_1", "#/P2": "Label_2", "#/P3": "Label_3"}


def use_fake_client(monkeypatch: pytest.MonkeyPatch, client: FakeGmailClient) -> None:
    monkeypatch.setattr("epc.cli._gmail_client", lambda _settings: client)


def test_labels_create_makes_only_the_missing_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    client = FakeGmailClient({"#/P1": "Label_1"})
    use_fake_client(monkeypatch, client)

    assert main(["labels", "--create", "--config", str(config)]) == EXIT_OK
    assert client.created == ["#/P2", "#/P3"]
    assert "Created label '#/P2'" in capsys.readouterr().out


def test_labels_without_create_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    client = FakeGmailClient({"#/P1": "Label_1"})
    use_fake_client(monkeypatch, client)

    assert main(["labels", "--config", str(config)]) == EXIT_FATAL
    assert client.created == []


def write_plan(path: Path, *mutations: object) -> None:
    path.write_text("".join(m.model_dump_json() + "\n" for m in mutations), encoding="utf-8")  # type: ignore[attr-defined]


def planned(thread_id: str, label: str, *, origin: str = "classified") -> object:
    from datetime import UTC, datetime

    from epc.actions.model import ThreadMutation
    from epc.priority import Priority

    return ThreadMutation(
        thread_id=thread_id,
        message_ids=[f"{thread_id}-m1"],
        add_label_ids=[label],
        priority=Priority.P1,
        classified_at=datetime(2026, 9, 14, tzinfo=UTC),
        origin=origin,  # type: ignore[arg-type]
    )


def test_apply_skips_threads_labelled_since_the_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hours can pass between a dry run and its replay. A label a person set in
    that time must not be overwritten by the replay."""
    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    plan = tmp_path / "plan.jsonl"
    write_plan(plan, planned("untouched", "Label_1"), planned("relabelled", "Label_1"))
    client = FakeGmailClient(PRIORITY_LABELS, {"untouched": {"INBOX"}, "relabelled": {"INBOX", "Label_3"}})
    use_fake_client(monkeypatch, client)

    assert main(["apply", str(plan), "--config", str(config)]) == EXIT_OK
    assert client.writes == [(["untouched-m1"], ["Label_1"])]
    assert "skipped 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("labels_now", "applied"),
    [({"Label_2"}, True), (set(), False), ({"Label_1"}, False)],
    ids=["still-carried-label", "label-removed", "label-changed"],
)
def test_apply_carries_a_label_only_if_the_thread_still_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, labels_now: set[str], applied: bool
) -> None:
    """A removed label is a request to re-classify; carrying it back would undo that."""
    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    plan = tmp_path / "plan.jsonl"
    write_plan(plan, planned("t1", "Label_2", origin="carried_forward"))
    client = FakeGmailClient(PRIORITY_LABELS, {"t1": {"INBOX", *labels_now}})
    use_fake_client(monkeypatch, client)

    assert main(["apply", str(plan), "--config", str(config)]) == EXIT_OK
    assert bool(client.writes) is applied


def test_apply_does_not_write_what_it_could_not_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yml"
    config.write_text(VALID_CONFIG, encoding="utf-8")
    plan = tmp_path / "plan.jsonl"
    write_plan(plan, planned("gone", "Label_1"))
    client = FakeGmailClient(PRIORITY_LABELS, {})
    use_fake_client(monkeypatch, client)

    assert main(["apply", str(plan), "--config", str(config)]) == EXIT_PARTIAL
    assert client.writes == []
    assert "labels could not be checked" in capsys.readouterr().err
