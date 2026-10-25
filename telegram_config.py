"""Optional Telegram configuration. No built-in account IDs, hashes or links."""
import os
from pathlib import Path
from shared import PROJECT_DIR


def parse_group(value):
    text = str(value).strip()
    return int(text) if text.lstrip("-").isdigit() else text


def settings():
    api_id = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id.isdigit() or int(api_id) <= 0 or not api_hash.strip():
        raise ValueError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in the environment first.")
    path = Path(os.environ.get("TELEGRAM_SESSION_PATH") or str(PROJECT_DIR / ".private" / "telegram")).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return int(api_id), api_hash, str(path)
