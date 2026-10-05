"""Settings.

Everything here is overridable from the environment so the same image runs
locally on SQLite and on Vercel against Neon without a code change.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- identity ---------------------------------------------------------
    app_name: str = "ParkWise - Priority-Aware Campus Parking"
    version: str = "1.0.0"
    api_prefix: str = "/api/v1"
    environment: str = "development"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    # --- persistence ------------------------------------------------------
    # Unset => zero-config SQLite for local dev. Deployment supplies
    # postgresql:// via DATABASE_URL.
    database_url: str | None = None
    auto_seed: bool = True

    # --- auth -------------------------------------------------------------
    jwt_secret: str = "parkwise-dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 60 * 60 * 12
    admin_token: str = ""
    # Per-device keys for the ingest endpoints. Format: "device_id:key,...".
    device_api_keys: str = ""

    # --- allocation engine defaults (mirrored by the `rules` table) -------
    grace_minutes: int = 10
    default_tier2_cutoff_hour: int = 11
    prediction_min_history_days: int = 5

    # --- notifications ----------------------------------------------------
    demo_now: str | None = None

    def device_keys(self) -> dict[str, str]:
        """Parse ``DEVICE_API_KEYS`` into ``{device_id: key}``."""
        out: dict[str, str] = {}
        for chunk in (self.device_api_keys or "").split(","):
            if not chunk.strip():
                continue
            device_id, _, key = chunk.partition(":")
            if device_id.strip() and key.strip():
                out[device_id.strip()] = key.strip()
        return out


settings = Settings()
