import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"

_REQUIRED_FIELDS = [
    "model_id",
    "version",
    "status",
    "owner",
    "created_at",
    "description",
    "provider",
    "model_name",
    "use_case",
]


@dataclass
class ModelRecord:
    model_id: str
    version: str
    status: str
    owner: str
    created_at: str
    description: str
    provider: str
    model_name: str
    use_case: str


@dataclass
class ModelRuntime:
    """Registry metadata plus the actual provider/model resolved after env overrides."""
    model_id: str
    model_version: str
    model_status: str
    model_provider: str       # from registry
    model_name: str           # from registry
    model_description: str
    runtime_provider: str     # effective provider after LLM_PROVIDER override
    runtime_model_name: str   # effective model name after OPENAI_MODEL / BEDROCK_MODEL_ID override
    runtime_overridden: bool  # True when any env var changed the registry values


def resolve_model_runtime(model_record: ModelRecord) -> ModelRuntime:
    """Return the effective provider and model name after applying env overrides.

    - LLM_PROVIDER overrides model_record.provider.
    - OPENAI_MODEL overrides model_name when the effective provider is openai.
    - BEDROCK_MODEL_ID overrides model_name when the effective provider is bedrock.
    - runtime_overridden is True when either value differs from the registry.
    """
    runtime_provider = os.getenv("LLM_PROVIDER", model_record.provider).lower()

    if runtime_provider == "openai":
        env_model = os.getenv("OPENAI_MODEL")
    elif runtime_provider == "bedrock":
        env_model = os.getenv("BEDROCK_MODEL_ID")
    else:
        env_model = None

    runtime_model_name = env_model or model_record.model_name
    runtime_overridden = (
        runtime_provider != model_record.provider.lower()
        or runtime_model_name != model_record.model_name
    )

    return ModelRuntime(
        model_id=model_record.model_id,
        model_version=model_record.version,
        model_status=model_record.status,
        model_provider=model_record.provider,
        model_name=model_record.model_name,
        model_description=model_record.description,
        runtime_provider=runtime_provider,
        runtime_model_name=runtime_model_name,
        runtime_overridden=runtime_overridden,
    )


def _load_file(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    missing = [field for field in _REQUIRED_FIELDS if field not in data]
    if missing:
        raise ValueError(f"Model file {path} is missing required fields: {missing}")
    return data


def _to_record(data: dict) -> ModelRecord:
    return ModelRecord(
        model_id=data["model_id"],
        version=data["version"],
        status=data["status"],
        owner=data["owner"],
        created_at=data["created_at"],
        description=data["description"],
        provider=data["provider"],
        model_name=data["model_name"],
        use_case=data["use_case"],
    )


def _version_number(version: str) -> int:
    m = re.match(r"v(\d+)$", version)
    return int(m.group(1)) if m else 0


def get_model(
    model_id: str,
    version: str | None = None,
    require_approved: bool = True,
    models_dir: Path | None = None,
) -> ModelRecord:
    """Return a ModelRecord from the registry.

    If version is omitted, returns the latest approved version (require_approved
    has no effect in this path — the scan is always approval-gated).

    If version is provided:
    - require_approved=True (default): raises ValueError when the loaded version's
      status is not "approved", blocking unapproved models from production.
    - require_approved=False: loads the version regardless of status, for use
      during development or testing of draft / deprecated model configs.

    Raises ValueError for unknown model_id, missing version file, or no approved
    version found when version is omitted.
    """
    base = models_dir if models_dir is not None else MODELS_DIR
    model_dir = base / model_id

    if not model_dir.exists():
        raise ValueError(f"Unknown model_id: '{model_id}'")

    if version is not None:
        path = model_dir / f"{version}.json"
        if not path.exists():
            raise ValueError(
                f"Version '{version}' not found for model_id '{model_id}'"
            )
        data = _load_file(path)
        if require_approved and data.get("status") != "approved":
            raise ValueError(
                f"Version '{version}' of '{model_id}' has status "
                f"'{data.get('status')}', not 'approved'. "
                f"Pass require_approved=False to load non-approved versions."
            )
        return _to_record(data)

    approved = []
    for path in sorted(model_dir.glob("*.json")):
        try:
            data = _load_file(path)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        if data.get("status") == "approved":
            approved.append(data)

    if not approved:
        raise ValueError(f"No approved version found for model_id '{model_id}'")

    latest = max(approved, key=lambda d: _version_number(d["version"]))
    return _to_record(latest)
