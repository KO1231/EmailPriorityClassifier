# CLAUDE.md

Implementation notes for working on this codebase. Product-level description, design rationale,
and setup instructions live in `README.md` — this file covers the parts only an implementer
needs: exact control flow, data shapes, invariants, known defects, and the traps that are easy
to fall into during a refactor.

---

## Status: rewrite in progress

`feature/scrap-and-build` is replacing this implementation package by package. Two trees coexist:

| Tree | State |
|---|---|
| `src/epc/` | The rewrite. uv + Python 3.14, `ruff`/`mypy --strict`/`pytest` all enforced in CI. |
| `main.py`, `email_priority_classifier/` | The legacy implementation. Still runnable, still the thing that actually triages mail. Excluded from ruff and mypy; deleted once the new pipeline takes over. |

Everything below the Commands section describes the **legacy** tree unless it says otherwise.

Landed so far:

- **Ground work** — `pyproject.toml` + `uv.lock`, dependencies split into `core` / `classify` /
  `aws` so the apply worker never pulls an LLM SDK; `[tool.uv] exclude-newer` as a supply-chain
  cooldown; ruff, mypy strict, pytest, pre-commit with gitleaks, CI with SHA-pinned actions.
- **MIME core** — `epc.gmail.mime` and `epc.gmail.models`. Recursive payload walk, charset-aware
  decoding, HTML decoded before parsing, RFC 2047 headers, sender and bulk-mail headers preserved,
  raw `labelIds` kept. `tests/fixtures/gmail.py` generates Gmail JSON from synthetic MIME — no real
  mail in the repo, ever.
- **Config and labels** — `epc.settings` (pydantic-settings; CLI > env > `config.yml` > defaults,
  unknown keys rejected), `epc.gmail.labels` (names resolved to IDs at startup, `labelID` gone),
  `epc.gmail.auth` (JSON not pickle, `gmail.modify` only, access token never stored),
  `epc.gmail.client`. CLI: `epc config validate`, `epc labels`, `epc login`.

  The two implementations both default to `config.yml` during the transition and their schemas
  are incompatible. The legacy file keeps working for the legacy tool; `epc` detects the old
  schema and says so rather than emitting a wall of unknown-key errors. Pass `--config` to point
  the new tool at a new-schema file until the legacy tree is removed.
- **Untrusted-content handling** — `epc.security.sanitize` (NFKC, zero-width and bidi stripping,
  control characters, `data:` URIs, long binary runs, length caps) and `epc.security.detect`
  (narrow, high-precision heuristics that produce a signal and never a decision). Text hidden by
  CSS is dropped during HTML extraction instead, because that is a question of what is *visible*;
  the count is carried on `EmailMessage.hidden_elements_removed` so the detector can use it.
  `tests/injection/` is the adversarial corpus.
- **Classification** — `epc.classify`. `budget` (quote stripping, newest-first budgeting, and the
  sanitisation gate), `prompt` (committed prompts + git-ignored `policy.yml`, nonce-wrapped
  untrusted content), `base` (the `Classifier` protocol and the shared response parser), and three
  backends behind `factory.build_classifier`: `openai`, `local` (OpenAI-compatible), `bedrock`.

  **Bedrock uses boto3's Converse API, not an Anthropic-specific client.** Converse is the
  provider-agnostic surface — the same request reaches Claude, Nova, Llama, Mistral and the OpenAI
  open-weight models on Bedrock. A vendor client would fit one family and dead-end the rest, which
  is the opposite of what a swappable backend is for. Structured output is *requested* through a
  tool definition and never depended upon: forcing a tool is unsupported on some models, so a
  rejection degrades to `toolChoice: auto` (latched, so 1500 threads do not each pay for a doomed
  first attempt) and then to reading plain text. `parse_classification` is the backstop in every
  case, which is what keeps the enum — not the transport — as the security boundary.
