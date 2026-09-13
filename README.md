# EmailPriorityClassifier

EmailPriorityClassifier intelligently evaluates incoming emails and threads to determine how urgently they require user attention.

By analyzing subjects, message bodies, Gmail's native classifications, and user-assigned labels, it assigns each thread a clear Priority label (P1–P3) directly in Gmail.
The ability to freely configure system prompts allows for flexible labeling and priority logic (e.g., always assigning emails from specific topics or senders to P1).

This application supports both OpenAI models and OpenAI-compatible local LLMs such as those running through LM Studio, allowing users to choose between high-efficiency cloud models and fully private, on-device inference.
This flexibility enables streamlined inbox triage while meeting diverse performance and privacy requirements.

<img width="756" height="411" alt="email_priority_classifier" src="https://github.com/user-attachments/assets/ed5db17a-f8bb-45d2-94da-9ceae2ae6298" />

---

## Table of Contents

- [Concept](#concept)
- [How It Works](#how-it-works)
- [Design Principles](#design-principles)
- [Priority Levels](#priority-levels)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Project Layout](#project-layout)
- [Privacy and Data Handling](#privacy-and-data-handling)
- [Operational Notes](#operational-notes)
- [Known Limitations](#known-limitations)
- [Contributing](#contributing)

---

## Concept

Inbox triage is a ranking problem, not a filtering problem. Traditional Gmail filters answer
"does this match a rule?", which forces you to enumerate every sender and keyword you care
about in advance. This tool instead answers a different question: **"if I only had time for
a few emails today, which ones would they be?"**

The answer is expressed as a single Gmail label per thread. Nothing is hidden, archived, or
deleted — the mailbox stays exactly as it was, with one extra dimension of information that
you can sort, search, and build filters on top of.

The judgment itself is delegated to an LLM, and the judgment *policy* lives in a prompt that
you own and edit. Adjusting how aggressively academic mail, invoices, or a specific customer
gets escalated is a prompt change, not a code change.

---

## How It Works

One invocation is one batch. The process starts, triages what it finds, applies labels, and exits.

```
  config.yml + .env
         │
         ▼
  ┌─────────────────┐
  │ Load config     │  Which labels mean P1/P2/P3, how many threads,
  │ Pick classifier │  how fast, and which LLM backend to use
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ Gmail OAuth     │  Desktop-app OAuth flow, token cached locally
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ List threads    │  in:inbox, excluding anything already labeled P1/P2/P3
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ Fetch + parse   │  Full thread payload → subject, body text,
  │ each thread     │  human-readable label names
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ Classify        │  Thread JSON → LLM → {"priority": "P1", "reason": "..."}
  │ (concurrent)    │  Run in parallel, throttled to your rate limit
  └────────┬────────┘
           ▼
  ┌─────────────────┐
  │ Apply labels    │  Add the matching priority label to each thread
  └─────────────────┘
```

The unit of classification is the **thread**, not the individual message. A reply changes the
urgency of the whole conversation, so the whole conversation is scored together and the label
is applied to the thread.

---

## Design Principles

**Gmail is the database.**
There is no local store of classification state. The presence of a priority label on a thread
*is* the record that it has been handled. This keeps the tool stateless, makes it safe to
interrupt at any point, and means results are visible everywhere Gmail is — phone, web, and
desktop clients alike.

**Re-running is always safe.**
Already-labeled threads are excluded by the Gmail search query itself, and re-checked again
after fetching in case a label was added mid-run. A crashed or killed run costs at most the
work in flight; the next run picks up where it left off without duplicating LLM calls.

**Additive changes only.**
The tool only ever *adds* a priority label. It never removes labels, never archives, never
deletes, and never moves mail between tabs. If you disagree with a classification, changing
the label by hand is permanent — the tool will not overwrite your decision on a later run.

**Policy lives in the prompt, not the code.**
Priority rules — which domains are always urgent, how to treat newsletters, what to do when
the content is ambiguous — are expressed in natural language in a prompt you control. The
Python code decides *what to send* and *what to do with the answer*, never *what counts as
urgent*.

**The LLM backend is swappable.**
Classifiers sit behind one small interface: thread messages in, priority out. Switching
between a hosted OpenAI model and a locally hosted open-weight model is a one-line config
change, with no other part of the system aware of the difference.

**Cost is a first-class constraint.**
Inboxes are large and LLM calls are not free. Thread payloads are truncated to a fixed
character budget before being sent, concurrency and requests-per-minute are configurable, and
a hard cap on threads per run bounds the worst-case spend of any single invocation.

---

## Priority Levels

| Priority | Meaning | Typical content |
|---|---|---|
| **P1** | Needs attention now | Deadlines within ~48 hours, incidents, security and payment issues, urgent requests from people who matter |
| **P2** | Needs a reply, but not today | Ordinary work correspondence, scheduling, questions, non-urgent personal mail |
| **P3** | Can be deferred or ignored | Promotions, newsletters, social notifications, routine automated updates |

The shipped prompt biases toward **P2 when uncertain** — under-triaging an important mail to
P3 is far more costly than over-triaging a newsletter to P2. It also enforces a floor: mail
that appears to be individually written by a human is never P3.

These definitions are conventions of the prompt, not of the code. The code only requires that
the model return one of the three names.

---

## Requirements

- Python 3.14 (pyenv recommended)
- [uv](https://docs.astral.sh/uv/)
- A Google Cloud project with the Gmail API enabled and an OAuth client of type **Desktop app**
- One of:
    - An OpenAI API key, or
    - AWS credentials with Bedrock access, or
    - A local OpenAI-compatible server (e.g. LM Studio) serving an open-weight model

---

## Installation

1. (If you use pyenv, and do not have Python 3.14 yet)
   ```bash
   pyenv install 3.14.5
   ```

2. Clone this repository
   ```bash
   git clone https://github.com/KO1231/EmailPriorityClassifier.git
   cd EmailPriorityClassifier
   ```

3. Install
   ```bash
   make install
   ```

4. Prepare configuration
    - Copy `config.yml.example` to `config.yml` and fill it in.
    - Copy `policy.yml.example` to `policy.yml` if you have rules of your own — it is
      git-ignored, which is what lets the prompts in `prompts/` stay publishable.
    - Put your Gmail OAuth client credentials (Desktop application) at
      **`secrets/client_secrets.json`**.
    - Put your model credentials in `.env`, based on `.env.example`.

5. Create the three priority labels in Gmail (for example `#/P1`, `#/P2`, `#/P3`) and
   put their **names** in `config.yml`. The internal IDs are resolved from the mailbox
   at startup, so there is nothing to keep in sync by hand.

6. Authorise
   ```bash
   uv run epc login
   uv run epc labels   # confirms the configured names resolve
   ```

## Configuration

`config.yml` holds policy and no secrets, so it can be read, diffed and reviewed
freely. `config.yml.example` is the annotated reference; the shape is:

```yaml
labels:                     # display names only — IDs are resolved at startup
  p1: "#/P1"
  p2: "#/P2"
  p3: "#/P3"

gmail:
  query: "in:inbox"
  extra_query: "newer_than:14d -in:chats"   # the largest single cost lever
  max_threads: 1500
  incremental: true                          # resume from the stored checkpoint

llm:
  backend: openai           # openai | bedrock | local
  model: gpt-5.4-mini
  reasoning_effort: low
  concurrency: 15
  requests_per_min: 120

actions:                    # what happens once a priority is known
  rules:
    - when:   { priority: P1 }
      unless: { any_label: [SPAM, TRASH] }
      do:     [add_star, move_to_primary, mark_important]

dry_run: false              # overrides dispatch: nothing is written to Gmail
```

Any value can be overridden by an `EPC__`-prefixed environment variable, with `__`
between levels: `EPC__GMAIL__MAX_THREADS=50`. Precedence, highest first: command line,
environment, file, defaults. Unknown keys are an error rather than being ignored, so a
typo fails loudly instead of doing nothing.

### Secrets

Never in `config.yml`. `OPENAI_API_KEY` and friends come from the environment (see
`.env.example`); Gmail credentials are stored by the credential backend — a local JSON
file today, a secret store on AWS.

An OpenAI key scoped to `api.responses.write` and `api.responses.read` is sufficient.
The broader `model.request` scope is not needed.

### Prompts and personal rules

The priority policy is the prompt, so it lives in the repository where it can be
reviewed and diffed. Your *own* rules do not belong in a public repository, so they go
in `policy.yml` (git-ignored) and are rendered into the system prompt at run time:

```yaml
guidance:
  - "Mail from university or government domains is never P3."
  - "Newsletters from example-vendor.com are P3 even when the subject says URGENT."
```

`epc prompt render` prints the result, so you can see exactly what is sent.

## Usage

Plan a run without touching anything:

```bash
uv run --env-file .env epc run --dry-run
```

That writes every intended change to `log/mutations.jsonl` and applies none of them.
Read it, and when you are happy, apply exactly what you read:

```bash
uv run epc apply log/mutations.jsonl
```

Or, once you trust the configuration, run it directly:

```bash
uv run --env-file .env epc run
```

Useful along the way:

```bash
uv run epc config validate    # parse and print the configuration; no network
uv run epc prompt render      # the exact prompt a thread produces, policy included
uv run epc run --limit 20     # cap a run while trying things out
```

Exit codes are `0` clean, `1` fatal (configuration, credentials), `2` partial — some
threads were lost but the run completed. Schedule it with cron, launchd or a systemd
timer; it is a batch job, not a daemon.

## Project Layout

```
config.yml            Runtime configuration (git-ignored)
policy.yml            Your own classification rules (git-ignored)
prompts/              Generic, committed prompt templates
secrets/              OAuth client secrets and cached token (git-ignored)

src/epc/
├── cli.py            Entry point
├── settings.py       Configuration schema and layering
├── pipeline.py       The run: list, fetch, classify, plan, dispatch
├── ratelimit.py      Request pacing
├── state.py          The historyId checkpoint between runs
├── report.py         Classification history
├── gmail/            auth · client · query · mime · labels · models
├── classify/         budget · prompt · base · openai/bedrock/local backends
├── security/         sanitize · detect
├── actions/          model · rules · planner
└── dispatch/         sink · applier

tests/                unit · integration · injection · fixtures (synthetic only)
```

## Privacy and Data Handling

**What leaves your machine.** With `backend: openai`, a budgeted extract of each thread
— headers, and body text trimmed of quoted history — is sent to the OpenAI API with
`store: false`, so the provider is not asked to retain it. With `backend: bedrock` it
goes to AWS instead. With `backend: local`, nothing leaves the machine at all.

**What is stored.** The OAuth token (`secrets/token.json`, user-readable only, holding
the durable credential fields and not the short-lived access token), the application
log, and — only if you turn it on — a classification history. That history records a
*digest* of each subject and the sender's *domain*, never message content: it outlives
the run and is the kind of file that ends up in a backup.

**Gmail permissions requested.** `gmail.modify`, and nothing else. It subsumes read
access and label management. Note that the scope technically permits archiving and
trashing mail; this tool only does so if you write a rule that says to, and
`allow_destructive` is off by default.

## Operational Notes

- **Cost scales with inbox size, not with new mail.** The first run over a large inbox will
  classify up to `maxThreads` threads, including old ones. Consider lowering `maxThreads`
  for the first few runs to see costs before committing to a full sweep.
- **A run is interruptible.** Labels are written after classification completes, so killing
  the process mid-classification loses the work in flight but corrupts nothing.
- **Failures are per-thread.** A thread that fails to parse or classify is logged and skipped;
  the rest of the batch continues.
- **Manual corrections stick.** Relabeling a thread by hand excludes it from all future runs.

---

## Status

The pipeline runs locally and in a container, and the AWS side is described in
Terraform: an ECR image, a scheduled Fargate task that classifies, a FIFO queue, and a
Lambda that applies the resulting label changes.

Not built yet: an evaluation harness, so prompt changes can be measured rather than
guessed at. It is worth having once the priority criteria are being actively tuned.

## Contributing

Branching model:

- `main` — released state. Pull requests targeting `main` are automatically closed unless they
  originate from `develop`, `hotfix`, or `hotfix/*`.
- `develop` — integration branch. Feature work branches from here and merges back here.
- Pull requests *from* `main` are automatically closed.

Both rules are enforced by GitHub Actions workflows in `.github/workflows/`.

Commit messages follow a `type: summary` convention — `add:`, `fix:`, `update:`, `refactor:`,
`remove:`.
