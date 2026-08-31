"""Application-owned participant/admin authorization.

The Discord adapter validates guild and channel scope; this policy validates
the actor against bundle participants and the configured administrators in
application code, independent of any UI placement.
"""

from __future__ import annotations

from nemoir.domain.errors import AuthorizationError
from nemoir.domain.models import ConversationBundle, Tendril
from nemoir.persistence.sqlite_repository import SQLiteRepository


class AuthorizationPolicy:
    def __init__(
        self,
        repository: SQLiteRepository,
        admin_user_ids: set[str] | None = None,
    ) -> None:
        self.repository = repository
        self.admin_user_ids = set(admin_user_ids or ())

    def is_admin(self, actor_user_id: str) -> bool:
        return actor_user_id in self.admin_user_ids

    def require_admin(self, actor_user_id: str) -> None:
        if not self.is_admin(actor_user_id):
            raise AuthorizationError("Only an administrator may perform this operation")

    def require_bundle_participant(self, bundle_id: str, actor_user_id: str) -> ConversationBundle:
        """Require the actor to be a bundle participant (submitter/recipient) or admin."""
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id not in {
            bundle.submitter_user_id,
            bundle.recipient_user_id,
        } and not self.is_admin(actor_user_id):
            raise AuthorizationError(
                "Only bundle participants or administrators may perform this operation"
            )
        return bundle

    def require_bundle_recipient(self, bundle_id: str, actor_user_id: str) -> ConversationBundle:
        """Require the designated recipient or an administrator."""
        bundle = self.repository.get_bundle(bundle_id)
        if actor_user_id != bundle.recipient_user_id and not self.is_admin(actor_user_id):
            raise AuthorizationError(
                "Only the designated recipient or an administrator may run analysis"
            )
        return bundle

    def require_tendril_participant(self, tendril_id: str, actor_user_id: str) -> Tendril:
        """Require the actor to be a participant of the tendril's bundle or an admin."""
        tendril = self.repository.get_tendril(tendril_id)
        self.require_bundle_participant(tendril.bundle_id, actor_user_id)
        return tendril

    def can_act_on_tendril(self, tendril_id: str, actor_user_id: str) -> bool:
        try:
            self.require_tendril_participant(tendril_id, actor_user_id)
        except AuthorizationError:
            return False
        return True
