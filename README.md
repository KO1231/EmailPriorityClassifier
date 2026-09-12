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

- Python 3.13.5
    - pyenv (recommended)
    - pipenv
- A Google Cloud project with the Gmail API enabled and an OAuth client of type **Desktop app**
- One of:
    - An OpenAI API key, or
    - A local OpenAI-compatible server (e.g. LM Studio) serving an open-weight model

---

## Installation

1. (If you use pyenv, and did not install Python 3.13.5 yet)
   ```bash
   pyenv install 3.13.5
   ```

2. Clone this repository
   ```bash
   git clone https://github.com/KO1231/EmailPriorityClassifier.git
   cd EmailPriorityClassifier
   ```

3. Install dependencies using pipenv
   ```bash
   pip3 install pipenv
   pipenv install
   ```

4. Prepare secrets and configuration
    - Create a `.env` file in the project root, based on `.env.example`.
    - Create a `config.yml` file in the project root, based on `config.yml.example`.
    - Place your Gmail API OAuth client credentials (Desktop application) at
      **`secrets/client_secrets.json`**.

5. Create the three priority labels in Gmail (for example `#/P1`, `#/P2`, `#/P3`), then put
   both their display names and their internal label IDs into `config.yml`. See
   [Configuration](#configuration) for how to look the IDs up.

6. Create OAuth2 tokens
   ```bash
   pipenv run login_google
   ```
   A browser window opens for consent. The resulting token is cached at `secrets/token.pickle`
   and refreshed automatically on later runs.

---

## Configuration

### `config.yml`

```yaml
labelID:                      # Gmail internal label IDs (Label_...)
  P1: "Label_????"
  P2: "Label_????"
  P3: "Label_????"

priorityLabels:               # Display names of the same three labels
  P1: "#/P1"
  P2: "#/P2"
  P3: "#/P3"

maxThreads: 1500              # Hard cap on threads classified per run
concurrency: 15               # Parallel LLM requests
requestsPerMin: 120           # LLM request rate limit
model: "openai"               # "openai" | "gpt-oss"
```

| Key | Purpose |
|---|---|
| `labelID` | Used when **writing** labels back to Gmail. Must be the internal `Label_...` IDs. |
| `priorityLabels` | Used when **reading** — both to exclude already-classified threads from the search query and to detect existing labels after fetching. Must be the display names of the same three labels. |
| `maxThreads` | Upper bound on threads processed in one invocation. Also bounds the cost of a run. |
| `concurrency` | Number of classification requests in flight at once. Set to `1` to disable parallelism. |
| `requestsPerMin` | Throttle applied between batches, to stay inside your provider's rate limit. |
| `model` | Which classifier backend to load. |

> **Both `labelID` and `priorityLabels` must describe the same three labels.** They are not
> cross-checked at startup — a mismatch results in threads being classified as one priority
> and labeled as another. To look up your label IDs, run
> `pipenv run python -c "from googleapiclient.discovery import build; from email_priority_classifier.gmail_credentials import get_credential; import json; print(json.dumps({l['name']: l['id'] for l in build('gmail','v1',credentials=get_credential('secrets/client_secrets.json','secrets/token.pickle')).users().labels().list(userId='me').execute()['labels']}, indent=2, ensure_ascii=False))"`.

### `.env`

```bash
# Required when model: "openai"
OPENAI_API_KEY=sk-????
OPENAI_PROMPT_ID=pmpt_????          # A saved prompt in the OpenAI dashboard
OPENAI_PROMPT_VERSION=????

# Required when model: "gpt-oss"
LOCAL_LM_PORT=1234                  # Port of your local OpenAI-compatible server

# Optional
DEV_NOT_MODIFY=true                 # Dry run: classify but never write labels to Gmail
EMAIL_PRIORITY_CLASSIFIER_LOG=...   # Override the log file path (default: log/application.log)
```

`.env` is loaded automatically by `pipenv run`. If you invoke `python main.py` outside pipenv,
you must export these variables yourself.

### Prompts

The two backends source their prompt differently:

- **`openai`** — the prompt is stored server-side in the OpenAI dashboard and referenced by
  `OPENAI_PROMPT_ID` / `OPENAI_PROMPT_VERSION`. It must accept the variables
  `thread_subject` and `thread_messages`, and must return a JSON object containing a
  `priority` field whose value is `"P1"`, `"P2"`, or `"P3"`.
- **`gpt-oss`** — the prompt is read from `prompts/gptoss_system_prompt.txt` and
  `prompts/gptoss_user_prompt.txt`, with `{{thread_subject}}` and `{{thread_messages}}`
  substituted at request time.

> The `prompts/` directory is git-ignored, so it is **not** present in a fresh clone. The
> `gpt-oss` backend will fail with `FileNotFoundError` until you supply your own templates.

---

## Usage

Dry run first — classify everything, write nothing:

```bash
DEV_NOT_MODIFY=true pipenv run start
```

Then, once the results look right:

```bash
pipenv run start
```

Progress, per-thread failures, and a line per label written are logged to stdout and to
`log/application.log` (rotating, 1 MB × 5 backups).

The tool is a batch job, not a daemon. For continuous triage, schedule it with cron, launchd,
or a systemd timer.

---

## Project Layout

```
main.py                                   Entry point: fetch → classify → label
config.yml                                Runtime configuration (git-ignored)
.env                                      Secrets and environment (git-ignored)
prompts/                                  Prompt templates for the local backend (git-ignored)
secrets/                                  OAuth client secrets and cached token (git-ignored)
log/                                      Rotating application log (git-ignored)

email_priority_classifier/
├── config.py                             config.yml → typed config object
├── gmail_credentials.py                  OAuth flow and token caching
├── exception.py                          Exception hierarchy
├── classifier/
│   ├── email_priority_classifier.py      Abstract classifier interface
│   ├── classifier_openai.py              Hosted OpenAI backend
│   └── classifier_gptoss.py              Local OpenAI-compatible backend
├── type/
│   ├── priority.py                       P1 / P2 / P3 enum
│   └── classified_email_data.py          Gmail message → LLM-ready representation
└── util/
    ├── logger_util.py                    Shared logging setup
    └── assert_util.py                    Small validation helpers
```

---

## Privacy and Data Handling

**What leaves your machine.** With `model: "openai"`, the subject line and a truncated
extract of the thread body are sent to the OpenAI API, along with the thread's Gmail label
names. With `model: "gpt-oss"`, nothing leaves the machine — the request goes to a local
server over loopback.

**What is stored.** Nothing but the OAuth token (`secrets/token.pickle`) and the application
log. No message bodies are persisted; the log records thread IDs and priorities, not content.

**Gmail permissions requested.** `gmail.readonly`, `gmail.labels`, and `gmail.modify`. The
`modify` scope is what allows labels to be applied. Note that this scope technically permits
deleting and archiving mail — this tool never does either, but you are granting the capability.

---

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

## Known Limitations

The project is in active development, and the following are known gaps rather than
intentional design:

- **Sender headers are not sent to the model.** Only subject, body text, and label names are.
  "Who sent this" — arguably the strongest signal available — is currently invisible to the
  classifier unless it appears in the body.
- **HTML-only mail is sent with markup intact.** Bodies without a `text/plain` alternative
  are not reliably stripped, which wastes the character budget on tags and scripts.
- **Nested MIME structures are not traversed.** Mail with attachments often nests the real
  body one level deeper than the parser looks, and can end up classified with no body at all.
- **Non-UTF-8 bodies fail.** Legacy Japanese encodings (ISO-2022-JP, Shift_JIS) cause the
  thread to be skipped.
- **Truncation drops the newest messages.** Long threads are cut from the end, which is where
  the most decision-relevant content lives.
- **No retries.** A transient Gmail or LLM error costs that thread for the run.
- **No tests.**

A detailed remediation plan exists in `local/improvement_proposals.md` (not committed).

---

## Contributing

Branching model:

- `main` — released state. Pull requests targeting `main` are automatically closed unless they
  originate from `develop`, `hotfix`, or `hotfix/*`.
- `develop` — integration branch. Feature work branches from here and merges back here.
- Pull requests *from* `main` are automatically closed.

Both rules are enforced by GitHub Actions workflows in `.github/workflows/`.

Commit messages follow a `type: summary` convention — `add:`, `fix:`, `update:`, `refactor:`,
`remove:`.
