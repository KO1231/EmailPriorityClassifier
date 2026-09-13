# CLAUDE.md

Implementation notes. Product description and setup live in `README.md`; this file
covers what an implementer needs — the invariants, the reasons behind the odd-looking
choices, and the traps.

`epc` is short for *EmailPriorityClassifier*. It is the Python package (`src/epc/`),
the CLI command, and the environment-variable prefix (`EPC__GMAIL__MAX_THREADS`). The
distribution keeps the full name because that is what appears in a lockfile.

---

## Commands

```bash
make install        # uv sync --all-extras + install the git hooks
make check          # ruff + ruff format --check + mypy --strict + pytest
make test           # pytest, excluding the opt-in eval suite
make audit          # pip-audit over the locked dependency set

uv run epc config validate      # parse and print the config; no network
uv run epc labels               # resolve the configured labels against the mailbox
uv run epc login                # OAuth, once
uv run epc prompt render        # the exact prompt a thread produces
uv run epc run --dry-run        # plan everything, write nothing
uv run epc apply log/mutations.jsonl
```

`.env` is not read by the application — there is no `python-dotenv`. Use
`uv run --env-file .env …`, or export the variables. Anything that adds a new
entrypoint (Docker, cron, systemd) has to load it explicitly.

Python 3.14, pinned in `.python-version` and `requires-python`.

---

## The two rules everything rests on

**A. The model chooses a priority, never an action.** Its whole output surface is
`{priority: "P1"|"P2"|"P3", reason, confidence, signals}` under a strict JSON schema —
see `classify/prompt.py::response_json_schema`. There is no field for naming a label,
requesting an action, or calling a tool. Actions are derived by `actions/planner.py`
from configuration. **A completely successful prompt injection can therefore do exactly
one thing: mislabel one thread.** Any feature that lets the model pick an action
invalidates the entire security story, not just part of it.

**B. Only newly classified threads are planned.** A thread that already carries a
priority label is counted and skipped (`pipeline.py::_hydrate`). It may have been set by
hand, and now that actions can *remove* labels, re-planning would undo that correction
on every run — silently, forever.

Both are covered by tests. Breaking either should fail the suite, not a mailbox.

---

## Flow

```
epc run
 ├ resolve labels          gmail/labels.py    names → IDs, at startup, fails loud
 ├ list threads            gmail/query.py     quoted label exclusions
 │   or history.list       gmail/client.py    incremental, from a stored historyId
 ├ fetch + parse           gmail/mime.py      ★ recursive MIME walk, charsets, headers
 ├ budget + sanitise       classify/budget.py ★ the only route to a prompt
 ├ classify                classify/*         openai | bedrock | local
 ├ plan                    actions/planner.py priority + rules → ThreadMutation
 └ dispatch                dispatch/sink.py   DirectSink applies, JsonlSink is dry-run
```

### Things that look odd and are not

**`classify/budget.py::build_payload` is the sanitisation gate.** It is the only
supported route from a parsed thread to a prompt, and it sanitises on the way through.
Anything reading `EmailMessage.body` directly has skipped that; `tests/injection/`
exists to catch it.

**Hidden-text removal lives in `gmail/mime.py`, not `security/`.** Text hidden with
`display:none` is a question of what is *visible*, and deciding it needs the markup —
which is gone by the time sanitisation runs. `html_to_text` parses; it does not render,
resolve the CSS cascade, or compute layout. Two kinds of hiding therefore get through
(class selectors in a `<style>` block, and colour matching an inherited background);
both are named cases in `tests/injection/corpus.py` and are held shut by detection
rather than removal.

**The injection signal is never shown to the model.** Telling it "this thread looks
hostile" would make that judgement itself worth attacking. It travels beside the
payload and is acted on by the planner.

**`actions/` and `dispatch/` must not import `classify/` or `gmail/mime`.** That is
what keeps the apply side's dependencies to `google-api-python-client` plus `boto3`.
`tests/unit/test_layering.py` asserts it against the import graph — a convention nobody
can verify is one that erodes.

