"""Local paths, image discovery, atomic state writes and recoverable file moves."""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = PROJECT_DIR / "demo_data" / "sorted"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
SKIP_DIRS = {"_unsorted", "_singles", "_state"}


def enable_heif() -> bool:
    """JPEG/PNG remain usable when the optional HEIF plugin is unavailable."""
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return False
    register_heif_opener()
    return True


def image_paths(folder: Path, recursive: bool = True) -> list[Path]:
    folder = Path(folder)
    if not folder.is_dir():
        return []
    root = folder.resolve()
    candidates = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(
        (p for p in candidates if p.is_file() and not p.is_symlink()
         and p.suffix.lower() in IMAGE_EXTS and p.resolve().is_relative_to(root)),
        key=lambda p: str(p).casefold(),
    )


def safe_folder(root: Path, name: str) -> Path:
    """Accept one portable folder name, never an absolute or relative path."""
    root = Path(root).resolve()
    if not name or name != name.strip() or name in {".", ".."}:
        raise ValueError("Enter a folder name, not a path.")
    if any(c in name for c in '/\\:*?"<>|') or any(ord(c) < 32 for c in name):
        raise ValueError("Folder names cannot contain path separators or reserved characters.")
    if name.endswith((".", " ")) or name.startswith("_"):
        raise ValueError("Trailing dots/spaces and reserved _ folders are not allowed.")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", name):
        raise ValueError("This folder name is reserved by Windows.")
    result = root / name
    if result.is_symlink() or not result.resolve().is_relative_to(root):
        raise ValueError("The destination must stay inside the data directory.")
    if result.exists() and not result.is_dir():
        raise ValueError("The destination already exists as a file.")
    return result


def _atomic_write(path: Path, writer) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as stream:
            writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    _atomic_write(path, lambda stream: json.dump(value, stream, ensure_ascii=False, indent=2))


def append_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    """Preserve old columns while replacing the complete CSV atomically."""
    path = Path(path)
    previous = []
    columns = list(fields)
    if path.exists():
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            for field in reader.fieldnames or []:
                if field not in columns:
                    columns.append(field)
            previous = list(reader)
    def write(stream):
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="raise")
        writer.writeheader()
        writer.writerows(previous)
        writer.writerows(rows)
    _atomic_write(path, write)


def _event(root: Path, **event) -> None:
    folder = root / "_state"
    if folder.is_symlink() or not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError("Recovery journal cannot be outside the data directory.")
    folder.mkdir(exist_ok=True)
    path = folder / "moves.jsonl"
    if path.is_symlink():
        raise ValueError("Recovery journal must not be a symbolic link.")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), **event}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def move_batch(files: list[Path], destination: Path, root: Path) -> list[tuple[Path, Path]]:
    """Move files without overwriting; roll back ordinary failures within a batch.

    The journal survives abrupt exits, but is not an automatic undo mechanism.
    Concurrent writers to the same archive are not supported.
    """
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination == root or not destination.is_relative_to(root):
        raise ValueError("Destination must be a child folder of the data directory.")
    if not root.is_dir():
        raise ValueError("Data directory does not exist.")
    plan, used = [], set()
    for path in dict.fromkeys(Path(f) for f in files):
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("Every source must be an existing local file inside the data directory.")
        source = path.resolve()
        if source.parent == destination:
            plan.append((source, source))
            continue
        target = destination / source.name
        number = 1
        while target.exists() or target in used:
            target = destination / f"{source.stem}_dup{number}{source.suffix}"
            number += 1
        used.add(target)
        plan.append((source, target))
    if not plan:
        return []
    transaction = uuid.uuid4().hex
    _event(root, transaction=transaction, state="planned", moves=[{"source": str(a), "destination": str(b)} for a, b in plan])
    completed = []
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for source, target in plan:
            if source == target:
                continue
            if target.exists():
                raise FileExistsError("Destination changed during the operation; nothing will be overwritten.")
            shutil.move(str(source), str(target))
            completed.append((source, target))
            _event(root, transaction=transaction, state="file_moved", source=str(source), destination=str(target))
        _event(root, transaction=transaction, state="committed")
    except Exception:
        rollback_errors = []
        for source, target in reversed(completed):
            try:
                if source.exists():
                    raise FileExistsError("Original location is occupied.")
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(source))
            except Exception as error:
                rollback_errors.append(type(error).__name__)
        _event(root, transaction=transaction, state="rollback_incomplete" if rollback_errors else "rolled_back", errors=rollback_errors)
        if rollback_errors:
            raise RuntimeError("Rollback was incomplete. Stop and inspect _state/moves.jsonl.") from None
        raise
    return plan


def rename_or_merge(source: Path, name: str, root: Path) -> str:
    root = Path(root).resolve()
    source = Path(source)
    if source.is_symlink() or not source.resolve().is_relative_to(root) or source.resolve() == root:
        raise ValueError("Source folder must be inside the data directory.")
    source = source.resolve()
    target = safe_folder(root, name)
    if target == source:
        return "confirmed"
    if not source.is_dir():
        raise ValueError("Source folder no longer exists.")
    if not target.exists():
        transaction = uuid.uuid4().hex
        _event(root, transaction=transaction, state="rename_planned", source=str(source), destination=str(target))
        source.rename(target)
        _event(root, transaction=transaction, state="renamed", source=str(source), destination=str(target))
        return "renamed"
    # Flattening a nested folder implicitly could destroy album boundaries.
    if any(p.is_dir() for p in source.iterdir()):
        raise ValueError("Merge of nested folders needs an explicit review; no files were moved.")
    move_batch([p for p in source.iterdir() if p.is_file()], target, root)
    source.rmdir()
    return "merged"
