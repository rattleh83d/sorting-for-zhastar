"""Targeted Telegram photo recovery. Defaults to an offline preview."""
from __future__ import annotations

import argparse
import asyncio
import re
import tempfile
from pathlib import Path
from shared import DEFAULT_DATA_DIR, IMAGE_EXTS, image_paths, enable_heif
from telegram_config import parse_group, settings
from verify_completeness import message_id


def parse_ids(value: str) -> list[int]:
    if not re.fullmatch(r"\s*\d+(?:\s*,\s*\d+)*\s*", value):
        raise argparse.ArgumentTypeError("Use positive message IDs separated by commas.")
    values = list(dict.fromkeys(int(v.strip()) for v in value.split(",")))
    if not values or min(values) <= 0:
        raise argparse.ArgumentTypeError("Message IDs must be positive.")
    return values


def existing_ids(root: Path) -> set[int]:
    return {mid for path in image_paths(root) if path.stat().st_size > 0 and (mid := message_id(path)) is not None}


async def recover(root: Path, group, ids: list[int], client_factory=None) -> dict:
    if client_factory is None:
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise ValueError("Install requirements-telegram.txt first.") from exc
        client_factory = TelegramClient
    api_id, api_hash, session = settings()
    destination = root / "_unsorted"
    if destination.is_symlink() or not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("The unsorted directory must stay inside the selected archive.")
    destination.mkdir(exist_ok=True)
    known = existing_ids(root)
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    enable_heif()
    from PIL import Image
    async with client_factory(session, api_id, api_hash) as client:
        entity = await client.get_entity(parse_group(group))
        for mid in ids:
            if mid in known:
                counts["skipped"] += 1
                continue
            temporary = None
            try:
                message = await client.get_messages(entity, ids=mid)
                document = getattr(message, "document", None) if message else None
                if not message or not (getattr(message, "photo", None) or document and (document.mime_type or "").startswith("image/")):
                    raise ValueError("Message is unavailable or is not an image.")
                suffix = ".jpg" if getattr(message, "photo", None) else Path(getattr(message.file, "name", "") or "").suffix.lower()
                if suffix not in IMAGE_EXTS:
                    suffix = {"image/heic": ".heic", "image/heif": ".heif", "image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}.get(getattr(document, "mime_type", ""), ".jpg")
                handle, temp_name = tempfile.mkstemp(prefix=f".msg_{mid}-", suffix=".part", dir=destination)
                import os
                os.close(handle)
                temporary = Path(temp_name)
                returned = await client.download_media(message, file=str(temporary))
                if not returned or not temporary.stat().st_size:
                    raise ValueError("Empty download.")
                with Image.open(temporary) as image:
                    image.verify()
                target = destination / f"msg_{mid}{suffix}"
                # A nonempty existing file anywhere in the archive was already skipped.
                # Never overwrite even an empty file: retain it for inspection.
                number = 1
                while target.exists():
                    target = destination / f"msg_{mid}_dup{number}{suffix}"
                    number += 1
                temporary.rename(target)
                known.add(mid)
                counts["downloaded"] += 1
            except Exception:
                counts["failed"] += 1
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--ids", required=True, type=parse_ids, help="Comma-separated positive message IDs")
    parser.add_argument("--group", "--group-id", dest="group", required=True)
    parser.add_argument("--apply", action="store_true", help="Authenticate to Telegram and download missing images")
    args = parser.parse_args(argv)
    root = args.data_dir.expanduser().resolve()
    if not root.is_dir():
        parser.error("Data directory does not exist.")
    missing = [mid for mid in args.ids if mid not in existing_ids(root)]
    print(f"Requested: {len(args.ids)}; missing nonempty images: {len(missing)}")
    if not args.apply:
        print("Preview only. No Telegram connection or downloads.")
        return 0
    try:
        result = asyncio.run(recover(root, args.group, args.ids))
    except Exception as exc:
        parser.error(f"Telegram recovery could not start ({type(exc).__name__}). Check configuration and group access.")
    print(f"Downloaded: {result['downloaded']}; skipped: {result['skipped']}; failed: {result['failed']}")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
