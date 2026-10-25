"""Read-only photo inventory; Telegram comparison is opt-in."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from shared import DEFAULT_DATA_DIR, image_paths, _atomic_write, enable_heif


def message_id(path: Path) -> int | None:
    match = re.fullmatch(r"msg_(\d+)(?:_dup\d+)?", path.stem, re.IGNORECASE)
    return int(match.group(1)) if match else None


def inspect_local(root: Path, validate_images: bool = False) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Data directory does not exist.")
    by_id, unsorted, singles, invalid, unknown, folders = defaultdict(list), [], [], [], [], []
    images = image_paths(root)
    if validate_images:
        from PIL import Image
        enable_heif()
    for path in images:
        rel = path.relative_to(root)
        if rel.parts[0] == "_state":
            continue
        if rel.parts[0] == "_unsorted" or len(rel.parts) == 1:
            unsorted.append(str(rel))
        elif rel.parts[0] == "_singles":
            singles.append(str(rel))
        mid = message_id(path)
        if mid is None:
            unknown.append(str(rel))
        else:
            by_id[mid].append(str(rel))
        reason = "empty file" if path.stat().st_size == 0 else None
        if not reason and validate_images:
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception:
                reason = "image could not be decoded (including unsupported codec)"
        if reason:
            invalid.append({"path": str(rel), "reason": reason})
    for folder in sorted(root.iterdir(), key=lambda p: (not p.name.isdigit(), int(p.name) if p.name.isdigit() else p.name.casefold())):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        files = image_paths(folder)
        mids = [message_id(f) for f in files if message_id(f) is not None]
        flags = []
        if not files:
            flags.append("EMPTY")
        if files and not any(p.suffix.lower() in {".jpg", ".jpeg"} for p in files):
            flags.append("NO_JPG_INDEX")
        if len(mids) > 1 and max(mids) - min(mids) > 500:
            flags.append("WIDE_ID_SPREAD")
        folders.append({"folder": folder.name, "file_count": len(files), "has_jpg_index": any(p.suffix.lower() in {".jpg", ".jpeg"} for p in files), "is_numeric": folder.name.isdigit(), "msg_id_min": min(mids) if mids else "", "msg_id_max": max(mids) if mids else "", "id_spread": max(mids) - min(mids) if mids else "", "flags": ";".join(flags)})
    duplicates = {str(k): v for k, v in by_id.items() if len(v) > 1}
    return {"image_count": len(images), "by_id": dict(by_id), "unsorted": unsorted, "singles": singles, "invalid": invalid, "unknown_ids": unknown, "duplicates": duplicates, "folders": folders}


def compare_inventory(local: dict, albums: list[dict]) -> dict:
    """Compare IDs and every local location; never collapse duplicate files."""
    ids = {int(mid) for album in albums for mid in album["msg_ids"]}
    local_ids = {int(k) for k in local["by_id"]}
    split, partial = [], []
    for album in albums:
        mids = [int(mid) for mid in album["msg_ids"]]
        locations = {Path(path).parts[0] for mid in mids for path in local["by_id"].get(mid, []) if len(Path(path).parts) > 1}
        if len(locations) > 1:
            split.append({"group_id": str(album.get("group_id", "")), "folders": sorted(locations)})
        missing = [mid for mid in mids if mid not in local_ids]
        if missing:
            partial.append({"group_id": str(album.get("group_id", "")), "missing_ids": missing})
    return {"source_ids": len(ids), "missing_ids": sorted(ids - local_ids), "extra_ids": sorted(local_ids - ids), "split_albums": split, "partial_albums": partial}


def write_manifest(root: Path, local: dict) -> Path:
    path = Path(root) / "manifest.csv"
    fields = ["folder", "file_count", "has_jpg_index", "is_numeric", "msg_id_min", "msg_id_max", "id_spread", "flags"]
    def write(stream):
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(local["folders"])
    _atomic_write(path, write)
    return path


async def fetch_albums(group, client_factory=None) -> list[dict]:
    from telegram_config import settings, parse_group
    if client_factory is None:
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise ValueError("Install requirements-telegram.txt to enable Telegram comparison.") from exc
        client_factory = TelegramClient
    api_id, api_hash, session = settings()
    grouped = defaultdict(list)
    async with client_factory(session, api_id, api_hash) as client:
        entity = await client.get_entity(parse_group(group))
        async for message in client.iter_messages(entity, limit=None):
            # Compare photos and image documents, not unrelated PDFs/videos.
            document = getattr(message, "document", None)
            if not getattr(message, "photo", None) and not (document and (getattr(document, "mime_type", "") or "").startswith("image/")):
                continue
            key = str(message.grouped_id) if message.grouped_id else f"single:{message.id}"
            grouped[key].append(message.id)
    return [{"group_id": key, "msg_ids": sorted(ids)} for key, ids in grouped.items()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--local-only", action="store_true", help="Local-only is also the default")
    parser.add_argument("--validate-images", action="store_true", help="Decode image headers locally")
    parser.add_argument("--inventory", type=Path, help="Offline JSON list of {group_id, msg_ids}")
    parser.add_argument("--telegram", action="store_true", help="Read media metadata from Telegram")
    parser.add_argument("--group", "--group-id", dest="group", help="Accessible Telegram group ID or username")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args(argv)
    if args.local_only and args.telegram:
        parser.error("--local-only and --telegram cannot be combined")
    if args.inventory and args.telegram:
        parser.error("Choose --inventory or --telegram")
    if args.telegram and not args.group:
        parser.error("--telegram requires --group")
    try:
        local = inspect_local(args.data_dir, args.validate_images)
        print(f"Images: {local['image_count']}; unique message IDs: {len(local['by_id'])}")
        for key in ("unsorted", "singles", "duplicates", "invalid", "unknown_ids"):
            print(f"{key}: {len(local[key])}")
        comparison = None
        if args.inventory:
            albums = json.loads(args.inventory.read_text(encoding="utf-8-sig"))
            comparison = compare_inventory(local, albums)
        elif args.telegram:
            comparison = compare_inventory(local, asyncio.run(fetch_albums(args.group)))
        if comparison:
            for key, value in comparison.items():
                print(f"{key}: {len(value) if isinstance(value, list) else value}")
        if args.write_manifest:
            print(f"Manifest: {write_manifest(args.data_dir, local)}")
        issues = any(local[k] for k in ("unsorted", "singles", "duplicates", "invalid", "unknown_ids"))
        if comparison:
            issues = issues or any(comparison[k] for k in ("missing_ids", "extra_ids", "split_albums", "partial_albums"))
        print("Review needed." if issues else "No issues found by the selected checks; content correctness is not guaranteed.")
        return 1 if issues else 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
