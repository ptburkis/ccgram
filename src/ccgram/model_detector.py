"""Model detector — infer the current Claude model from a JSONL transcript.

Reads the last 16 KB of a transcript file and scans lines in reverse for the
most-recent assistant entry that carries a 'model' field.  Returns a short
normalised label ('opus', 'sonnet', 'haiku') or None.

Key function: get_current_model(transcript_path) -> str | None
"""

import json

_TAIL_BYTES = 16 * 1024

_MODEL_MAP: dict[str, str] = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
}


def _normalise_model(model: str | None) -> str | None:
    if not model or model == "<synthetic>":
        return None
    for prefix, label in _MODEL_MAP.items():
        if model.startswith(prefix):
            return label
    return None


def _extract_model_from_entry(entry: dict) -> str | None:
    """Return the 'model' field from a parsed JSONL entry."""
    # Top-level model field (Claude Code transcript format)
    model = entry.get("model")
    if model:
        return model
    # Nested under 'message' (some providers wrap the API response)
    message = entry.get("message")
    if isinstance(message, dict):
        model = message.get("model")
        if model:
            return model
    return None


def _is_assistant_entry(entry: dict) -> bool:
    """Return True if the entry represents an assistant turn."""
    # Direct role field
    if entry.get("role") == "assistant":
        return True
    # Type-tagged transcript entries (Claude Code JSONL)
    if entry.get("type") == "assistant":
        return True
    # Nested message object
    message = entry.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return True
    return False


def get_current_model(transcript_path: str) -> str | None:
    """Return a normalised model label from the transcript, or None.

    Reads the last 16 KB of the file and scans lines in reverse order to
    find the most-recent assistant entry with a 'model' field.  Never raises.
    """
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            read_size = min(_TAIL_BYTES, size)
            fh.seek(-read_size, 2)
            raw = fh.read(read_size)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if not _is_assistant_entry(entry):
                continue
            raw_model = _extract_model_from_entry(entry)
            if raw_model:
                return _normalise_model(raw_model)
    except OSError, ValueError:
        pass
    return None
