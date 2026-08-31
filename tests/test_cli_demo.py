from __future__ import annotations

import json

import pytest

from nemoir.__main__ import _demo_offline


@pytest.mark.asyncio
async def test_offline_demo_is_end_to_end_and_repeatable(tmp_path, capsys) -> None:
    database = tmp_path / "demo.sqlite3"
    assert await _demo_offline(database) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["fixture_status"] == "SYNTHETIC"
    assert first["status"] == "REVIEW_READY"
    assert first["coverage_entries"] == 7
    # Option 1 ("Free Will and Compassion") absorbs the free-will tendril into
    # the selected claim boundary, leaving the other four fixture tendrils.
    assert len(first["tendrils"]) == 4
    assert first["provider"] == "fake"

    assert await _demo_offline(database) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["bundle_id"] != first["bundle_id"]
