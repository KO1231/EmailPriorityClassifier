"""Configuration: one file, one schema, validated before anything runs.

Two things the previous implementation got wrong are fixed by the shape of this
module rather than by any particular check:

* **There is no `labelID`.** The old config asked for the internal `Label_...`
  IDs *and* the display names of the same three labels, hand-maintained and
  never cross-checked. A mismatch meant threads were classified as one priority
  and labelled as another, silently, forever. Only names are configured now;
  IDs are resolved from the mailbox at startup by :mod:`epc.gmail.labels`.
* **Secrets never appear here.** API keys and OAuth tokens come from the
  environment or from a credential backend. The config file holds policy, so it
  can be read, diffed and reviewed without any special handling.

Precedence, highest first: constructor arguments (the CLI) → environment →
`config.yml` → defaults. Environment variables are `EPC__`-prefixed with `__`
between levels, so `EPC__GMAIL__MAX_THREADS=50` overrides `gmail.max_threads`.

Unknown keys are rejected rather than ignored: a typo in a config file that
silently does nothing is the failure mode this whole module exists to avoid.
"""

from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from epc.actions.rules import ActionRule
from epc.gmail.models import NON_PRIMARY_CATEGORIES
from epc.priority import Priority

DEFAULT_CONFIG_FILENAME = "config.yml"

