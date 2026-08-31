"""Deterministic Discord channel-name sanitisation and tendril slug derivation.

This module deliberately imports nothing from ``discord.py`` so the
platform-neutral publishing service and the Discord adapter can share the
exact same naming rules. Channel names are sanitised to lowercase ASCII,
hyphens, and digits, and collisions are resolved deterministically without
ever silently adopting an unrelated pre-existing channel.
"""

from __future__ import annotations

import re

from nemoir.domain.models import Tendril

_SLUG_MAX_LEN = 90

_UNUSABLE = re.compile(r"[^a-z0-9-]+")


def sanitize_channel_slug(value: str) -> str:
    """Lowercase ``value`` and keep only ``[a-z0-9-]``, collapsing separators.

    Raises ``ValueError`` when the input contains no usable characters.
    """
    slug = value.strip().lower()
    slug = _UNUSABLE.sub("-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        raise ValueError("Channel name contains no usable characters")
    return slug[:_SLUG_MAX_LEN].rstrip("-")


def derive_channel_slug(tendril: Tendril) -> str:
    """Return the sanitised base slug for a tendril.

    A validated ``suggested_habitat_slug`` is preferred; otherwise the tendril
    title is sanitised. When neither yields usable characters the deterministic
    fallback ``"tendril"`` is returned (the collision resolver can still make
    it unique).
    """
    for raw in (tendril.suggested_habitat_slug, tendril.title):
        if raw is None or not raw.strip():
            continue
        try:
            return sanitize_channel_slug(raw)
        except ValueError:
            continue
    return "tendril"


def resolve_channel_slug(base: str, existing_names: set[str]) -> str:
    """Deterministically resolve ``base`` against already-taken channel names.

    A colliding name is never adopted: ``-2``, ``-3``, ... are appended in
    order until the candidate is free. The base is trimmed so the suffix always
    fits inside the sanitised length limit.
    """
    candidate = base
    suffix = 2
    while candidate in existing_names:
        suffix_text = f"-{suffix}"
        budget = _SLUG_MAX_LEN - len(suffix_text)
        trimmed = base[:budget].rstrip("-") if len(base) > budget else base
        candidate = f"{trimmed}{suffix_text}"
        suffix += 1
    return candidate