- **Pipeline and actions** — `epc.pipeline` (ThreadPoolExecutor, not processes; the work is HTTP
  wait end to end), `epc.ratelimit` (a next-slot clock, so one slow response delays only itself),
  `epc.actions` (declarative rules, first match wins), `epc.dispatch` (`DirectSink` applies,
  `JsonlSink` *is* dry-run and its output replays exactly). `epc run` / `epc apply`.
  `tests/unit/test_layering.py` asserts the import graph: `actions/` and `dispatch/` never reach
  `classify/` or the MIME parser, which is what keeps the apply worker's dependencies to
  `google-api-python-client` plus `boto3`.
- **Operations** — `epc.logging` (structlog; a redaction processor as a backstop, because the
  previous implementation wrote whole email bodies to disk at DEBUG), `epc.report` (classification
  history: subject as a digest, sender as a bare domain, never the mail), `epc.state` (the Gmail
  `historyId` checkpoint), and History API incremental sync in the pipeline. The checkpoint only
  advances on a clean run — after a partial failure the next run re-lists the same window, and the
  label-based exclusion makes the threads that did succeed free to skip.

### Carried forward — obligations deferred out of a completed phase

Deliberate omissions, recorded so they are not mistaken for oversights later.
Nothing is removed from this list until the work is actually in the tree.

| Owed | Deferred from | Status |
|---|---|---|
| Untrusted-content neutralisation | MIME core | **Delivered.** `epc.security.sanitize` |
| Injection-detection signal | MIME core | **Delivered.** `epc.security.detect` |

One obligation remains open, and it is the reason the list stays:

> **`epc.gmail.mime` output must not reach a prompt directly.** Everything that
> goes to a model passes through `sanitise()` first, and carries the resulting
> `InjectionSignal`. `tests/injection/` holds the invariant — for every known
> attack, the payload is either removed or flagged, never present and silent —
> and it is the gate on the classifier. A backend that reads `message.body`
> without sanitising it is a bug that suite must be extended to catch.

---

---

## Commands

```bash
# Rewrite (src/epc/)
make install                      # uv sync --all-extras + install the git hooks
make check                        # lint + format check + mypy --strict + pytest
make test                         # pytest, excluding the opt-in eval suite
make audit                        # pip-audit over the locked dependency set

# Legacy (main.py) — still runs, unaffected by the Python 3.14 bump because
# pipenv keeps using its existing 3.13.5 virtualenv.
make legacy-run                   # DEV_NOT_MODIFY=true pipenv run start
pipenv run login_google           # python email_priority_classifier/gmail_credentials.py
```

Lint, format, type and test configuration all live in `pyproject.toml` and apply to `src/` and
`tests/` only; the legacy tree is in ruff's `extend-exclude` and outside mypy's `files`. Linting
code that is being deleted buys nothing. (`.idea/misc.xml` still references Black via the Pipenv
SDK — a leftover; Black is not a dependency and ruff does the formatting.)

**`.env` is loaded by pipenv, not by the application.** There is no `python-dotenv` dependency.
`pipenv run ...` auto-loads `.env` from the project root; a bare `python main.py` will not, and
will fail at import time with `KeyError` or an OpenAI auth error. Any refactor that introduces a
non-pipenv entry point (Docker, cron, systemd) must load `.env` explicitly.

`python-version` is pinned to 3.13.5 in both `.python-version` and `Pipfile`. The code uses
PEP 701 nested same-quote f-strings (`main.py:52`, `main.py:76`, `main.py:145`), so **3.12 is
the hard floor** regardless of what the Pipfile says.

---

## Execution flow

Entry point is the `__main__` block at `main.py:248-260`. One invocation is one batch; there is
no loop, no daemon, and no scheduler.

```
__main__ (main.py:248)
├── load_config(CONFIG_FILE)                      config.py:18
├── get_classifier(config.model_name)             main.py:235   ← lazy import, see below
└── main(classifier, config)                      main.py:214
    ├── build("gmail", "v1", credentials=get_credential(...))
    ├── fetch_personal_label_info(service)        main.py:209   Label_* → display name
    ├── assert_positive × 3                       main.py:225-227
    ├── classify(...)                             main.py:61    → dict[EmailPriority, set[str]]
    └── modify_thread_labels(...)                 main.py:179
```