**Threads, not processes.** The work is HTTP wait end to end. Processes forced the
classifier to be picklable (hence module-global API clients), paid spawn cost per run,
and had every child open the same rotating log. `ratelimit.py` is a next-slot clock, so
one slow response delays only itself.

**OpenAI goes through the Responses API; local goes through chat completions.** A key
scoped to `api.responses.write` reaches Responses without `model.request`, which is
"call any model on any endpoint". Local servers implement chat completions and mostly
not Responses, so that end cannot follow. `store=False` on every OpenAI request.

**Bedrock uses boto3 Converse, not a vendor client.** Converse is provider-agnostic —
the same request reaches Claude, Nova, Llama, Mistral. Structured output is *requested*
through a tool definition and never depended on: forcing a tool is unsupported on some
models, so a rejection latches and degrades to `toolChoice: auto`, then to reading text.
`parse_classification` is the backstop, which keeps the enum — not the transport — as
the boundary.

**Logging redaction is a backstop, not the policy.** Call sites pass IDs, counts and
enums. `logging.py::redact` replaces content-bearing keys anyway, so a future mistake
leaks a truncated line instead of a mailbox.

**The history file records a subject digest and a sender domain, never the mail.** It
outlives the run, gets copied to S3, and ends up in backups. Default is off.

**`dry_run` is top level, not inside a section.** It overrides `dispatch`, so it is not
one of dispatch's peers. `Settings.resolve_sink()` holds that precedence in one place:
every entry point gets it, and a sink added later cannot accidentally become one that
writes during a dry run.

**The checkpoint only advances on a clean run.** After a partial failure the next run
re-lists the same window; threads that did succeed are excluded by their labels, so the
repeat is free.

---

## Conventions

**Language.** Code comments and docstrings are Japanese. Log messages, exception
messages and documentation are English.

**Commits.** `type: summary` — `add:`, `fix:`, `update:`, `refactor:`, `remove:`,
`change:`. Summaries are usually Japanese. No attribution or co-author trailers.

**Branches.** `main` is released state, `develop` integrates, features branch from
`develop`. Two GitHub Actions workflows auto-close PRs that violate this. Never commit
to `main` (a pre-commit hook enforces it).

**The remote is public.** `.gitignore` denies by default where it matters: `prompts/*`
is excluded and the two generic templates are allow-listed by name, so a new file there
stays out until someone lists it. `config.yml`, `.env`, `policy.yml`, `secrets/**`,
`log/**` and `.state/` are excluded. gitleaks runs in pre-commit and CI. Test fixtures
are **generated**, never captured — `tests/fixtures/gmail.py` builds Gmail JSON from
synthetic MIME, which is also why hostile shapes are cheap to author.

**Personal rules go in `policy.yml`, not in `prompts/`.** The committed prompts have to
stay publishable. A guard test asserts they contain no personal context.

---

## Not done yet

- **Container and AWS.** `Dockerfile`, then Terraform laid out like
  `KO1231/Delibird-priv` (`environments/` + `modules/aws_*`). `dev/` and `prod/` stay
  git-ignored; only `sample/` is committed. Secrets never pass through Terraform state —
  SSM parameters are created empty with `lifecycle { ignore_changes = [value] }` and
  filled out-of-band.
- **SQS dispatch.** `SqsSink` / `SqsSource` plus an apply Lambda. FIFO with
  `MessageGroupId = thread_id`; the ordering matters more than the 5-minute dedup
  window, which is a cost optimisation rather than a correctness mechanism.
- **Remote credential and state backends.** The protocols exist (`gmail/auth.py`,
  `state.py`); SSM Parameter Store and S3 implementations do not.
- **Eval harness.** A golden set and `epc eval`, so prompt changes are measured rather
  than guessed at. `report.py` already writes what it needs.
- **Notification.** Deferred deliberately; revisit once the run summary is something
  worth sending.
