"""Choosing where credentials, state and mutations live.

One place, so a deployment is described by configuration rather than by which
branch of the CLI happened to run. Every AWS import is deferred: `boto3` is in
the `aws` extra, and a local run should not need it installed.
"""

from epc.dispatch.sink import MutationSink
from epc.errors import ConfigError
from epc.gmail.auth import CredentialStore, LocalFileCredentialStore
from epc.settings import Settings
from epc.state import LocalFileStateStore, StateStore


def _missing(extra: str, what: str) -> ConfigError:
    return ConfigError(f"the {what} backend needs boto3; install the '{extra}' extra")


def build_credential_store(settings: Settings) -> CredentialStore:
    credentials = settings.credentials
    match credentials.backend:
        case "local":
            return LocalFileCredentialStore(credentials.token_file)
        case "ssm" | "secrets_manager":
            try:
                from epc.gmail.auth import SsmCredentialStore
            except ImportError as exc:  # pragma: no cover - import is unconditional
                raise _missing("aws", credentials.backend) from exc
            if not credentials.parameter_name:  # pragma: no cover - settings validate this
                raise ConfigError("credentials.parameter_name is required")
            return SsmCredentialStore(credentials.parameter_name, region=settings.aws_region)
        case "service_account":
            raise ConfigError(
                "the service_account credential backend is not implemented yet; "
                "it needs a Workspace domain to delegate from"
            )


def build_state_store(settings: Settings) -> StateStore:
    state = settings.state
    match state.backend:
        case "local":
            return LocalFileStateStore(state.file)
        case "ssm":
            from epc.state import SsmStateStore

            if not state.parameter_name:  # pragma: no cover - settings validate this
                raise ConfigError("state.parameter_name is required")
            return SsmStateStore(state.parameter_name, region=settings.aws_region)
        case "s3":
            from epc.state import S3StateStore

            if not state.bucket:  # pragma: no cover - settings validate this
                raise ConfigError("state.bucket is required")
            return S3StateStore(state.bucket, state.key, region=settings.aws_region)


def build_sink(settings: Settings, *, gmail_client: object, force_dry_run: bool = False) -> MutationSink:
    """The sink `settings` asks for, with `dry_run` taking precedence."""
    from epc.dispatch.applier import MutationApplier
    from epc.dispatch.sink import DirectSink, JsonlSink

    match settings.resolve_sink(force_dry_run=force_dry_run):
        case "jsonl":
            return JsonlSink(settings.dispatch.jsonl_path)
        case "sqs":
            import boto3

            from epc.dispatch.sqs import SqsSink

            if not settings.dispatch.queue_url:  # pragma: no cover - settings validate this
                raise ConfigError("dispatch.queue_url is required")
            return SqsSink(
                boto3.client("sqs", region_name=settings.aws_region),
                settings.dispatch.queue_url,
            )
        case _:
            from epc.gmail.client import GmailClient

            assert isinstance(gmail_client, GmailClient)
            return DirectSink(MutationApplier(gmail_client), batch_size=settings.dispatch.batch_size)
