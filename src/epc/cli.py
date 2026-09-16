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

``epc run``
    List, classify and dispatch. ``--dry-run`` writes the planned changes to a
    file instead of applying them; that file can then be replayed.
``epc apply``
    Apply a file of planned changes — exactly what was reviewed, nothing else.
``epc failures``
    List or forget the threads that are skipped for failing repeatedly.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from epc import __version__
from epc.errors import EpcError
from epc.settings import CONFIG_ENV, DEFAULT_CONFIG_FILENAME, Settings, load_settings

if TYPE_CHECKING:  # pragma: no cover
    from epc.actions.model import ThreadMutation
    from epc.classify.prompt import PromptRenderer
    from epc.gmail.client import GmailClient

EXIT_OK = 0
EXIT_FATAL = 1
# Some threads were lost but the run completed. Distinct from a fatal error so a
# scheduler can tell "nothing happened" from "most of it happened".
EXIT_PARTIAL = 2


def _add_config_option(parser: argparse.ArgumentParser) -> None:
    # No default here: whether the flag was given decides between the file and
    # EPC_CONFIG_YAML, so an omitted flag has to be distinguishable.
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"configuration file (default: ${CONFIG_ENV} if set, else {DEFAULT_CONFIG_FILENAME})",
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
    labels.add_argument("--create", action="store_true", help="create configured labels that do not exist yet")

    login = subcommands.add_parser("login", help="authorise this tool against a Gmail account")
    _add_config_option(login)

    run = subcommands.add_parser("run", help="classify and label threads")
    _add_config_option(run)
    _add_prompt_options(run)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="write planned changes to a file instead of applying them",
    )
    run.add_argument("--limit", type=int, metavar="N", help="classify at most N threads (overrides the config)")

    failures = subcommands.add_parser("failures", help="inspect or forget threads that keep failing")
    failures_actions = failures.add_subparsers(dest="failures_command", required=True)
    failures_list = failures_actions.add_parser("list", help="show the recorded failures")
    _add_config_option(failures_list)
    _add_prompt_options(failures_list)
    failures_clear = failures_actions.add_parser("clear", help="forget recorded failures so they are retried")
    _add_config_option(failures_clear)
    failures_clear.add_argument("thread_ids", nargs="*", metavar="THREAD_ID", help="only these; omit for all")

    apply_command = subcommands.add_parser("apply", help="apply a file of planned changes")
    _add_config_option(apply_command)
    apply_command.add_argument("file", type=Path, help="a JSONL file written by --dry-run")

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
    _add_prompt_options(render)

    return parser


def _add_prompt_options(parser: argparse.ArgumentParser) -> None:
    from epc.classify.prompt import DEFAULT_POLICY_FILENAME, POLICY_ENV

    parser.add_argument("--prompts", type=Path, default=Path("prompts"), metavar="DIR", help="prompt directory")
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"personal policy (default: ${POLICY_ENV} if set, else {DEFAULT_POLICY_FILENAME})",
    )


def _config_source(path: Path | None) -> str:
    """What the configuration is read from, for messages."""
    if path is None and os.environ.get(CONFIG_ENV):
        return CONFIG_ENV
    return str(path or DEFAULT_CONFIG_FILENAME)


def _load(path: Path | None, **overrides: Any) -> Settings:
    """Settings from `--config` if given, else `EPC_CONFIG_YAML`, else config.yml.

    The same order as every other setting: the command line, then the
    environment, then the default. A variable picked up from `--env-file` must
    not quietly replace a file someone named on purpose — and on ECS, where the
    variable is how the config arrives, nobody passes the flag.
    """
    if path is None:
        text = os.environ.get(CONFIG_ENV)
        if text:
            return load_settings(config_text=text, **overrides)
        path = Path(DEFAULT_CONFIG_FILENAME)
    if not path.is_file():
        raise EpcError(
            f"configuration file not found: {path}\n"
            f"Copy {DEFAULT_CONFIG_FILENAME}.example and fill it in, or set {CONFIG_ENV} to its contents."
        )
    return load_settings(path, **overrides)


def _policy_source(path: Path | None) -> str:
    from epc.classify.prompt import DEFAULT_POLICY_FILENAME, POLICY_ENV

    if path is None and os.environ.get(POLICY_ENV):
        return POLICY_ENV
    resolved = path or Path(DEFAULT_POLICY_FILENAME)
    return str(resolved) if resolved.is_file() else "none"


