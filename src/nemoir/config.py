"""Environment-only configuration with no secret logging."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from nemoir.domain.models import StrictModel


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def _csv_env(name: str) -> set[str]:
    """Read a comma-separated set while ignoring whitespace and empty items."""
    return {
        item.strip()
        for item in os.getenv(name, "").split(",")
        if item.strip()
    }


class Settings(StrictModel):
    discord_bot_token: str | None = None
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    guild_id: str | None = None
    intake_channel_id: str | None = None
    additional_intake_channel_ids: set[str] = Field(default_factory=set)
    anemone_category_id: str | None = None
    nursery_channel_id: str | None = None
    admin_user_ids: set[str] = Field(default_factory=set)
    database_path: Path = Path("data/nemoir.sqlite3")
    runtime_dir: Path = Path("data/runtime")
    log_level: str = "INFO"
    allow_live_deepseek: bool = False
    allow_channel_write: bool = False
    prompt_max_input_chars: int = 2000
    prompt_max_output_tokens: int = 1024
    # Bounded background autonomous worker: how often it scans for actionable
    # jobs and how many provider requests may run at once (one seal therefore
    # can never launch an uncontrolled number of paid calls).
    autonomous_poll_seconds: float = 3.0
    autonomous_max_concurrent: int = 1

    @classmethod
    def from_environment(cls, *, include_deepseek: bool = True) -> "Settings":
        admins = _csv_env("NEMOIR_ADMIN_USER_IDS")
        return cls(
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN") or None,
            deepseek_api_key=(os.getenv("DEEPSEEK_API_KEY") or None) if include_deepseek else None,
            deepseek_base_url=(
                os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
                if include_deepseek
                else "https://api.deepseek.com"
            ),
            deepseek_model=(
                os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
                if include_deepseek
                else "deepseek-v4-flash"
            ),
            guild_id=os.getenv("NEMOIR_GUILD_ID") or None,
            intake_channel_id=os.getenv("NEMOIR_INTAKE_CHANNEL_ID") or None,
            additional_intake_channel_ids=_csv_env(
                "NEMOIR_ADDITIONAL_INTAKE_CHANNEL_IDS"
            ),
            anemone_category_id=os.getenv("NEMOIR_ANEMONE_CATEGORY_ID") or None,
            nursery_channel_id=os.getenv("NEMOIR_NURSERY_CHANNEL_ID") or None,
            admin_user_ids=admins,
            database_path=Path(os.getenv("NEMOIR_DATABASE_PATH", "data/nemoir.sqlite3")),
            runtime_dir=Path(os.getenv("NEMOIR_RUNTIME_DIR", "data/runtime")),
            log_level=os.getenv("NEMOIR_LOG_LEVEL", "INFO"),
            allow_live_deepseek=(
                _bool_env("NEMOIR_ALLOW_LIVE_DEEPSEEK") if include_deepseek else False
            ),
            allow_channel_write=_bool_env("NEMOIR_ALLOW_CHANNEL_WRITE"),
            prompt_max_input_chars=_int_env("NEMOIR_PROMPT_MAX_INPUT_CHARS", 2000),
            prompt_max_output_tokens=_int_env("NEMOIR_PROMPT_MAX_OUTPUT_TOKENS", 1024),
            autonomous_poll_seconds=_float_env("NEMOIR_AUTONOMOUS_POLL_SECONDS", 3.0),
            autonomous_max_concurrent=_int_env("NEMOIR_AUTONOMOUS_MAX_CONCURRENT", 1),
        )

    def safe_summary(self) -> dict[str, object]:
        return {
            "discord_token_configured": bool(self.discord_bot_token),
            "deepseek_key_configured": bool(self.deepseek_api_key),
            "deepseek_base_url": self.deepseek_base_url,
            "deepseek_model": self.deepseek_model,
            "guild_id_configured": bool(self.guild_id),
            "intake_channel_id_configured": bool(self.intake_channel_id),
            "intake_channel_count": len(
                {
                    *self.additional_intake_channel_ids,
                    *({self.intake_channel_id} if self.intake_channel_id else set()),
                }
            ),
            "anemone_category_id_configured": bool(self.anemone_category_id),
            "nursery_channel_id_configured": bool(self.nursery_channel_id),
            "admin_count": len(self.admin_user_ids),
            "database_path": str(self.database_path),
            "runtime_dir": str(self.runtime_dir),
            "log_level": self.log_level,
            "allow_live_deepseek": self.allow_live_deepseek,
            "allow_channel_write": self.allow_channel_write,
            "prompt_max_input_chars": self.prompt_max_input_chars,
            "prompt_max_output_tokens": self.prompt_max_output_tokens,
            "autonomous_poll_seconds": self.autonomous_poll_seconds,
            "autonomous_max_concurrent": self.autonomous_max_concurrent,
        }
