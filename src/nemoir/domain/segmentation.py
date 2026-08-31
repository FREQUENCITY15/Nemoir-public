"""Deterministic source-unit segmentation owned by the application."""

from __future__ import annotations

import re

from .models import SourceMessage, SourceUnit


_BLANK_LINE = re.compile(r"(?:\r?\n)[\t ]*(?:\r?\n)+")


def normalize_for_comparison(text: str) -> str:
    return " ".join(text.split())


def segment_message(message: SourceMessage) -> list[SourceUnit]:
    content = message.content
    boundaries: list[tuple[int, int]] = []
    cursor = 0
    for separator in _BLANK_LINE.finditer(content):
        boundaries.append((cursor, separator.start()))
        cursor = separator.end()
    boundaries.append((cursor, len(content)))

    units: list[SourceUnit] = []
    ordinal = 1
    for raw_start, raw_end in boundaries:
        raw = content[raw_start:raw_end]
        if not raw.strip():
            continue
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        start = raw_start + leading
        end = raw_start + trailing
        exact = content[start:end]
        units.append(
            SourceUnit(
                unit_id=f"{message.external_message_id}:p{ordinal}",
                source_message_id=message.external_message_id,
                paragraph_ordinal=ordinal,
                exact_text=exact,
                normalized_text=normalize_for_comparison(exact),
                start_offset=start,
                end_offset=end,
            )
        )
        ordinal += 1
    return units


def segment_messages(messages: list[SourceMessage]) -> list[SourceUnit]:
    units: list[SourceUnit] = []
    for message in sorted(messages, key=lambda item: item.ordinal):
        units.extend(segment_message(message))
    return units