def _renderer(args: argparse.Namespace) -> PromptRenderer:
    from epc.classify.prompt import POLICY_ENV, PromptRenderer

    # The same order as `--config`: the flag, then the variable, then policy.yml.
    policy_text = os.environ.get(POLICY_ENV) or None if args.policy is None else None
    if args.policy is not None and not args.policy.is_file():
        # An absent default policy.yml is normal: most people have none. A path
        # someone typed is not — silently classifying without their rules would
        # label a whole run by nobody's criteria, and rule B keeps those labels.
        raise EpcError(f"policy file not found: {args.policy}")
    return PromptRenderer.load(args.prompts, args.policy, policy_text=policy_text)


def cmd_config_validate(args: argparse.Namespace) -> int:
    settings = _load(args.config)
    print(f"{_config_source(args.config)}: valid\n")
    print(f"  search query        {settings.search_query}")
    print(f"  max threads         {settings.gmail.max_threads}")
    print(f"  llm backend         {settings.llm.backend}")
    print(f"  concurrency         {settings.llm.concurrency} at {settings.llm.requests_per_min}/min")
    print(f"  budget              {settings.llm.budget.thread_tokens} tokens/thread")
    print(f"  credentials         {settings.credentials.backend}")
    print(f"  state backend       {settings.state.backend}")
    print(f"  on injection        {settings.security.on_suspected_injection}")
    print(f"  dry run             {settings.dry_run}")
    print("\n  labels (names; IDs are resolved from the mailbox at startup)")
    for priority, name in settings.labels.by_priority.items():
        print(f"    {priority.value}  {name}")
    return EXIT_OK


def cmd_labels(args: argparse.Namespace) -> int:
    # Imported here so `config validate` stays importable without the Google
    # client libraries present.
    from epc.gmail.labels import resolve_priority_labels

    settings = _load(args.config)
    client = _gmail_client(settings)

    available = client.list_labels()
    if args.create:
        # Here, as a deliberate setup step, and never during `run`: a dry run
        # promises to write nothing, and a label is a write.
        existing = {label.name for label in available}
        for name in settings.labels.by_priority.values():
            if name not in existing:
                client.create_label(name)
                print(f"Created label {name!r}")
        available = client.list_labels()
    resolved = resolve_priority_labels(available, settings.labels.by_priority)

    print(f"{len(available)} labels in the mailbox. Configured priority labels resolve to:\n")
    for priority, label_id in resolved.items():
        print(f"  {priority.value}  {settings.labels.by_priority[priority]:<24} {label_id}")
    return EXIT_OK


def cmd_login(args: argparse.Namespace) -> int:
    from epc.backends import build_credential_store
    from epc.gmail.auth import SCOPES, authorise

    settings = _load(args.config)
    # Authorise here, store wherever the backend says — so the browser flow can
    # run on a laptop and the credential land in a parameter the task reads.
    store = build_credential_store(settings)
    authorise(store, client_secrets_file=settings.credentials.client_secrets_file)
    where = (
        settings.credentials.token_file
        if settings.credentials.backend == "local"
        else settings.credentials.parameter_name
    )
    print(f"Authorised. Credentials stored at {where}")
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
    from epc.gmail.mime import parse_thread

    settings = _load(args.config)

    if args.file is not None:
        if not args.file.is_file():
            raise EpcError(f"thread file not found: {args.file}")
        raw = json.loads(args.file.read_text(encoding="utf-8"))
    else:
        raw = _EXAMPLE_THREAD

    payload = build_payload(parse_thread(raw), settings.llm.budget)
    rendered = _renderer(args).render(payload)

    print(f"# prompt version {rendered.prompt_version}   nonce {rendered.nonce}")
    print(f"# policy: {_policy_source(args.policy)}")
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


def _gmail_client(settings: Settings) -> GmailClient:
    from epc.backends import build_credential_store
    from epc.gmail.auth import load_credentials
    from epc.gmail.client import GmailClient

    return GmailClient(load_credentials(build_credential_store(settings)))