### `classify()` — `main.py:61-176`

The single largest function in the codebase and the primary refactor target.

```
build threads().list request                      main.py:71-77
create Pool(concurrency) if concurrency > 1       main.py:80-85
while request is not None:                        main.py:89
  ├── response = request.execute()                main.py:92
  ├── for each thread in page:                    main.py:99
  │   ├── get_thread_messages(service, id)        main.py:103   threads().get(format="full")
  │   ├── ClassifiedEmailData.init(m, labels)     main.py:105   per message; failure skips thread
  │   ├── if thread already has a priority label: main.py:114-119
  │   │     fold into result, do NOT re-classify
  │   └── else append to thread_data_list         main.py:120
  ├── classify the batch:
  │   ├── concurrency > 1  → pool.map per batch   main.py:126-152
  │   └── concurrency == 1 → inline sequential    main.py:154-164
  └── request = threads().list_next(...)          main.py:167
finally: pool.close(); pool.join()                main.py:168-171
```

Key details:

- **The Gmail query is built from label *display names*, not IDs** (`main.py:76`):
  `(in:inbox) AND NOT(label:#/P1 OR label:#/P2 OR label:#/P3)`. Label names are interpolated
  unquoted, so a label name containing a space will silently produce a malformed query and
  classify already-labeled threads again. Quote the names if label naming is ever relaxed.
- **Idempotency is enforced twice**: once by the query above, and again at `main.py:114-119`
  after fetching, in case a label appeared mid-run. The second check compares *display names*,
  because `_PARSE_LABEL` has already converted IDs to names by that point.
- **Already-labeled threads are folded into `result` and then re-written**. They skip the LLM
  call, but `modify_thread_labels` still issues a `threads().modify` adding a label the thread
  already has. Correct, but wasted API calls — one per skipped thread.
- **`max_threads` short-circuits with `return`, not `break`** (`main.py:142-143`, `main.py:161-162`).
  This returns from inside the `try`, so the `finally` at `main.py:168` still closes the pool.
  It does mean the remaining pages are silently abandoned.
- **`processed_threads` counts failures too** (`main.py:140`). A run where every thread fails
  still terminates at `max_threads`.
- **Rate limiting is batch-granular** (`main.py:149-152`): after each batch of `concurrency`
  requests, sleep `60 * len(batch) / rate_limit_in_min` seconds. The sleep is skipped for the
  final batch of a page. The single-process path sleeps `60 / rate_limit_in_min` per thread
  (`main.py:163-164`).
- **The `try` block spans the entire function** (`main.py:70-174`) and re-raises everything as
  `EmailPriorityClassifierGmailAPIException("Some error occurred while listing threads.")`.
  Classification failures on the single-process path therefore surface as a *Gmail* error with
  a misleading message. See "Known defects" below.

### `modify_thread_labels()` — `main.py:179-206`

Serial loop, one `threads().modify` HTTPS round trip per thread. `addLabelIds` only;
`removeLabelIds` is hardcoded `[]` (`main.py:187`). Per-thread failures are caught and logged,
then the loop continues (`main.py:203-205`).

`DEV_NOT_MODIFY` is read from `os.environ` **inside the loop, once per thread** (`main.py:198`).
Any refactor should hoist this to a flag resolved once at startup.

---

## Data model

### `ClassifiedEmailData` — `type/classified_email_data.py`

Wraps one Gmail message and produces the LLM-facing representation.

```python
ClassifiedEmailData(
    date: int,            # internalDate, Unix ms
    payload: dict,        # raw Gmail payload, retained whole
    size_estimate: int,
    labels: list[str],    # display names, already translated
)
# .subject is derived from payload headers in __init__ (line 58)
```

