"""Command-line entrypoint.

Subcommands land alongside the code they drive. Present so far:

``epc config validate``
    Parse and validate the configuration, then print what was resolved. No
    network access — safe to run anywhere, including in CI.
``epc labels``
    Resolve the configured label names against the mailbox. This is the check
    that used to fail only after a full run had already been paid for.
``epc login``
    Run the OAuth flow and store the credentials.

``run`` and ``apply`` arrive with the pipeline and the mutation sinks.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from epc import __version__
from epc.errors import EpcError
from epc.settings import DEFAULT_CONFIG_FILENAME, Settings, load_settings

EXIT_OK = 0
EXIT_FATAL = 1


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(DEFAULT_CONFIG_FILENAME),
        metavar="PATH",
        help=f"configuration file (default: {DEFAULT_CONFIG_FILENAME})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epc",
        description="Classify Gmail threads by how urgently they need attention.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    config = subcommands.add_parser("config", help="inspect the configuration")
    config_actions = config.add_subparsers(dest="config_command", required=True)
    validate = config_actions.add_parser("validate", help="validate the configuration and print it")
    _add_config_option(validate)

    labels = subcommands.add_parser("labels", help="resolve the configured labels against the mailbox")
    _add_config_option(labels)

    login = subcommands.add_parser("login", help="authorise this tool against a Gmail account")
    _add_config_option(login)

    return parser


# Keys that only ever appeared in the previous config schema.
_LEGACY_KEYS = ("labelID", "priorityLabels", "maxThreads", "requestsPerMin")


def _load(path: Path) -> Settings:
    if not path.is_file():
        raise EpcError(f"configuration file not found: {path}\nCopy {DEFAULT_CONFIG_FILENAME}.example and fill it in.")

    # The legacy implementation still reads its own config from the same default
    # path while the rewrite is in progress. Say so plainly instead of emitting
    # a validation error about a dozen unknown keys.
    text = path.read_text(encoding="utf-8")
    if any(f"{key}:" in text for key in _LEGACY_KEYS):
        raise EpcError(
            f"{path} is in the legacy config schema, which this command does not read.\n"
            f"The rewrite uses the schema in {DEFAULT_CONFIG_FILENAME}.example — notably there is no\n"
            "labelID section, because label IDs are now resolved from the mailbox by name.\n"
            "Write the new config elsewhere and pass --config until the legacy implementation is removed."
        )

    return load_settings(path)


def cmd_config_validate(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    print(f"{args.config}: valid\n")
    print(f"  search query        {settings.search_query}")
    print(f"  max threads         {settings.gmail.max_threads}")
    print(f"  incremental         {settings.gmail.incremental}")
    print(f"  llm backend         {settings.llm.backend}")
    print(f"  concurrency         {settings.llm.concurrency} at {settings.llm.requests_per_min}/min")
    print(f"  budget              {settings.llm.budget.thread_tokens} tokens/thread")
    print(f"  credentials         {settings.credentials.backend}")
    print(f"  state backend       {settings.run.state_backend}")
    print(f"  on injection        {settings.security.on_suspected_injection}")
    print(f"  dry run             {settings.run.dry_run}")
    print("\n  labels (names; IDs are resolved from the mailbox at startup)")
    for priority, name in settings.labels.by_priority.items():
        print(f"    {priority.value}  {name}")
    return EXIT_OK


def cmd_labels(args: argparse.Namespace) -> int:
    # Imported here so `config validate` stays importable without the Google
    # client libraries present.
    from epc.gmail.auth import LocalFileCredentialStore, get_credentials
    from epc.gmail.client import GmailClient
    from epc.gmail.labels import resolve_priority_labels

    settings = _load(args.config)
    store = LocalFileCredentialStore(settings.credentials.token_file)
    client = GmailClient(get_credentials(store))

    available = client.list_labels()
    resolved = resolve_priority_labels(available, settings.labels.by_priority)

    print(f"{len(available)} labels in the mailbox. Configured priority labels resolve to:\n")
    for priority, label_id in resolved.items():
        print(f"  {priority.value}  {settings.labels.by_priority[priority]:<24} {label_id}")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    from epc.gmail.auth import SCOPES, LocalFileCredentialStore, get_credentials

    settings = _load(args.config)
    store = LocalFileCredentialStore(settings.credentials.token_file)
    get_credentials(store, client_secrets_file=settings.credentials.client_secrets_file)
    print(f"Authorised. Credentials stored at {settings.credentials.token_file}")
    print(f"Scope granted: {' '.join(SCOPES)}")
    return EXIT_OK


_COMMANDS = {
    ("config", "validate"): cmd_config_validate,
    ("labels", None): cmd_labels,
    ("login", None): cmd_login,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    handler = _COMMANDS[(args.command, getattr(args, "config_command", None))]
    try:
        return handler(args)
    except ValidationError as exc:
        print(f"{args.config}: invalid\n\n{exc}", file=sys.stderr)
        return EXIT_FATAL
    except EpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