def cmd_run(args: argparse.Namespace) -> int:
    from epc.backends import build_sink, build_state_store
    from epc.classify.factory import build_classifier
    from epc.gmail.labels import resolve_priority_labels
    from epc.logging import configure_logging
    from epc.pipeline import Pipeline
    from epc.report import HistorySink, JsonlHistorySink, NullHistorySink
    from epc.shutdown import shutdown_on_signal

    settings = _load(args.config) if args.limit is None else _load(args.config, gmail={"max_threads": args.limit})

    configure_logging(
        level=settings.observability.log_level,
        json_output=settings.observability.log_json,
        log_file=settings.observability.log_file,
    )

    client = _gmail_client(settings)
    # Before a single thread is fetched: a configured label that does not exist
    # is a configuration error, not a surprise 1500 threads later.
    priority_label_ids = resolve_priority_labels(client.list_labels(), settings.labels.by_priority)

    renderer = _renderer(args)
    classifier = build_classifier(settings, renderer)

    sink_kind = settings.resolve_sink(force_dry_run=args.dry_run)
    writes_nothing = sink_kind == "jsonl"
    sink = build_sink(settings, gmail_client=client, force_dry_run=args.dry_run)
    if writes_nothing:
        print(f"Dry run - planned changes go to {settings.dispatch.jsonl_path}, nothing is applied.")
    elif sink_kind == "sqs":
        print(f"Dispatching to {settings.dispatch.queue_url}; another process applies them.")

    state = build_state_store(settings)
    history: HistorySink = (
        JsonlHistorySink(settings.observability.history_dir)
        if settings.observability.history_dir is not None
        else NullHistorySink()
    )

    with shutdown_on_signal() as stopping:
        pipeline = Pipeline(
            settings=settings,
            client=client,
            classifier=classifier,
            sink=sink,
            priority_label_ids=priority_label_ids,
            state_store=state,
            history=history,
            shutdown=stopping,
            classifier_version=_classifier_version(settings, renderer),
        )
        print(f"Query: {pipeline.search_query()}")
        summary = pipeline.run()

    print()
    print(summary.render())
    if summary.interrupted:
        print("\nStopped on request. Everything already classified was flushed;")
        print("the rest still has no label, so it comes back next run.")
    elif summary.halted:
        print("\nStopped starting new classifications after too many failures in a row.")
        print("Everything already classified was flushed; the rest comes back next run.")

    if writes_nothing:
        print(f"\nReplay with:  epc apply {settings.dispatch.jsonl_path}")
    return EXIT_PARTIAL if summary.had_failures else EXIT_OK