StateBackend = Literal["local", "ssm", "s3"]
CredentialsBackend = Literal["local", "ssm", "secrets_manager", "service_account"]
LlmBackend = Literal["openai", "bedrock", "local"]
SinkKind = Literal["direct", "jsonl", "sqs"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
Verbosity = Literal["low", "medium", "high"]
InjectionResponse = Literal["ignore", "flag", "downgrade_and_flag"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabelSettings(_Section):
    """Display names of the three Gmail labels this tool writes.

    Names only. The internal IDs are looked up at startup from the mailbox.
    """

    p1: str
    p2: str
    p3: str
    create_if_missing: bool = False

    @model_validator(mode="after")
    def _names_must_be_distinct(self) -> Self:
        names = [self.p1, self.p2, self.p3]
        if len(set(names)) != len(names):
            raise ValueError(f"the three priority labels must be distinct, got {names}")
        return self

    @property
    def by_priority(self) -> dict[Priority, str]:
        return {Priority.P1: self.p1, Priority.P2: self.p2, Priority.P3: self.p3}


class GmailSettings(_Section):
    """What to fetch."""

    query: str = "in:inbox"
    # Appended to `query`. Mind what a time limit such as `newer_than:14d` costs:
    # every run already excludes labelled threads, so it saves little, and it
    # stops anything older being re-classified when its label is removed.
    extra_query: str = ""
    max_threads: int = Field(default=1500, gt=0)


class BudgetSettings(_Section):
    """How much of a thread the model is allowed to see.

    These numbers are tuned against cost. Change how truncation is *applied*
    freely; changing the budgets at the same time makes the result
    unattributable.
    """

    thread_tokens: int = Field(default=4000, gt=0)
    message_chars: int = Field(default=2000, gt=0)


class LlmSettings(_Section):
    backend: LlmBackend = "openai"

    # Required to classify, but not to validate a config or resolve labels, so
    # it is checked when the classifier is built rather than here. That is still
    # startup — just the startup of the command that needs it.
    model: str | None = None

    # backend: local — where the OpenAI-compatible server is listening. Not a
    # hardcoded localhost, so the same image can reach a server outside its
    # own container.
    base_url: str | None = None

    # backend: bedrock — falls back to the usual AWS region resolution.
    region: str | None = None

    # OpenAI-compatible backends only; ignored elsewhere. Classification is a
    # short, well-specified judgement, so the low end is usually right — and on
    # a reasoning model the thinking is billed as output.
    reasoning_effort: ReasoningEffort | None = None
    verbosity: Verbosity | None = None

    # Reasoning tokens count against this too, so it is not simply "how long is
    # the answer". Four short fields need very little; the headroom is for the
    # thinking in front of them.
    max_output_tokens: int = Field(default=2048, gt=0)

    concurrency: int = Field(default=15, gt=0)
    requests_per_min: int = Field(default=120, gt=0)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)

    @model_validator(mode="after")
    def _local_backend_needs_a_base_url(self) -> Self:
        if self.backend == "local" and not self.base_url:
            raise ValueError("llm.base_url is required when llm.backend is 'local'")
        return self


class ActionsSettings(_Section):
    """What happens to a thread once its priority is known.

    Rules live here rather than in the prompt because this is the half of the
    decision that must not be negotiable. A mail that talks its way to P1 still
    cannot talk its way into being starred.
    """

    rules: list[ActionRule] = Field(default_factory=list)

    # Which Gmail tabs `move_to_primary` will leave. Removing a non-primary
    # category is what moves a thread; Gmail treats their absence as Primary.
    move_targets: list[str] = Field(
        default_factory=lambda: sorted(NON_PRIMARY_CATEGORIES),
    )

    # Verbs that take a thread out of the inbox. Off unless asked for.
    allow_destructive: bool = False

    @model_validator(mode="after")
    def _move_targets_must_be_category_labels(self) -> Self:
        unknown = sorted(set(self.move_targets) - NON_PRIMARY_CATEGORIES)
        if unknown:
            raise ValueError(f"actions.move_targets may only name non-primary Gmail categories; got {unknown}")
        return self


class DispatchSettings(_Section):
    """Where planned mutations go."""

    sink: SinkKind = "direct"
    jsonl_path: Path = Path("log/mutations.jsonl")

    # sink: sqs — a FIFO queue. Ordering per thread is what protects
    # correctness once a rule can remove a label; the dedup window is a cost
    # optimisation on top of idempotency that already holds without it.
    queue_url: str | None = None
    # Threads buffered before a write. `batchModify` takes 1000 message IDs per
    # call, so buffering is what turns 1500 round trips into two.
    batch_size: int = Field(default=1000, gt=0, le=1000)

    @model_validator(mode="after")
    def _sqs_needs_a_queue(self) -> Self:
        if self.sink == "sqs" and not self.queue_url:
            raise ValueError("dispatch.queue_url is required when dispatch.sink is 'sqs'")
        return self


class SecuritySettings(_Section):
    # What to do with a thread whose content trips the injection heuristics.
    # `ignore`: nothing. `flag`: record it on the mutation and in the history,
    # and let rules match on it, but take no action away. `downgrade_and_flag`:
    # record it and withhold the high-privilege actions (starring, marking
    # important, moving to Primary) from that thread.
    on_suspected_injection: InjectionResponse = "downgrade_and_flag"


class CredentialsSettings(_Section):
    """Where the Gmail OAuth credentials live. Never the credentials themselves."""

    backend: CredentialsBackend = "local"

    # backend: local
    token_file: Path = Path("secrets/token.json")
    client_secrets_file: Path = Path("secrets/client_secrets.json")

    # backend: ssm | secrets_manager — the parameter path or secret name holding
    # the durable half of the credentials.
    parameter_name: str | None = None

    @model_validator(mode="after")
    def _remote_backends_need_a_location(self) -> Self:
        if self.backend in ("ssm", "secrets_manager") and not self.parameter_name:
            raise ValueError(f"credentials.parameter_name is required when backend is {self.backend!r}")
        return self


class ObservabilitySettings(_Section):
    """Logs, and the record of what was decided."""

    log_level: str = "INFO"
    # JSON when something is going to parse it; readable text otherwise.
    log_json: bool = False
    log_file: Path | None = None

    # Where classification records go. Unset means none are kept: a durable
    # record of decisions about your mail is something to opt into, not
    # something to discover later.
    history_dir: Path | None = None


class StateSettings(_Section):
    """Where run state — the record of threads that keep failing — is kept.

    Holds thread IDs, never content, and no secret.
    """

    backend: StateBackend = "local"
    # backend: local
    file: Path = Path(".state/run.json")
    # backend: ssm — the parameter name. A plain String: thread IDs are not
    # secrets, and encrypting them would add a kms:Decrypt for nothing.
    parameter_name: str | None = None
    # backend: s3
    bucket: str | None = None
    key: str = "epc/state.json"

    @model_validator(mode="after")
    def _remote_backends_need_a_location(self) -> Self:
        if self.backend == "ssm" and not self.parameter_name:
            raise ValueError("state.parameter_name is required when state.backend is 'ssm'")
        if self.backend == "s3" and not self.bucket:
            raise ValueError("state.bucket is required when state.backend is 's3'")
        return self


class Settings(BaseSettings):
    """The whole configuration.

    Sections arrive with the code that consumes them; `actions` and `dispatch`
    land with the action planner and the mutation sinks. Adding them earlier
    would mean designing a config schema for code that does not exist yet, and
    a config schema is the one thing that is expensive to change afterwards.
    """

    model_config = SettingsConfigDict(
        env_prefix="EPC__",
        env_nested_delimiter="__",
        extra="forbid",
        yaml_file=DEFAULT_CONFIG_FILENAME,
        nested_model_default_partial_update=True,
    )

    labels: LabelSettings
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    credentials: CredentialsSettings = Field(default_factory=CredentialsSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    actions: ActionsSettings = Field(default_factory=ActionsSettings)
    dispatch: DispatchSettings = Field(default_factory=DispatchSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    state: StateSettings = Field(default_factory=StateSettings)

    # Region for every AWS backend that does not name its own. Falls back to
    # the usual boto3 resolution when unset.
    aws_region: str | None = None

    # Top level, and not a member of any section, because it overrides one.
    # Whatever `dispatch` is configured to do — apply now, or hand off to a
    # queue — this says nothing is written to Gmail. A safety switch that only
    # worked for some dispatch settings would be worse than none.
    dry_run: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - fixed hook signature
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - fixed hook signature
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order is precedence: the first source to supply a key wins.

        `.env` is not a settings source. It carries secrets, which belong to the
        credential backend, not to configuration.
        """
        return (init_settings, env_settings, YamlConfigSettingsSource(settings_cls))

    def resolve_sink(self, *, force_dry_run: bool = False) -> SinkKind:
        """Where mutations actually go, with `dry_run` taking precedence.

        Keeping the precedence here rather than at the call site is the point:
        every future entry point gets it, and a new sink cannot accidentally
        become one that writes during a dry run.
        """
        if force_dry_run or self.dry_run:
            return "jsonl"
        return self.dispatch.sink

    @property
    def search_query(self) -> str:
        """`gmail.query` with `gmail.extra_query` appended, if any."""
        extra = self.gmail.extra_query.strip()
        return f"{self.gmail.query} {extra}".strip() if extra else self.gmail.query


def load_settings(config_path: Path | None = None, **overrides: Any) -> Settings:
    """Build :class:`Settings` from `config_path`, the environment and `overrides`.

    `overrides` are the highest-precedence source and exist for CLI flags.

    A per-call subclass is what lets the YAML path vary: pydantic-settings takes
    it from the class config, and mutating that on the shared class would leak
    between calls and between tests.
    """
    if config_path is None:
        return Settings(**overrides)

    class _FileScopedSettings(Settings):
        model_config = SettingsConfigDict(**{**Settings.model_config, "yaml_file": config_path})

    return _FileScopedSettings(**overrides)
