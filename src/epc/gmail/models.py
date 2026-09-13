"""The parsed form of a Gmail message, and the thread it belongs to.

These are what the classifier and the action planner consume. Everything the
old implementation discarded before the model ever saw it — the sender, the
recipients, the bulk-mail headers, the raw label IDs — is preserved here.
"""

from datetime import datetime

from pydantic import BaseModel, Field

# Gmail system labels naming the tab a message sits in. A message carries at
# most one. "Primary" is the absence of the others, not a label of its own.
NON_PRIMARY_CATEGORIES = frozenset(
    {
        "CATEGORY_PROMOTIONS",
        "CATEGORY_SOCIAL",
        "CATEGORY_UPDATES",
        "CATEGORY_FORUMS",
    }
)


class Attachment(BaseModel):
    """An attached part. Name, type and size only — never content."""

    filename: str
    mime_type: str
    size: int


class AuthenticationResults(BaseModel):
    """SPF / DKIM / DMARC verdicts recorded by the receiving server.

    Lets the prompt distrust a sender that merely *claims* to be someone.
    """

    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None


class EmailMessage(BaseModel):
    """One message, parsed from Gmail's `format=full` representation."""

    message_id: str
    thread_id: str
    date: datetime
    size_estimate: int

    # Raw Gmail label IDs, kept verbatim. Translating these to display names
    # here is what made tab membership unknowable in the old implementation,
    # and the action planner needs CATEGORY_* to move a thread to Primary.
    label_ids: list[str] = Field(default_factory=list)

    subject: str = ""
    sender: str = ""
    sender_name: str = ""
    sender_domain: str = ""
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    reply_to: str | None = None

    # Bulk-mail signals. Their presence separates a newsletter from a person
    # far more reliably than the body does.
    list_id: str | None = None
    has_list_unsubscribe: bool = False
    auto_submitted: str | None = None
    precedence: str | None = None

    authentication: AuthenticationResults = Field(default_factory=AuthenticationResults)

    body: str = ""
    # Which part won: "text/plain", "text/html", or None when no text part was
    # found at all (an attachment-only message, say).
    body_mime_type: str | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    # Elements an inline style hid from the reader, dropped during extraction.
    # Recorded because "this message contained text nobody sees" is a signal.
    hidden_elements_removed: int = 0

    @property
    def recipient_count(self) -> int:
        return len(self.to) + len(self.cc)

    @property
    def looks_bulk(self) -> bool:
        """Header-only heuristic. A signal for the prompt, not a verdict."""
        return bool(self.list_id) or self.has_list_unsubscribe or self.auto_submitted is not None


class EmailThread(BaseModel):
    """A Gmail thread. Messages stay in Gmail's order, which is oldest first."""

    thread_id: str
    messages: list[EmailMessage] = Field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.messages[0].subject if self.messages else ""

    @property
    def latest(self) -> EmailMessage | None:
        """The newest message — the one that decides whether action is needed."""
        return self.messages[-1] if self.messages else None

    @property
    def label_ids(self) -> set[str]:
        return {label for message in self.messages for label in message.label_ids}

    @property
    def message_ids(self) -> list[str]:
        """Every message ID, which is what `messages.batchModify` operates on."""
        return [m.message_id for m in self.messages]

    @property
    def non_primary_categories(self) -> set[str]:
        """Tab labels to remove in order to move this thread to Primary."""
        return self.label_ids & NON_PRIMARY_CATEGORIES
