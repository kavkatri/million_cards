from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://million:million@localhost:5432/million_cards"
    redis_url: str = "redis://localhost:6379/0"

    secret_key: str = "dev-only-change-me"
    # Fernet key, base64 32 bytes. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""

    media_root: Path = Path("./media")
    log_level: str = "INFO"

    # First-run admin, for platforms that offer no shell into the container.
    # Applied only when no user exists; ignored on every boot after that.
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    # How many tasks one worker process runs concurrently. Safe to raise: the
    # per-account token buckets, not this number, bound the marketplace request
    # rate. Raise it to keep more in flight while workers wait on tokens.
    worker_concurrency: int = 8

    # Seconds a claimed task may stay unfinished before another worker may
    # reclaim it. Must exceed the slowest single API call.
    task_lease_seconds: int = 300

    # A freshly created card is not visible to price/stock endpoints for a while
    # (WB documents up to 30 minutes). Aspects other than `card` skip SKUs whose
    # card was created less than this long ago.
    card_sync_grace_seconds: int = 1800


@lru_cache
def get_settings() -> Settings:
    return Settings()
