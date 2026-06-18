import json
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_REQUIRED_FIELDS = [
    "prompt_id",
    "version",
    "status",
    "owner",
    "created_at",
    "description",
    "template",
]


@dataclass
class PromptRecord:
    prompt_id: str
    version: str
    status: str
    owner: str
    created_at: str
    description: str
    template: str


def _load_file(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    missing = [field for field in _REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"Prompt file {path} is missing required fields: {missing}")
    return data


def _to_record(data: dict) -> PromptRecord:
    return PromptRecord(
        prompt_id=data["prompt_id"],
        version=data["version"],
        status=data["status"],
        owner=data["owner"],
        created_at=data["created_at"],
        description=data["description"],
        template=data["template"],
    )


def _version_number(version: str) -> int:
    m = re.match(r"v(\d+)$", version)
    return int(m.group(1)) if m else 0


def get_prompt(
    prompt_id: str,
    version: str | None = None,
    require_approved: bool = True,
    prompts_dir: Path | None = None,
) -> PromptRecord:
    """Return a PromptRecord from the registry.

    If version is omitted, returns the latest approved version (require_approved
    has no effect in this path — the scan is always approval-gated).

    If version is provided:
    - require_approved=True (default): raises ValueError when the loaded version's
      status is not "approved", blocking unapproved prompts from production.
    - require_approved=False: loads the version regardless of status, for use
      during development or testing of draft / deprecated prompts.

    Raises ValueError for unknown prompt_id, missing version file, or no approved
    version found when version is omitted.
    """
    base = prompts_dir if prompts_dir is not None else PROMPTS_DIR
    prompt_dir = base / prompt_id

    if not prompt_dir.exists():
        raise ValueError(f"Unknown prompt_id: '{prompt_id}'")

    if version is not None:
        path = prompt_dir / f"{version}.json"
        if not path.exists():
            raise ValueError(
                f"Version '{version}' not found for prompt_id '{prompt_id}'"
            )
        data = _load_file(path)
        if require_approved and data.get("status") != "approved":
            raise ValueError(
                f"Version '{version}' of '{prompt_id}' has status "
                f"'{data.get('status')}', not 'approved'. "
                f"Pass require_approved=False to load non-approved versions."
            )
        return _to_record(data)

    approved = []
    for path in sorted(prompt_dir.glob("*.json")):
        try:
            data = _load_file(path)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        if data.get("status") == "approved":
            approved.append(data)

    if not approved:
        raise ValueError(f"No approved version found for prompt_id '{prompt_id}'")

    latest = max(approved, key=lambda d: _version_number(d["version"]))
    return _to_record(latest)