**Label translation** (`_PARSE_LABEL`, lines 23-31): system labels are mapped through
`_LABEL_REPLACE_DATA` (lines 7-18) to readable names, user labels through the `Label_* → name`
dict fetched by `fetch_personal_label_info`. **Anything in neither map is silently dropped** —
including `UNREAD`, `SENT`, `DRAFT`, and every `CATEGORY_*` value not listed. The raw
`labelIds` are not retained anywhere, so any feature that needs the original IDs (e.g. moving a
thread between Gmail tabs) must first plumb them through.

**Body extraction** (`get_data`, lines 62-85), in order:
1. `payload["body"]` if non-empty and `size != 0` → base64-decode and return.
2. Otherwise, direct children of `payload["parts"]` whose `mimeType` starts with `text/`.
   Sorted `reverse=True` on mimeType string, which puts `text/plain` before `text/html` — an
   incidental property of alphabetical ordering, not an explicit preference.
3. If no parts matched: the literal string `"parts could not found."`.
4. If parts matched but none had `body.data`: the first part JSON-dumped whole.

`_decode_body` (lines 42-53) does `re.sub(r"\s+", " ", base64.urlsafe_b64decode(body).decode("utf-8"))`
— base64 decode, hardcoded UTF-8, whitespace collapse. The commented-out block below it is a
dead attempt at `Content-Transfer-Encoding` handling; `cchardet` was removed in `38481b0`, so
there is currently no encoding detection at all.

### `ClassifiedEmailDataEncoder` — `type/classified_email_data.py:97-105`

The JSON encoder that defines **exactly what the model sees per message**:

```json
{"date": 1700000000000, "size_estimate": 12345, "data": "…body text…"}
```

That is the complete list. No `From`, no `To`, no `Cc`, no per-message `Subject`. Only the
thread's first message subject is passed, separately, as a prompt variable
(`classifier_openai.py:30`). If you are wondering why sender-based rules in the prompt do not
fire — this is why.

### `_encode_thread_messages` — `classifier/email_priority_classifier.py:10-15`

```json
{"labels": ["Inbox", "Category: Personal", …], "messages": [ …encoded messages… ]}
```

Labels are unioned across all messages in the thread and deduplicated. Messages are in Gmail's
order, which is **oldest first**.

---

## Classifier backends

`EmailPriorityClassifier` (`classifier/email_priority_classifier.py:8-19`) is an ABC with one
abstract method, `calc(thread_messages) -> EmailPriority`, plus the shared static encoder.
Adding a backend means: subclass, implement `calc`, add a `case` to `get_classifier`
(`main.py:237-245`).

**`get_classifier` imports lazily on purpose** (`main.py:239`, `main.py:242`). Both backend
modules construct their `OpenAI` client at module scope, and both read required environment
variables at import time:

| Backend | Module-level side effect | Fails at import without |
|---|---|---|
| `ClassifierOpenAI` | `_CLIENT = OpenAI()` (line 13) | `OPENAI_API_KEY` |
| `ClassifierGPTOSS` | `OpenAI(base_url=f"http://localhost:{os.environ['LOCAL_LM_PORT']}/v1")` (lines 14-17) | `LOCAL_LM_PORT` |

Eager imports would make `openai` mode require `LOCAL_LM_PORT` and vice versa. **Do not
"clean up" these imports to the top of the file** without first moving client construction into
`__init__`.

`ClassifierGPTOSS`'s base URL is hardcoded to `localhost` (line 15), which is the main blocker
for containerizing the local-model path.

### Truncation

| Backend | Budget | Location |
|---|---|---|
| `openai` | 2500 chars | `classifier_openai.py:31` |
| `gpt-oss` | 1000 chars | `classifier_gptoss.py:48` |

These are **character slices of the already-serialized JSON string**, so the model receives
truncated, syntactically invalid JSON on any thread that exceeds the budget, with the newest
messages cut first. The numbers themselves were tuned against cost (~4000 input tokens for the
OpenAI path); keep the values when fixing *how* the truncation is applied.

### Response parsing

Both backends do the same thing (`classifier_openai.py:48-54`, `classifier_gptoss.py:70-76`):

```python
raw = json.loads(response.output_text)
return EmailPriority[raw["priority"]]        # KeyError unless exactly "P1"/"P2"/"P3"
```

