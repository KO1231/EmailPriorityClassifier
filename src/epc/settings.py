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

from epc.priority import Priority

DEFAULT_CONFIG_FILENAME = "config.yml"

StateBackend = Literal["local", "ssm", "s3"]
CredentialsBackend = Literal["local", "ssm", "secrets_manager", "service_account"]
LlmBackend = Literal["openai", "bedrock", "local"]
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
    # Appended to `query`. `newer_than:14d -in:chats` is the single largest cost
    # lever available: it stops a run re-reading the whole inbox every time.
    extra_query: str = ""
    max_threads: int = Field(default=1500, gt=0)
    # Use the History API from a stored checkpoint when one exists, falling back
    # to a full scan when Gmail has expired it.
    incremental: bool = True


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

    concurrency: int = Field(default=15, gt=0)
    requests_per_min: int = Field(default=120, gt=0)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)

    @model_validator(mode="after")
    def _local_backend_needs_a_base_url(self) -> Self:
        if self.backend == "local" and not self.base_url:
            raise ValueError("llm.base_url is required when llm.backend is 'local'")
        return self


class SecuritySettings(_Section):
    # What to do with a thread whose content trips the injection heuristics.
    # `downgrade_and_flag` records the signal and withholds the high-privilege
    # actions (starring, moving to Primary) from that thread.
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


class RunSettings(_Section):
    dry_run: bool = False
    state_backend: StateBackend = "local"


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
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    run: RunSettings = Field(default_factory=RunSettings)

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