def _classifier_version(settings: Settings, renderer: PromptRenderer) -> str:
    """What failure records are valid for. A change retries every one of them.

    Everything that shapes a request or its answer: the whole `llm` section,
    because output limits, reasoning effort and budgets cause refusals and cut
    answers as surely as the model does; the prompts and policy; and this
    program's version, so a fix that ships clears what the bug recorded. Only
    pacing is left out — it changes when a request is sent, not what comes back.
    Readable at the front, so `epc failures list` can say what changed.
    """
    import hashlib

    material = "\x00".join(
        [
            settings.llm.model_dump_json(exclude={"concurrency", "requests_per_min"}),
            renderer.fingerprint,
            __version__,
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    return f"{settings.llm.backend}/{settings.llm.model}/{digest}"


def cmd_failures_list(args: argparse.Namespace) -> int:
    from epc.backends import build_state_store
    from epc.state import SKIP_AFTER_ATTEMPTS

    settings = _load(args.config)
    recorded = build_state_store(settings).load()
    # Through the same filter the next run applies, so this shows what that run
    # will do rather than what happens to be stored. The mailbox is not checked:
    # that needs Gmail, and this command works offline.
    current = _classifier_version(settings, _renderer(args))
    state = recorded.for_run(mailbox=recorded.mailbox or "", classifier=current)
    if recorded.failures and not state.failures:
        if recorded.classifier not in (None, current):
            print(f"{len(recorded.failures)} failure(s) were recorded under {recorded.classifier}.")
            print(f"The model, its settings or the prompt has changed since ({current}); all are retried next run.")
        else:
            print(f"{len(recorded.failures)} failure(s) are past the 30-day limit; all are retried next run.")
        return EXIT_OK
    if not state.failures:
        print("No recorded failures.")
        return EXIT_OK

    skipped = set(state.skipped())
    print(f"{len(state.failures)} recorded failure(s), recorded under {state.classifier or 'an unknown classifier'}:\n")
    for thread_id, record in sorted(state.failures.items(), key=lambda item: item[1].first_failed_at):
        status = "skipped" if thread_id in skipped else f"retrying ({record.attempts}/{SKIP_AFTER_ATTEMPTS})"
        print(f"  {thread_id}  since {record.first_failed_at:%Y-%m-%d}  {status}")
    print("\nA thread comes back when it changes in Gmail, when the model or prompt changes, or after 30 days.")
    return EXIT_OK


def cmd_failures_clear(args: argparse.Namespace) -> int:
    from epc.backends import build_state_store

    store = build_state_store(_load(args.config))
    state = store.load()
    targets = set(args.thread_ids) if args.thread_ids else set(state.failures)
    unknown = sorted(set(args.thread_ids) - set(state.failures))
    remaining = {tid: record for tid, record in state.failures.items() if tid not in targets}

    store.save(state.model_copy(update={"failures": remaining}))
    print(f"Forgot {len(state.failures) - len(remaining)} recorded failure(s); they are retried on the next run.")
    for thread_id in unknown:
        print(f"  (no record for {thread_id})", file=sys.stderr)
    return EXIT_OK


def cmd_apply(args: argparse.Namespace) -> int:
    from epc.dispatch.applier import MutationApplier
    from epc.dispatch.sink import read_mutations

    settings = _load(args.config)
    if not args.file.is_file():
        raise EpcError(f"file not found: {args.file}")

    mutations = list(read_mutations(args.file))
    client = _gmail_client(settings)
    print(f"Applying {len(mutations)} planned change(s) from {args.file}")

    current, unchecked = _still_as_planned(client, settings, mutations)
    report = MutationApplier(client, batch_size=settings.dispatch.batch_size).apply(current)

    skipped = len(mutations) - len(current) - len(unchecked)
    print(
        f"  applied {report.applied}  already as planned {report.skipped_noop}  "
        f"failed {report.failed + len(unchecked)}  gmail calls {report.api_calls}"
    )
    if skipped:
        print(f"  skipped {skipped}: the thread's priority label changed after it was planned")
    for failure in [*report.failures, *unchecked]:
        print(f"  ! {failure}", file=sys.stderr)
    return EXIT_PARTIAL if report.had_failures or unchecked else EXIT_OK


def _still_as_planned(
    client: GmailClient, settings: Settings, mutations: list[ThreadMutation]
) -> tuple[list[ThreadMutation], list[str]]:
    """The mutations whose threads have not been re-labelled since planning.

    A plan file can wait hours between `run --dry-run` and `apply`, and a person
    can label a thread in that time. Applying the plan over that label would
    undo the correction — rule B, broken by a replay. So each thread's labels are
    read again first. A classified change goes ahead only if the thread still
    has no priority label; a carried-forward one only if it still has exactly
    the label being carried.

    The apply Lambda does not do this. It runs seconds after planning, and a
    read per thread would spend Gmail quota on a race that window barely allows.
    """
    from epc.errors import GmailError
    from epc.gmail.labels import resolve_priority_labels

    priority_ids = set(resolve_priority_labels(client.list_labels(), settings.labels.by_priority).values())
    current: list[ThreadMutation] = []
    unchecked: list[str] = []
    for mutation in mutations:
        try:
            present = client.thread_label_ids(mutation.thread_id) & priority_ids
        except GmailError as exc:
            unchecked.append(f"{mutation.thread_id}: not applied, labels could not be checked: {exc}")
            continue
        expected = set(mutation.add_label_ids) & priority_ids if mutation.origin == "carried_forward" else set()
        if present == expected:
            current.append(mutation)
    return current, unchecked


_COMMANDS = {
    ("config", "validate"): cmd_config_validate,
    ("labels", None): cmd_labels,
    ("login", None): cmd_login,
    ("prompt", "render"): cmd_prompt_render,
    ("run", None): cmd_run,
    ("failures", "list"): cmd_failures_list,
    ("failures", "clear"): cmd_failures_clear,
    ("apply", None): cmd_apply,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return EXIT_OK

    subcommand = (
        getattr(args, "config_command", None)
        or getattr(args, "prompt_command", None)
        or getattr(args, "failures_command", None)
    )
    handler = _COMMANDS[(args.command, subcommand)]
    try:
        return handler(args)
    except ValidationError as exc:
        print(f"{_config_source(args.config)}: invalid\n\n{exc}", file=sys.stderr)
        return EXIT_FATAL
    except EpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
