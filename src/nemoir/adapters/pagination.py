"""Deterministic Discord-safe message pagination helpers.

Discord limits each message to 2000 characters, measured in UTF-16 code units:
a BMP code point counts as one unit while an astral code point (for example an
emoji outside the Basic Multilingual Plane) counts as two units. Nemoir
measures every page with ``discord_text_units`` so an emoji-heavy message can
never exceed the platform limit even when ``len()`` (which counts Python code
points) says it fits. No helper here ever truncates content:

- ``discord_text_units`` reports the exact UTF-16 code-unit length of a string;
- ``paginate_text`` splits evidence-bearing text into whole pages at line
  boundaries, hard-splitting only a single over-long line (never dropping it
  and never splitting inside a Unicode code point), and labels multi-page
  output with a compact page footer;
- ``bounded_page`` renders the leading rows of a list that fit on one page
  and reports how many were omitted instead of silently discarding them.

Both pagination helpers are pure and deterministic: identical input always
produces identical pages, and every returned page is guaranteed no longer than
the platform limit in UTF-16 code units.
"""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

DISCORD_MESSAGE_LIMIT = 2000

T = TypeVar("T")


def discord_text_units(text: str) -> int:
    """Return the number of UTF-16 code units in ``text``.

    ``len(text)`` counts Python code points, but Discord implementations may
    enforce their limit in UTF-16 code units. A code point above U+FFFF (an
    astral character such as most emoji) is a surrogate pair and therefore
    counts as two units; every BMP code point counts as one unit.
    """
    # UTF-16-LE encodes each code point to exactly 2 or 4 bytes with no BOM,
    # so the byte length divided by two is the exact code-unit count.
    return len(text.encode("utf-16-le")) // 2


def _page_footer(index: int, total: int) -> str:
    return f"\n— page {index}/{total} —"


def _char_units(char: str) -> int:
    return 2 if ord(char) > 0xFFFF else 1


def _hard_split(line: str, budget: int) -> tuple[str, str]:
    """Split ``line`` into a head of at most ``budget`` units plus the rest.

    The split always falls between Python characters, never inside a Unicode
    code point, and never drops the first character even when a single astral
    character alone would exceed a pathologically small budget.
    """
    used = 0
    for index, char in enumerate(line):
        unit = _char_units(char)
        if used + unit > budget:
            if index == 0:
                return line[:1], line[1:]
            return line[:index], line[index:]
        used += unit
    return line, ""


def _pack_lines(text: str, budget: int) -> list[str]:
    """Greedily pack whole lines into pages of at most ``budget`` units.

    A single line longer than the budget is hard-split at code-point
    boundaries so no content is ever dropped and no code point is ever cut in
    half; the split is deterministic.
    """
    lines = text.splitlines()
    if text.endswith(("\n", "\r")):
        lines.append("")
    lines = lines or [""]
    pages: list[str] = []
    current = ""
    started = False
    for line in lines:
        candidate = f"{current}\n{line}" if started else line
        if discord_text_units(candidate) <= budget:
            current = candidate
            started = True
            continue
        if started:
            pages.append(current)
            line = "\n" + line  # keep the separator before hard-splitting
            started = False
        while discord_text_units(line) > budget:
            head, line = _hard_split(line, budget)
            pages.append(head)
        current = line
        started = True
    if started or not pages:
        pages.append(current)
    return pages


def paginate_text(text: str, *, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into deterministic pages no longer than ``limit`` units.

    A single over-long line is hard-split at code-point boundaries so the
    content is never silently discarded and no code point is ever cut in half.
    Multi-page results carry a compact ``— page i/n —`` footer whose unit
    width is reserved on every page.
    """
    if limit < 64:
        raise ValueError("pagination limit must be at least 64 characters")
    if discord_text_units(text) <= limit:
        return [text]
    pages = _pack_lines(text, limit)
    if len(pages) == 1:
        return pages
    # The footer must fit on every page; re-pack with the widest footer's
    # unit width reserved (page indexes can be wider than the total, e.g.
    # "page 10/17" is one character longer than "page 1/17"), and iterate
    # because the page count can change the footer width.
    total = len(pages)
    while True:
        budget = limit - discord_text_units(_page_footer(total, total))
        repacked = _pack_lines(text, budget)
        if len(repacked) == total:
            pages = repacked
            break
        total = len(repacked)
    return [
        f"{page}{_page_footer(index, len(pages))}"
        for index, page in enumerate(pages, start=1)
    ]


def bounded_page(
    items: Sequence[T],
    *,
    render: Callable[[T], str],
    limit: int = DISCORD_MESSAGE_LIMIT,
    footer: str = "",
) -> tuple[str, int]:
    """Render the leading ``items`` that fit on one page.

    Returns ``(text, omitted_count)``. Rows are never truncated: a row that
    would overflow the page ends it, and a row longer than the whole budget
    is counted as omitted. All measurements use UTF-16 code units.

    ``footer`` is the literal text appended when items were omitted. A
    leading newline on the footer is ignored, so callers cannot accidentally
    introduce a duplicate separator: the helper inserts exactly one ``\\n``
    between the rows and the footer (only when at least one row was kept) and
    reserves that separator inside the limit, so the returned text is always
    at most ``limit`` units.
    """
    if limit < 64:
        raise ValueError("pagination limit must be at least 64 characters")
    footer = footer.lstrip("\n")
    if footer and discord_text_units(footer) >= limit - 1:
        raise ValueError("page footer is longer than the page limit")
    # Reserve the footer plus its single separator inside the limit so the
    # final "text\\nfooter" combination can never exceed `limit` units.
    budget = limit - discord_text_units(footer) - (1 if footer else 0)
    text = ""
    kept = 0
    for item in items:
        row = render(item)
        if not row:
            continue
        candidate = f"{text}\n{row}" if text else row
        if discord_text_units(candidate) <= budget:
            text = candidate
            kept += 1
        else:
            break
    omitted = len(items) - kept
    if omitted and footer:
        return f"{text}\n{footer}" if text else footer, omitted
    return text, omitted
