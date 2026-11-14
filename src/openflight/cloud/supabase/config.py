"""Persistent config for Supabase uploads.

Stored beside the FlightWeb config as ``~/.config/openflight/supabase.json``
with mode ``0600``. FlightWeb and Supabase are configured independently: either
file may exist without the other.

``supabase_device_token`` is this Pi's credential (the database holds only its
SHA-256) and must never be printed or logged. ``supabase_key`` is the project's
publishable key, which is public by design; secret and service-role keys are
refused because they bypass the database's row-level security.
"""

import base64
import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ..config import CONFIG_PATH, write_private_json

CONFIG_NAME = "supabase.json"
SUPABASE_CONFIG_PATH = CONFIG_PATH.with_name(CONFIG_NAME)

# schema.sql ignores tokens shorter than 32 characters; 32 random bytes as hex
# gives 64.
TOKEN_BYTES = 32

PUBLISHABLE_PREFIX = "sb_publishable_"
SECRET_PREFIX = "sb_secret_"
LEGACY_JWT_PREFIX = "eyJ"

_PROJECT_URL = re.compile(r"^https://[a-z0-9]+\.supabase\.co$")
_DASHBOARD_URL = re.compile(r"supabase\.com/dashboard/project/([a-z0-9]+)")
_PUBLISHABLE_KEY = re.compile(r"^sb_publishable_[A-Za-z0-9_-]+$")
_JWT = re.compile(r"^[A-Za-z0-9_-]+\.([A-Za-z0-9_-]+)\.[A-Za-z0-9_-]+$")


@dataclass
class SupabaseConfig:
    """Supabase upload configuration persisted to disk."""

    supabase_url: str = ""
    supabase_key: str = ""
    supabase_device_token: str = field(default="", repr=False)
    supabase_device_name: str = ""
    enabled: bool = True

    def is_configured(self) -> bool:
        """True when the project URL, key and device token are all present."""
        return bool(self.supabase_url and self.supabase_key and self.supabase_device_token)

    def is_active(self) -> bool:
        """True when uploads should actually run (configured and enabled)."""
        return self.enabled and self.is_configured()


def config_path_for(cloud_config_path: Path) -> Path:
    """The Supabase config file that sits beside a FlightWeb config file."""
    return Path(cloud_config_path).with_name(CONFIG_NAME)


def load_supabase_config(path: Optional[Path] = None) -> Optional[SupabaseConfig]:
    """Load config from ``path`` (default ``SUPABASE_CONFIG_PATH``); None if absent.

    The default is read at call time, so the test suite can point the server
    away from a developer's real config. Raises ValueError if the file is not a
    JSON object.
    """
    path = Path(SUPABASE_CONFIG_PATH if path is None else path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return SupabaseConfig(
        supabase_url=str(data.get("supabase_url") or ""),
        supabase_key=str(data.get("supabase_key") or ""),
        supabase_device_token=str(data.get("supabase_device_token") or ""),
        supabase_device_name=str(data.get("supabase_device_name") or ""),
        enabled=data.get("enabled", True) is True,
    )


def save_supabase_config(config: SupabaseConfig, path: Path = SUPABASE_CONFIG_PATH) -> None:
    """Write config to ``path`` with mode 0600, creating parent dirs."""
    write_private_json(path, asdict(config))


def normalize_project_url(raw: str) -> str:
    """The project URL as ``https://<ref>.supabase.co``; ValueError if it is not one.

    A trailing slash or ``/rest/v1`` (the REST endpoint the dashboard also
    shows) is removed.
    """
    url = raw.strip().rstrip("/").lower()
    if url.endswith("/rest/v1"):
        url = url[: -len("/rest/v1")]
    if _PROJECT_URL.match(url):
        return url
    dashboard = _DASHBOARD_URL.search(url)
    if dashboard:
        raise ValueError(
            "That is the dashboard address. The project URL is "
            f"https://{dashboard.group(1)}.supabase.co"
        )
    raise ValueError(
        "Expected the project URL, like https://abcdefghijklmnopqrst.supabase.co "
        "(dashboard: Project Settings > Data API)."
    )


def _jwt_role(key: str) -> Optional[str]:
    """The ``role`` claim of a JWT, read without verifying it; None if unreadable."""
    match = _JWT.match(key)
    if not match:
        return None
    payload = match.group(1)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    return claims.get("role") if isinstance(claims, dict) else None


def check_publishable_key(raw: str) -> str:
    """The key, stripped, if a Pi may hold it; ValueError otherwise.

    Accepts a publishable key (``sb_publishable_...``) or a legacy anon JWT.
    """
    key = raw.strip()
    if _PUBLISHABLE_KEY.match(key):
        return key
    if key.startswith(SECRET_PREFIX):
        raise ValueError(
            "That is a secret key, which bypasses the database's access rules and must "
            "never be on the Pi. Use the publishable key (sb_publishable_...)."
        )
    if key.startswith(LEGACY_JWT_PREFIX):
        role = _jwt_role(key)
        if role == "anon":
            return key
        if role == "service_role":
            raise ValueError(
                "That is the legacy service_role key, which bypasses the database's access "
                "rules and must never be on the Pi. Use the publishable (anon) key."
            )
        raise ValueError("That JWT is not a Supabase anon key.")
    raise ValueError("Expected the publishable key (sb_publishable_...) or the legacy anon key.")


def uses_bearer(key: str) -> bool:
    """True for a legacy JWT key, which is also sent as ``Authorization: Bearer``.

    New ``sb_publishable_`` keys are not JWTs; sent as a bearer token they fail.
    """
    return key.startswith(LEGACY_JWT_PREFIX)


def generate_device_token() -> str:
    """A new random device token (64 hex characters)."""
    return secrets.token_hex(TOKEN_BYTES)


def token_sha256(token: str) -> str:
    """The SHA-256 hex digest schema.sql stores in ``golf_devices.token_sha256``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
