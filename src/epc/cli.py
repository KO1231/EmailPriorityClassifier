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
``epc prompt render``
    Print the exact prompt a thread would produce, personal policy included.
    The only way to review what is actually sent before sending it.

``run`` and ``apply`` arrive with the pipeline and the mutation sinks.
"""

import argparse
import json
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

    prompt = subcommands.add_parser("prompt", help="inspect the prompts")
    prompt_actions = prompt.add_subparsers(dest="prompt_command", required=True)
    render = prompt_actions.add_parser("render", help="print the prompt a thread would produce")
    _add_config_option(render)
    render.add_argument(
        "--file",
        type=Path,
        metavar="PATH",
        help="a Gmail threads.get JSON response; omit for a built-in example thread",
    )
    render.add_argument("--prompts", type=Path, default=Path("prompts"), metavar="DIR", help="prompt directory")
    render.add_argument("--policy", type=Path, default=Path("policy.yml"), metavar="PATH", help="personal policy")

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


# A thread with nothing personal in it, so `prompt render` works in a fresh
# clone with no mailbox and no policy file.
_EXAMPLE_THREAD = {
    "id": "example-thread",
    "messages": [
        {
            "id": "example-message",
            "threadId": "example-thread",
            "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1767225600000",
            "sizeEstimate": 2048,
            "payload": {
                "mimeType": "text/plain",
                "filename": "",
                "headers": [
                    {"name": "From", "value": "Dana Reed <dana@example.com>"},
                    {"name": "To", "value": "you@example.com"},
                    {"name": "Subject", "value": "Contract review before Friday"},
                    {"name": "Content-Type", "value": "text/plain; charset=utf-8"},
                ],
                "body": {
                    "size": 64,
                    "data": "Q291bGQgeW91IHJldmlldyB0aGUgYXR0YWNoZWQgY29udHJhY3QgYmVmb3JlIEZyaWRheT8",
                },
            },
        }
    ],
}


def cmd_prompt_render(args: argparse.Namespace) -> int:
    from epc.classify.budget import build_payload
    from epc.classify.prompt import PromptRenderer
    from epc.gmail.mime import parse_thread

    settings = _load(args.config)

    if args.file is not None:
        if not args.file.is_file():
            raise EpcError(f"thread file not found: {args.file}")
        raw = json.loads(args.file.read_text(encoding="utf-8"))
    else:
        raw = _EXAMPLE_THREAD

    payload = build_payload(parse_thread(raw), settings.llm.budget)
    rendered = PromptRenderer.load(args.prompts, args.policy).render(payload)

    print(f"# prompt version {rendered.prompt_version}   nonce {rendered.nonce}")
    print(f"# policy: {args.policy if args.policy.is_file() else 'none'}")
    print(f"# estimated {payload.estimated_tokens} tokens, {payload.omitted_messages} messages omitted")
    print("\n===== system =====\n")
    print(rendered.system)
    print("\n===== user =====\n")
    print(rendered.user)

    # Reported here, never in the prompt itself.
    if payload.injection.suspicious:
        print(
            f"\n# injection signal (not sent to the model): "
            f"{payload.injection.confidence} {payload.injection.patterns}",
            file=sys.stderr,
        )
    return EXIT_OK


_COMMANDS = {
    ("config", "validate"): cmd_config_validate,
    ("labels", None): cmd_labels,
    ("login", None): cmd_login,
    ("prompt", "render"): cmd_prompt_render,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    subcommand = getattr(args, "config_command", None) or getattr(args, "prompt_command", None)
    handler = _COMMANDS[(args.command, subcommand)]
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