No JSON schema is enforced on the response, no fallback priority, no retry. Any deviation —
a markdown fence, a prose preamble, `"P1 (high)"` — raises and loses the thread for that run.

### `ClassifierGPTOSS._create_request_prompt` — `classifier_gptoss.py:37-42`

Declared `@staticmethod` but takes `self` as its first parameter, and is called as
`self._create_request_prompt(self, self._prompt_info, ...)` (line 47). It works by accident.
Fix it to an instance method or a true static method, but note the call site must change too.

---

## Configuration

`load_config` (`config.py:18-29`) is a flat read of `config.yml` into a `NamedTuple`. Every key
is required; a missing key is a `KeyError` at startup, which is the desired behavior.

**`labelID` and `priorityLabels` are two independent hand-maintained maps of the same three
labels.** Nothing validates that they agree. A mismatch produces the worst kind of bug: threads
classified as P1 get labeled P3, silently, forever. Both are needed because Gmail's search
syntax takes names while `threads().modify` takes IDs — but `fetch_personal_label_info`
(`main.py:209-211`) already builds the full `Label_* → name` map at startup, so the IDs can be
resolved from the names and `labelID` removed entirely.

**`priorityAssociatedLabelIDs` exists in the working `config.yml` but is not read by any code.**
`load_config` ignores it. It is a leftover from the `feature/associated_label` branch; either
implement it or drop it from the file.

Note that `config.yml` and `config.yml.example` have drifted apart — the example lacks
`priorityAssociatedLabelIDs`, and its `priorityLabels` values are placeholders rather than the
`#/P1` style actually in use.

---

## Concurrency

`multiprocessing.Pool` (`main.py:84`), sized by `config.concurrency`. Work is dispatched with
`pool.map` over slices of `batch_size == concurrency` (`main.py:131-135`), so **each batch
blocks until its slowest request finishes** before the next batch starts.

Consequences that constrain the code as written:

- **The classifier instance must be picklable.** `partial(_classify_worker, classifier=classifier)`
  (`main.py:128`) ships the classifier to each worker. This is precisely why the `OpenAI` clients
  are module-level globals rather than instance attributes — an `OpenAI` client is not picklable.
  Both classifiers currently hold only strings/NamedTuples, so they pickle fine.
- **macOS uses the `spawn` start method.** Each child re-imports the backend module, so each
  child constructs its own `OpenAI` client and its own copy of the `logger_util` handlers.
- **Every child opens the same `RotatingFileHandler`** (`logger_util.py:19-24`). Concurrent
  rotation across processes is not safe; interleaved or lost log lines are possible under load.
- The workload is pure HTTP wait. `ThreadPoolExecutor` would remove all three constraints above,
  but it is a behavioral change and should not be bundled with correctness fixes.

`_classify_worker` (`main.py:28-37`) catches every exception, logs it, and returns `(None, None)`,
which the caller filters at `main.py:138`. **This safety net only exists on the multiprocess
path** — see below.

---

## Logging

`util/logger_util.py` builds three handlers **at module import**, shared by every logger:

| Handler | Target | Level | Filter |
|---|---|---|---|
| `_HANDLER` | stdout | DEBUG | `levelno < ERROR` (line 11) |
| `_ERROR_HANDLER` | stderr | ERROR | — |
| `_LOG_FILE_HANDLER` | `log/application.log` | DEBUG | rotating, 1 MB × 5 |

`setup_logger(name, level)` attaches all three to a named logger and returns it. **It does not
guard against duplicate attachment** — calling it twice with the same name duplicates every log
line. Currently each module calls it exactly once at import, so this does not bite today.

Log path override: `EMAIL_PRIORITY_CLASSIFIER_LOG` (line 17). `_LOG_FILE.parent.mkdir(exist_ok=True)`
(line 18) has **no `parents=True`**, so a multi-level override path raises at import.

Per-logger levels as set today: `main` INFO, `classifier_openai` INFO, `classifier_gptoss`
**DEBUG** — the local backend logs full request and response payloads (`classifier_gptoss.py:66,73`),
i.e. email content, into `log/application.log`.

