"""Loading, versioning and rendering the prompts.

The prompt *is* the priority policy, so it is kept where policy belongs: in the
repository, versioned, diffable and reviewable. The previous implementation put
it either in a vendor dashboard, invisible to git, or in an ignored directory
that a fresh clone did not have — neither could be reviewed or tested.

That leaves one problem. Real classification rules are personal ("mail from my
university is never P3"), and this repository is public. So the split is:

* ``prompts/*.md`` — generic, committed, publishable;
* ``policy.yml`` — personal, git-ignored, rendered into the system prompt at run
  time.

Rendering is plain substitution rather than a template engine. What is needed is
two placeholders; a dependency that can execute arbitrary expressions inside a
string built partly from untrusted input is not a trade worth making.
"""

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from epc.classify.budget import ThreadPayload
from epc.errors import ConfigError
from epc.security.sanitize import escape_delimiter

DEFAULT_PROMPT_DIR = Path("prompts")
DEFAULT_POLICY_FILENAME = "policy.yml"

_FRONTMATTER_FENCE = "---"
_DELIMITER_OPEN = '<untrusted_email_content nonce="{nonce}">'
_DELIMITER_CLOSE = '</untrusted_email_content nonce="{nonce}">'
# The fixed part an attacker could guess. The nonce itself cannot be.
_DELIMITER_STEM = "untrusted_email_content"


class PromptMetadata(BaseModel):
    """Frontmatter. `version` is recorded with every classification, so a change
    in accuracy can be attributed to a change in the prompt."""

    version: str = "0"
    updated: str | None = None
    changelog: list[str] = Field(default_factory=list)


class Prompt(BaseModel):
    metadata: PromptMetadata = Field(default_factory=PromptMetadata)
    body: str = ""


class Policy(BaseModel):
    """Personal guidance, rendered into the system prompt.

    Advice to the model, not a rule the code enforces. The classifier still
    returns nothing but a priority, and actions are still decided by config —
    personal policy does not get to widen what the model can do.
    """

    version: int = 1
    guidance: list[str] = Field(default_factory=list)

    def render(self) -> str:
        if not self.guidance:
            return ""
        lines = ["## Additional guidance from the recipient", ""]
        lines += [f"- {item}" for item in self.guidance]
        return "\n".join(lines)


def parse_prompt(text: str) -> Prompt:
    """Split YAML frontmatter from the prompt body."""
    if not text.startswith(_FRONTMATTER_FENCE):
        return Prompt(body=text.strip())

    parts = text.split(_FRONTMATTER_FENCE, 2)
    if len(parts) < 3:
        return Prompt(body=text.strip())

    try:
        raw = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"prompt frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("prompt frontmatter must be a mapping")

    return Prompt(metadata=PromptMetadata(**raw), body=parts[2].strip())


def load_prompt(path: Path) -> Prompt:
    if not path.is_file():
        raise ConfigError(f"prompt file not found: {path}")
    return parse_prompt(path.read_text(encoding="utf-8"))


# The whole of `policy.yml`, as an environment variable. Delivered on ECS the
# same way as `EPC_CONFIG_YAML`; see `epc.settings`.
POLICY_ENV = "EPC_POLICY_YAML"


def load_policy(path: Path) -> Policy:
    """Load personal guidance. An absent file is normal, not an error."""
    if not path.is_file():
        return Policy()
    return load_policy_text(path.read_text(encoding="utf-8"), source=str(path))


def load_policy_text(text: str, *, source: str = POLICY_ENV) -> Policy:
    """Personal guidance from YAML text rather than a file."""
    from epc.settings import parse_yaml_mapping

    return Policy(**parse_yaml_mapping(text, source=source))


class RenderedPrompt(BaseModel):
    """Everything needed to make one request, plus what to record about it."""

    system: str
    user: str
    prompt_version: str
    nonce: str


class PromptRenderer:
    """Renders the committed prompts together with personal policy."""

    def __init__(self, system: Prompt, user: Prompt, policy: Policy | None = None) -> None:
        self._system = system
        self._user = user
        self._policy = policy or Policy()

    @classmethod
    def load(
        cls,
        prompt_dir: Path = DEFAULT_PROMPT_DIR,
        policy_path: Path | None = None,
        *,
        policy_text: str | None = None,
    ) -> PromptRenderer:
        """The committed prompts, with policy from `policy_text` when given and
        from `policy_path` otherwise."""
        policy = (
            load_policy_text(policy_text)
            if policy_text is not None
            else load_policy(policy_path or Path(DEFAULT_POLICY_FILENAME))
        )
        return cls(
            system=load_prompt(prompt_dir / "system.md"),
            user=load_prompt(prompt_dir / "user.md"),
            policy=policy,
        )

    @property
    def version(self) -> str:
        return self._system.metadata.version

    @property
    def fingerprint(self) -> str:
        """A digest of everything this renderer puts in front of the model.

        Unlike `version`, which a person bumps in the prompt's front matter,
        this changes on any edit — including one to `policy.yml` — and is what
        tells a failure recorded under the old prompt apart from the new.
        """
        material = "\x00".join([self._system.body, self._user.body, self._policy.render()])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def render(self, payload: ThreadPayload) -> RenderedPrompt:
        """Build one request.

        The payload is serialised, any forged copy of the delimiter inside it is
        broken up, and the result is wrapped in a delimiter carrying a fresh
        random nonce. Guessing the nonce is not feasible, so content cannot
        close the block it is inside.

        The injection signal is deliberately left out of what the model sees.
        Telling it "this thread looks hostile" would make that judgement itself
        a thing worth attacking.
        """
        nonce = secrets.token_hex(8)
        body = payload.model_dump_json(exclude={"injection"}, indent=2)
        body = escape_delimiter(body, _DELIMITER_STEM)

        system = self._system.body.replace("{{policy}}", self._policy.render()).strip()
        user = self._user.body.replace("{{nonce}}", nonce).replace("{{payload}}", body).strip()
        return RenderedPrompt(
            system=system,
            user=user,
            prompt_version=self.version,
            nonce=nonce,
        )


def response_json_schema() -> dict[str, Any]:
    """The strict JSON schema the backends constrain the model to.

    This is the whole of the model's output surface. It is why a successful
    prompt injection can only mislabel one thread: there is no field here for
    naming a label, requesting an action, or calling anything.
    """
    return {
        "type": "object",
        "properties": {
            "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
            "signals": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["priority", "reason", "confidence", "signals"],
        "additionalProperties": False,
    }


def schema_as_text() -> str:
    """The schema rendered for backends that cannot enforce one natively."""
    return json.dumps(response_json_schema(), indent=2)
