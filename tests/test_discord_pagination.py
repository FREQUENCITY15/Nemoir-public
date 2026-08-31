"""Deterministic Discord-safe pagination tests; no live Discord connection."""

from __future__ import annotations

import re

import pytest

from nemoir.adapters.pagination import (
    DISCORD_MESSAGE_LIMIT,
    bounded_page,
    discord_text_units,
    paginate_text,
)


def _strip_footer(page: str) -> str:
    return re.sub(r"\n— page \d+/\d+ —$", "", page)


def _reconstruct(pages: list[str]) -> str:
    return "".join(_strip_footer(page) for page in pages)


def test_platform_limit_constant_is_2000() -> None:
    assert DISCORD_MESSAGE_LIMIT == 2000


def test_short_text_is_a_single_page_without_footer() -> None:
    text = "**A title**\nShort description."
    pages = paginate_text(text)
    assert pages == [text]


def test_single_overlong_line_is_hard_split_not_dropped() -> None:
    title = "t" * (DISCORD_MESSAGE_LIMIT + 500)
    text = f"**{title}**\nA second line."
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_long_description_preserves_full_content_across_pages() -> None:
    description = "line one\n" + ("d" * 2500) + "\nfinal line"
    text = f"**Long tendril**\n{description}"
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text
    assert "final line" in pages[-1]


def test_long_evidence_is_never_silently_discarded() -> None:
    quotes = [f"> quote {index}: " + ("q" * 700) for index in range(5)]
    text = "\n".join(["**Evidence-heavy tendril**", *quotes, "Open because: still open"])
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    reconstructed = _reconstruct(pages)
    for quote in quotes:
        assert quote in reconstructed


def test_multipage_output_carries_page_footers() -> None:
    text = "\n".join(f"row {index} " + "x" * 100 for index in range(100))
    pages = paginate_text(text)
    assert len(pages) > 1
    assert re.search(r"\n— page 1/\d+ —$", pages[0])
    assert pages[-1].endswith(f"— page {len(pages)}/{len(pages)} —")
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)


def test_two_digit_page_footers_never_push_a_page_over_the_limit() -> None:
    text = "\n".join(f"row {index:03d} " + "x" * 90 for index in range(150))
    pages = paginate_text(text, limit=256)
    assert len(pages) > 9  # two-digit page indexes exist
    assert all(len(page) <= 256 for page in pages)
    assert max(len(page) for page in pages) <= 256
    assert _reconstruct(pages) == text


def test_pagination_is_deterministic() -> None:
    text = "\n".join(f"row {index} " + "y" * 120 for index in range(80))
    assert paginate_text(text) == paginate_text(text)