---

## Exceptions

`exception.py` defines the hierarchy; nothing catches these types specifically anywhere.

```
EmailPriorityClassifierException
├── EmailPriorityClassifierGmailAPIException      raised by main.py:57, 174, 191
└── EmailPriorityClassifierClassifyException
    └── EmailPriorityClassifierOpenAIException    raised by both backends
```

`EmailPriorityClassifierClassifyException` is never raised directly. There is no
`...ConfigException`, though config errors are the most likely startup failure.

`main()` does not catch anything, and there is no `sys.exit` — **the process exit code is 0 on
every path that does not crash outright**, which makes failures invisible to cron.

---

## Known defects

Verified against the code, ordered by impact. A full remediation plan with implementation
sketches lives in `local/improvement_proposals.md` (untracked, local only).

1. **HTML is not stripped** — `type/classified_email_data.py:83`. `BeautifulSoup` is handed the
   *base64 string* rather than the decoded HTML. Base64 contains no `<`, so `get_text()` returns
   its input essentially unchanged, and `_decode_body` then base64-decodes it into raw HTML. The
   model receives tags, inline CSS, and `<script>` bodies, inside a 2500-character budget. Decode
   first, then parse; also `.decompose()` `script`/`style`/`head`/`noscript`, which `get_text()`
   does not remove.

2. **Nested MIME parts are not traversed** — `type/classified_email_data.py:69`. Only direct
   children of `payload["parts"]` are examined. The extremely common
   `multipart/mixed > multipart/alternative > text/plain` shape yields no match (the
   `multipart/alternative` wrapper fails the `text/` prefix test), and the model gets the string
   `"parts could not found."`. Mail with attachments is disproportionately affected, and mail
   with attachments is disproportionately important.

3. **Hardcoded UTF-8 decoding** — `type/classified_email_data.py:43`. This fails two different
   ways, measured against the same fixtures the new parser is tested with:
   - **Shift_JIS / EUC-JP** raise `UnicodeDecodeError`, which propagates to `main.py:106` and
     **drops the entire thread**.
   - **ISO-2022-JP does not raise at all.** It is 7-bit clean, so `.decode("utf-8")` succeeds and
     hands the model the raw escape sequences — `'\x1b$BK\\F|Cf$K...'`. Silent mojibake, which is
     worse than the loud failure: nothing in the logs says anything went wrong.

   The `charset` parameter is available on each part's `Content-Type` header; combine it with
   `errors="replace"` so a mislabeled charset degrades instead of skipping.

4. **Single-process path has no per-thread error handling** — `main.py:157`. `classifier.calc()`
   is called bare; the exception unwinds to `main.py:173` and is re-raised as
   `EmailPriorityClassifierGmailAPIException("Some error occurred while listing threads.")`,
   aborting the run. Behavior therefore differs between `concurrency: 1` and `concurrency: 2`,
   and the error message is actively misleading. Route both paths through `_classify_worker`.

5. **Truncation drops the newest messages** — `classifier_openai.py:31`,
   `classifier_gptoss.py:48`. Gmail returns messages oldest-first, and the slice takes the head,
   so the most recent exchange — the part that determines urgency — is what gets cut. The cut
   also lands mid-JSON.

6. **No retries anywhere.** A single Gmail 429/5xx or OpenAI 429 loses that thread.
   `execute(num_retries=...)` and `OpenAI(max_retries=..., timeout=...)` cover both sides with no
   new dependencies.

7. **`labelID` / `priorityLabels` are unvalidated duplicates.** See "Configuration".

8. **`token.pickle` uses `pickle`** — `gmail_credentials.py:21, 31`. `pickle.load` on a file an
   attacker can write is arbitrary code execution. `Credentials.from_authorized_user_file` /
   `creds.to_json()` are drop-in replacements. Also, `_SCOPES` (lines 8-12) requests
   `gmail.readonly` alongside `gmail.modify`, which subsumes it.

