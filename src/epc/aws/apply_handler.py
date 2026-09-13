"""Lambda handler for the apply half.

Reads mutations from SQS and writes the labels they ask for. It classifies
nothing, so it needs no model credential, no prompt, and no `config.yml` — the
mutation already carries the label IDs, decided by the run that produced it.

That is the import boundary paying off: `tests/unit/test_layering.py` asserts
that `actions/` and `dispatch/` never reach `classify/`, which is what keeps
this deployment to `google-api-python-client` plus `boto3`.

Configuration is three environment variables. Deliberately not the full
`Settings` object: requiring `labels` here would mean carrying the classifier's
configuration into a process that cannot classify.
"""

import os
from typing import Any

from epc.dispatch.applier import MutationApplier
from epc.dispatch.sqs import parse_mutations
from epc.gmail.auth import SsmCredentialStore, get_credentials
from epc.gmail.client import GmailClient
from epc.logging import configure_logging, get_logger

CREDENTIALS_PARAMETER_ENV = "EPC__CREDENTIALS__PARAMETER_NAME"
REGION_ENV = "EPC__AWS_REGION"
LOG_LEVEL_ENV = "EPC__OBSERVABILITY__LOG_LEVEL"

logger = get_logger(__name__)


def _client() -> GmailClient:
    parameter = os.environ[CREDENTIALS_PARAMETER_ENV]
    region = os.environ.get(REGION_ENV) or None
    return GmailClient(get_credentials(SsmCredentialStore(parameter, region=region)))


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Apply one batch of mutations.

    Returns partial batch failures rather than raising, so one unusable message
    does not drag nine good ones back through the queue. A message reported as
    failed becomes visible again and, after its retries, reaches the
    dead-letter queue where a person can look at it.
    """
    configure_logging(level=os.environ.get(LOG_LEVEL_ENV, "INFO"), json_output=True)

    records = event.get("Records") or []
    if not records:
        return {"batchItemFailures": []}

    bodies = [str(record.get("body") or "") for record in records]
    mutations = parse_mutations(bodies)

    unparseable = [
        {"itemIdentifier": str(record.get("messageId"))}
        for record, body in zip(records, bodies, strict=True)
        if not _is_parseable(body)
    ]

    if not mutations:
        logger.warning("no usable mutations in batch", received=len(records))
        return {"batchItemFailures": unparseable}

    report = MutationApplier(_client()).apply(mutations)
    logger.info(
        "applied batch",
        received=len(records),
        applied=report.applied,
        failed=report.failed,
        api_calls=report.api_calls,
    )

    if not report.had_failures:
        return {"batchItemFailures": unparseable}

    # `batchModify` groups threads by the change they ask for, so a failure
    # names a group rather than a message. Without a per-message result the
    # honest thing is to return the whole batch: re-applying a label is a
    # no-op, so a redelivery costs an API call, while dropping a failure
    # silently loses the label.
    logger.warning("batch had failures; returning it for redelivery", failures=report.failures)
    return {"batchItemFailures": [{"itemIdentifier": str(r.get("messageId"))} for r in records]}


def _is_parseable(body: str) -> bool:
    return bool(parse_mutations([body]))
