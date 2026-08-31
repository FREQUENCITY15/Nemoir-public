"""Operator CLI recovery for ambiguous publish operations."""

from __future__ import annotations

import sys

import pytest

from nemoir.__main__ import main
from nemoir.adapters.channel_naming import derive_channel_slug
from nemoir.application.analysis_service import AnalysisService
from nemoir.application.publishing_service import stable_publish_key
from nemoir.domain.states import ExternalOperationStatus, TendrilState
from nemoir.providers.fake import FakeAnalysisProvider


async def _review_ready(repository, captured_bundle, synthetic_analysis, authorization):
    await AnalysisService(
        repository, FakeAnalysisProvider(synthetic_analysis), authorization
    ).analyse(
        captured_bundle.id,
        actor_user_id="person-2",
        idempotency_key="publish-cli-analyse",
    )
    return repository.get_bundle(captured_bundle.id)


def _reserve_publish(repository, tendril, *, guild_id="guild-1", category_id="cat-1"):
    return repository.reserve_external_operation(
        operation_type="habitat_create",
        idempotency_key=stable_publish_key(tendril.bundle_id, tendril.id),
        platform="discord",
        tendril_id=tendril.id,
        actor_user_id="person-2",
        metadata={
            "bundle_id": tendril.bundle_id,
            "publish": "true",
            "requested_slug": derive_channel_slug(tendril),
            "guild_id": guild_id,
            "category_id": category_id,
            "phase": "reserved",
        },
    )


@pytest.mark.asyncio
async def test_cli_reconcile_publish_with_observed_channel_and_message(
    repository, captured_bundle, synthetic_analysis, authorization, capsys
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nemoir",
            "ops",
            "--database",
            str(repository.database_path),
            "--reconcile",
            op.id,
            "--channel-id",
            "ch-observed",
            "--message-id",
            "msg-observed",
        ],
    )
    try:
        assert main() == 0
    finally:
        monkeypatch.undo()

    assert "Reconciled" in capsys.readouterr().out
    assert repository.get_external_operation_by_id(op.id).status == ExternalOperationStatus.COMPLETED
    assert repository.get_route_for_tendril(tendril.id) is not None
    assert repository.get_tendril(tendril.id).status == TendrilState.ROUTED

    # Reconciliation is idempotent after completion and does not duplicate the
    # habitat or route.
    route_before = repository.get_route_for_tendril(tendril.id)
    reconciled_again = repository.reconcile_publish_operation(op.id)
    assert reconciled_again.status == ExternalOperationStatus.COMPLETED
    assert repository.get_route_for_tendril(tendril.id)["id"] == route_before["id"]


@pytest.mark.asyncio
async def test_cli_abandon_frees_tendril(
    repository, captured_bundle, synthetic_analysis, authorization, capsys
) -> None:
    await _review_ready(repository, captured_bundle, synthetic_analysis, authorization)
    tendril = repository.list_tendrils(bundle_id=captured_bundle.id)[0]
    op, _created = _reserve_publish(repository, tendril)
    repository.mark_operation_needs_reconciliation(op.id, reason="channel creation ambiguous")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nemoir",
            "ops",
            "--database",
            str(repository.database_path),
            "--reconcile",
            op.id,
            "--abandon",
        ],
    )
    try:
        assert main() == 0
    finally:
        monkeypatch.undo()

    assert "Abandoned" in capsys.readouterr().out
    assert repository.list_external_operations(tendril_id=tendril.id) == []


def test_cli_channel_id_requires_reconcile(repository) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nemoir",
            "ops",
            "--database",
            str(repository.database_path),
            "--channel-id",
            "ch-1",
        ],
    )
    try:
        with pytest.raises(SystemExit, match="require --reconcile"):
            main()
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize(
    "extra",
    [
        ["--channel-id", "ch-observed"],
        ["--message-id", "msg-observed"],
        ["--channel-id", "ch-observed", "--message-id", "msg-observed"],
    ],
)
def test_cli_abandon_rejects_observed_ids(repository, extra) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "nemoir",
            "ops",
            "--database",
            str(repository.database_path),
            "--reconcile",
            "op-1",
            "--abandon",
            *extra,
        ],
    )
    try:
        with pytest.raises(SystemExit, match="cannot be combined"):
            main()
    finally:
        monkeypatch.undo()