def test_overlong_line_in_the_middle_preserves_separators() -> None:
    text = "\n".join(
        [
            "first short line",
            "x" * (DISCORD_MESSAGE_LIMIT + 300),
            "final short line",
        ]
    )
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_many_short_lines_pack_into_bounded_pages_without_loss() -> None:
    text = "\n".join(f"line {index} " + "z" * 40 for index in range(120))
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(len(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_trailing_line_break_is_preserved() -> None:
    text = "alpha\nbeta\n"
    assert _reconstruct(paginate_text(text)) == text


def test_bounded_page_keeps_leading_rows_and_counts_omitted() -> None:
    items = [f"item-{index}" for index in range(60)]
    render = lambda item: f"`{item}` **row** " + "x" * 60
    text, omitted = bounded_page(items, render=render)
    assert omitted > 0
    assert omitted + text.count("`item-") == 60
    assert len(text) <= DISCORD_MESSAGE_LIMIT
    assert text.startswith("`item-0`")


def test_bounded_page_reports_nothing_omitted_when_all_fit() -> None:
    items = ["a", "b"]
    text, omitted = bounded_page(items, render=lambda item: f"- {item}")
    assert omitted == 0
    assert text == "- a\n- b"


def test_bounded_page_reserves_footer_and_separator_inside_the_limit() -> None:
    items = [f"item-{index} " + "z" * 90 for index in range(30)]
    footer = "… more remain; filter by status"
    text, omitted = bounded_page(items, render=lambda item: item, footer=footer)
    assert omitted > 0
    assert len(text) <= DISCORD_MESSAGE_LIMIT
    assert text.endswith("\n" + footer)
    assert text.count("\n… more") == 1


def test_bounded_page_exact_boundary_fills_the_calculated_budget() -> None:
    footer = "…more"
    budget = DISCORD_MESSAGE_LIMIT - len(footer) - 1  # footer plus one separator
    items = ["x" * budget, "y"]  # the first row fills the whole budget exactly
    text, omitted = bounded_page(items, render=lambda item: item, footer=footer)
    assert omitted == 1
    assert text == ("x" * budget) + "\n" + footer
    assert len(text) == DISCORD_MESSAGE_LIMIT
    assert len(text) <= DISCORD_MESSAGE_LIMIT


def test_bounded_page_footer_leading_newline_is_normalized_not_doubled() -> None:
    items = ["x" * 1994, "y"]
    plain = bounded_page(items, render=lambda item: item, footer="…more")
    with_leading = bounded_page(items, render=lambda item: item, footer="\n…more")
    assert plain == with_leading
    assert "\n\n" not in plain[0]
    assert len(plain[0]) <= DISCORD_MESSAGE_LIMIT


@pytest.mark.parametrize(
    ("row_width", "count", "footer"),
    [
        (90, 30, "… more remain; filter by status"),
        (1994, 2, "…"),  # first row fills the budget exactly
        (2004, 3, "…more"),  # oversized first row: the footer stands alone
        (7, 400, ""),  # many rows, no footer
        (2000, 3, ""),  # oversized first row, no footer
    ],
)
def test_bounded_page_never_exceeds_the_real_discord_limit(
    row_width: int, count: int, footer: str
) -> None:
    items = [f"{index:04d}" + "z" * row_width for index in range(count)]
    text, omitted = bounded_page(items, render=lambda item: item, footer=footer)
    assert len(text) <= DISCORD_MESSAGE_LIMIT
    if omitted == 0:
        assert footer not in text


def test_bounded_page_drops_footer_when_everything_fits() -> None:
    items = ["only", "two", "rows"]
    footer = "… more remain"
    text, omitted = bounded_page(items, render=lambda item: f"- {item}", footer=footer)
    assert omitted == 0
    assert "… more" not in text


def test_bounded_page_counts_an_oversized_row_as_omitted() -> None:
    items = ["x" * (DISCORD_MESSAGE_LIMIT + 10), "small"]
    text, omitted = bounded_page(items, render=lambda item: item)
    assert text == ""
    assert omitted == 2


def test_bounded_page_is_deterministic() -> None:
    items = [f"item-{index} " + "w" * 60 for index in range(40)]
    first = bounded_page(items, render=lambda item: item)
    assert bounded_page(items, render=lambda item: item) == first


def test_invalid_limits_are_rejected() -> None:
    with pytest.raises(ValueError):
        paginate_text("text", limit=10)
    with pytest.raises(ValueError):
        bounded_page(["a"], render=lambda item: item, limit=10)
    with pytest.raises(ValueError):
        bounded_page(["a"], render=lambda item: item, footer="f" * 2000)
    with pytest.raises(ValueError):
        bounded_page(["a"], render=lambda item: item, footer="f" * 1999)


def test_discord_text_units_counts_astral_code_points_as_two() -> None:
    assert discord_text_units("") == 0
    assert discord_text_units("a") == 1
    assert discord_text_units("é") == 1  # BMP combining-accented character
    assert discord_text_units("😀") == 2  # astral emoji
    assert discord_text_units("a😀b") == 4
    assert discord_text_units("😀😀😀") == 6


def test_pagination_never_splits_inside_an_astral_code_point() -> None:
    text = "😀" * 1500
    assert len(text) <= DISCORD_MESSAGE_LIMIT  # 1500 code points fits
    assert discord_text_units(text) > DISCORD_MESSAGE_LIMIT  # 3000 units does not
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_pagination_handles_code_points_fitting_but_units_exceeding() -> None:
    text = "😀" * 1500
    assert len(text) <= DISCORD_MESSAGE_LIMIT
    assert discord_text_units(text) > DISCORD_MESSAGE_LIMIT
    pages = paginate_text(text)
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_pagination_mixed_unicode_reconstructs_within_unit_limit() -> None:
    text = "\n".join(
        f"row {index} é 😀 " + "x" * 60 + " 😀 é"
        for index in range(60)
    )
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_pagination_long_unbroken_astral_line_reconstructs() -> None:
    text = "😀" * 500 + "zzz" + "😀" * 900
    pages = paginate_text(text)
    assert len(pages) > 1
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)
    assert _reconstruct(pages) == text


def test_pagination_footer_is_reserved_in_units_for_astral_content() -> None:
    text = "😀" * 1500
    pages = paginate_text(text)
    assert len(pages) > 1
    for page in pages:
        assert re.search(r"\n— page \d+/\d+ —$", page)
    assert all(discord_text_units(page) <= DISCORD_MESSAGE_LIMIT for page in pages)


def test_bounded_page_counts_astral_units_toward_the_limit() -> None:
    footer = "…more"
    # budget = 2000 - 5 (footer units) - 1 (separator) = 1994 units.
    # 997 astral emoji are exactly 1994 units, so they fill the budget exactly
    # and the next row is omitted.
    items = ["😀" * 997, "x"]
    text, omitted = bounded_page(items, render=lambda item: item, footer=footer)
    assert omitted == 1
    assert text == ("😀" * 997) + "\n" + footer
    assert discord_text_units(text) == DISCORD_MESSAGE_LIMIT


def test_bounded_page_astral_rows_are_never_truncated_or_dropped() -> None:
    items = ["😀" * 600, "😀" * 600, "small"]
    text, omitted = bounded_page(items, render=lambda item: item, footer="…")
    assert discord_text_units(text) <= DISCORD_MESSAGE_LIMIT
    # First two rows are 1200 + 1 + 1200 = 2401 units, over budget, so only the
    # first row is kept and both remaining rows are omitted (never truncated).
    assert text.startswith("😀" * 600)
    assert omitted == 2
