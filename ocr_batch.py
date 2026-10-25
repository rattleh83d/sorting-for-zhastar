"""Optional Google Vision hints. A preview is offline; --apply sends images."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
from PIL import Image, ImageOps
from shared import DEFAULT_DATA_DIR, atomic_json, enable_heif
import sorting_core as core

MAX_BYTES = 9 * 1024 * 1024


def extract_numbers(text: str, maximum: int = 405) -> list[int]:
    # Bound both sides: do not turn a year such as 2026 into folder 26.
    return list(dict.fromkeys(int(hit) for hit in re.findall(r"(?<!\d)\d{1,3}(?!\d)", text) if 1 <= int(hit) <= maximum))


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_bytes(path: Path) -> bytes:
    enable_heif()
    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
    # Invalid input fails locally; never send truncated bytes to the service.
    for _ in range(12):
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            if len(buffer.getvalue()) <= MAX_BYTES:
                return buffer.getvalue()
        if min(image.size) <= 1:
            break
        image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)
    raise ValueError("Image cannot be encoded within the request size limit.")


def successful(entry: dict | None, digest: str) -> bool:
    return bool(entry and entry.get("status") == "ok" and entry.get("sha256") == digest)


def run_batch(paths: list[Path], root: Path, cache_file: Path, recognize, force=False, maximum=405) -> dict:
    """The recognizer is injected so the local workflow can be tested offline."""
    if cache_file.is_symlink() or not cache_file.resolve().is_relative_to(root.resolve()):
        raise ValueError("OCR cache must remain inside the selected archive.")
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    counts = {"ok": 0, "error": 0, "cached": 0}
    for path in paths:
        key = str(path.resolve().relative_to(root.resolve()))
        digest = fingerprint(path)
        if not force and successful(cache.get(key), digest):
            counts["cached"] += 1
            continue
        try:
            text = recognize(path)
            cache[key] = {"status": "ok", "sha256": digest, "raw": text[:300], "nums": extract_numbers(text, maximum)}
            counts["ok"] += 1
        except Exception as exc:
            # Keep the exception type only, avoiding account details from provider messages.
            cache[key] = {"status": "error", "sha256": digest, "error_type": type(exc).__name__, "raw": "", "nums": []}
            counts["error"] += 1
        atomic_json(cache_file, cache)
    # Review UI consumes basenames only when they are unambiguous.
    # Keep a separate adapter instead of throwing away relative-path cache identity.
    names = {}
    for key, value in cache.items():
        if value.get("status") == "ok":
            names.setdefault(Path(key).name, []).append(value)
    hints = {name: entries[0] for name, entries in names.items() if len(entries) == 1}
    atomic_json(root / "batch_ocr.json", hints)
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--apply", action="store_true", help="Send orphan-cluster images to Google Vision; charges may apply")
    parser.add_argument("--force", action="store_true", help="Repeat successful cached requests too")
    parser.add_argument("--max-folder", type=int, default=405)
    args = parser.parse_args(argv)
    root = args.data_dir.expanduser().resolve()
    if not root.is_dir():
        parser.error("Data directory does not exist. Run python make_demo.py first.")
    if args.max_folder < 1:
        parser.error("--max-folder must be positive")
    core.configure_paths(root)
    units = [u for u in core.build_units_unsorted({}) if u["type"] == "D_ORPHAN"]
    paths = list(dict.fromkeys(path for unit in units for path in unit["member_files"]))
    print(f"Orphan clusters: {len(units)}; candidate images: {len(paths)}")
    if not args.apply:
        print("Preview only. No images uploaded. Use --apply to enable Google Vision.")
        return 0
    if not paths:
        return 0
    try:
        from google.cloud import vision
    except ImportError:
        parser.error("Install requirements-ocr.txt to enable Google Vision")
    try:
        client = vision.ImageAnnotatorClient()  # Application Default Credentials; no hardcoded key path.
        def recognize(path):
            response = client.text_detection(image=vision.Image(content=image_bytes(path)))
            if response.error.message:
                raise RuntimeError("Vision request failed")
            return response.full_text_annotation.text if response.full_text_annotation else ""
        counts = run_batch(paths, root, root / "_state" / "ocr_cache.json", recognize, args.force, args.max_folder)
    except Exception as exc:
        parser.error(f"OCR could not start ({type(exc).__name__}). Check local Application Default Credentials and permissions.")
    print(f"Recognized: {counts['ok']}; cached: {counts['cached']}; errors: {counts['error']}")
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