9. **`main.py:75` type annotation lies.** `maxResults=min(500, max_threads)` would `TypeError`
   if `max_threads` were `None`, but `assert_positive` rejects `None` first. The parameter should
   be typed `int`, not `int | None`.

Documentation gaps fixed in the current `README.md`, listed here so they are not re-introduced:
`client_secrets.json` lives in `secrets/`, not the project root; `.env.example` is missing
`LOCAL_LM_PORT`, `DEV_NOT_MODIFY`, and `EMAIL_PRIORITY_CLASSIFIER_LOG`; and `prompts/` is
git-ignored, so the `gpt-oss` backend cannot run from a fresh clone.

---

## Conventions

**Naming.** `epc` is the abbreviation of *EmailPriorityClassifier*. It is the Python package
(`src/epc/`, imported as `from epc.gmail.mime import …`), the CLI command (`epc labels`), and the
environment-variable prefix (`EPC__GMAIL__MAX_THREADS`). The distribution keeps the full name,
`email-priority-classifier`, because that is what appears in a lockfile and on an index.

The short form exists because the legacy package produced imports like
`email_priority_classifier.classifier.classifier_openai`, and because the CLI name is typed by
hand many times a day. Once Terraform lands, `epc` also becomes the prefix for resource names,
the ECR repository, the SSM parameter paths and the CloudWatch log groups — renaming it after
that point is no longer a find-and-replace.

**Language.** Code comments and docstrings are Japanese. Log messages, exception messages, and
all documentation are English. Keep the split.

**Commit messages.** `type: summary`, where type is one of `add:`, `fix:`, `update:`,
`refactor:`, `remove:`. Summaries are usually Japanese. Do not add attribution or co-author
trailers.

**Branches.** `main` is the released state; `develop` is the integration branch; features branch
from `develop`. Two GitHub Actions workflows enforce this by auto-closing PRs: any PR *from*
`main` (`close_pr_from_main.yml`), and any PR *to* `main` not from `develop`/`hotfix`/`hotfix/*`
(`close_pr_to_main.yml`). Never commit directly to `main`.

**Untracked-but-present files.** `test.py` and `local/` are excluded via `.git/info/exclude`
(symlinked as `personal.gitignore`), not `.gitignore`. `config.yml`, `.env`, `prompts/`,
`secrets/**`, and `log/` are excluded via `.gitignore`. These exist in the working tree and are
readable — do not assume a file is absent just because `git ls-files` does not list it, and do
not commit any of them.

`test.py` is a scratch script for looking up Gmail label IDs, not a test. If a real test suite
is added under `tests/`, delete it.

---

## Refactoring guidance

**Sequence matters.** Fix defects 1-4 before touching prompts or architecture: they all corrupt
what the model sees, so any accuracy measurement taken before fixing them is measuring noise.
Their blast radius is almost entirely `type/classified_email_data.py`, which makes them cheap
and independently verifiable.

**Add tests at the parsing boundary first.** Defects 1, 2, and 3 are all "given this Gmail JSON,
what text comes out" bugs, and all three would have been caught by a handful of fixtures against
`ClassifiedEmailData.get_data`. `ClassifiedEmailData`, `_encode_thread_messages`, and
`load_config` are fully testable with zero network access.

**Treat label removal as a separate class of change.** Everything today is additive, which is
what makes the tool safe to run repeatedly and safe to interrupt. The first feature that calls
`removeLabelIds` — moving threads between Gmail tabs is the obvious candidate — breaks that
property and needs a working dry-run path and a decision log before it ships. In particular it
must never apply to the already-labeled fold-in path (`main.py:114-119`), or it will undo the
user's manual corrections on every run.

**Do not change the truncation budgets or the prompt's priority semantics** while fixing
mechanics. Both are tuned, and conflating a tuning change with a correctness fix makes the
result unattributable.

**The `classify()` decomposition is the structural win.** Pulling page iteration, thread
hydration, batch dispatch, and rate limiting apart — and unifying the serial and parallel paths
— removes roughly half the function and fixes defect 4 as a side effect.
