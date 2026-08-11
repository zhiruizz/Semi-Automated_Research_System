from __future__ import annotations

from uuid import uuid4


def new_id(prefix: str) -> str:
    """Return an opaque, sortable-enough identifier with a human-friendly prefix."""
    return f"{prefix}_{uuid4().hex}"
