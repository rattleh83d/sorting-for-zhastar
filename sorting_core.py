"""Photo grouping and review state. No network or window creation on import."""
import csv
import json
import re
from pathlib import Path
from shared import IMAGE_EXTS, SKIP_DIRS, DEFAULT_DATA_DIR, move_batch, rename_or_merge, append_csv, image_paths

CLUSTER_GAP = 3
INDEX_WINDOW = 8
NEARBY_RADIUS = 60
SPREAD_WARN = 500
NUM_RANGE = (1, 405)
LOG_FIELDS = ["mode", "unit_id", "folder_name", "filename", "action", "new_name", "timestamp", "source_path", "destination_path", "original_filename"]

def configure_paths(data_dir):
    global OUTPUT_DIR, UNSORTED_DIR, VISUAL_LOG, SORT_LOG, BATCH_OCR_CACHE
    OUTPUT_DIR = str(Path(data_dir).expanduser().resolve())
    UNSORTED_DIR = str(Path(OUTPUT_DIR) / "_unsorted")
    VISUAL_LOG = str(Path(OUTPUT_DIR) / "visual_log.csv")
    SORT_LOG = str(Path(OUTPUT_DIR) / "sort_log.csv")
    BATCH_OCR_CACHE = str(Path(OUTPUT_DIR) / "batch_ocr.json")

configure_paths(DEFAULT_DATA_DIR)

def msg_id(path: Path) -> int:
    m = re.match(r"msg_(\d+)", path.name, re.IGNORECASE)
    return int(m.group(1)) if m else 0

def is_jpg(path: Path) -> bool:
    return path.suffix.lower() in (".jpg", ".jpeg")

def is_numeric(name: str) -> bool:
    """True for pure-digit folder names (the sorted target names)."""
    return name.isdigit()

def index_candidate(paths: list) -> "Path | None":
    """Highest-msg_id JPG in the set — the probable index photo."""
    jpgs = [p for p in paths if is_jpg(p)]
    return max(jpgs, key=msg_id, default=None)

def iter_images(folder: Path) -> list:
    return sorted(image_paths(folder, recursive=True), key=lambda p: (msg_id(p), p.name))

def folder_sort_key(name: str):
    return (0, int(name)) if name.isdigit() else (1, name.lower())

def make_unit_id(member_files: list) -> str:
    return "U:" + "|".join(sorted(f.name for f in member_files))

def load_batch_ocr() -> dict:
    """Load Vision results from ocr_batch.py pre-pass. Keys = file basenames."""
    p = Path(BATCH_OCR_CACHE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_ocr_hints() -> dict:
    hints: dict = {}
    p = Path(SORT_LOG)
    if not p.exists():
        return hints
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dest = row.get("destination", "").strip()
            name = Path(dest).name if dest else ""
            if name and name not in hints:
                raw = row.get("ocr_raw_text", "").replace("\n", " ").strip()
                hints[name] = raw[:120]
    return hints

def load_terminal_state() -> tuple:
    """Returns (terminal_basenames: set[str], terminal_unit_ids: set[str]).
    Terminal = moved_to / confirmed / renamed / merged (NOT skipped).
    """
    terminal_basenames: set = set()
    terminal_unit_ids: set = set()
    p = Path(VISUAL_LOG)
    if not p.exists():
        return terminal_basenames, terminal_unit_ids
    terminal_actions = {"moved_to", "confirmed", "renamed", "merged"}
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("action") in terminal_actions:
                fn = row.get("filename", "").strip()
                if fn:
                    terminal_basenames.add(fn)
                uid = row.get("unit_id", "").strip()
                if uid:
                    terminal_unit_ids.add(uid)
    return terminal_basenames, terminal_unit_ids

def load_done_folder_names(mode: str) -> set:
    # Older archives used assign/review; only review maps to the folder pass.
    modes = {mode, "review"} if mode == "folders" else {mode}
    p = Path(VISUAL_LOG)
    if not p.exists():
        return set()
    done = set()
    with p.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if row.get("mode") in modes and row.get("action") in {"confirmed", "renamed", "merged"}:
                name = row.get("new_name") if row.get("action") in {"renamed", "merged"} else row.get("folder_name")
                if name:
                    done.add(name)
    return done

def append_log(rows: list) -> None:
    append_csv(Path(VISUAL_LOG), LOG_FIELDS, rows)

def _cluster_unsorted(terminal_basenames: set) -> list:
    # Files still physically in _unsorted must remain visible even with a stale log.
    root = Path(UNSORTED_DIR)
    if not root.exists():
        return []
    by_parent = {}
    for file in image_paths(root, recursive=True):
        by_parent.setdefault(file.parent, []).append(file)
    clusters = []
    for parent, files in sorted(by_parent.items(), key=lambda item: str(item[0])):
        files.sort(key=lambda f: (msg_id(f), f.name))
        if parent != root:
            # Explicit batch folders retain their album boundaries.
            clusters.append(files)
            continue
        current = []
        for file in files:
            fid = msg_id(file)
            if current and (not fid or not msg_id(current[-1]) or fid - msg_id(current[-1]) > CLUSTER_GAP):
                clusters.append(current)
                current = []
            current.append(file)
        if current:
            clusters.append(current)
    return clusters

def _suspicious_flags(imgs: list) -> str:
    if not imgs:
        return "⚠ EMPTY"
    flags = []
    if len(imgs) == 1:
        flags.append("SINGLE FILE")
    if not any(is_jpg(f) for f in imgs):
        flags.append("NO JPG INDEX")
    ids = sorted(msg_id(f) for f in imgs if msg_id(f))
    if len(ids) >= 2 and (ids[-1] - ids[0]) > SPREAD_WARN:
        flags.append(f"SPREAD {ids[-1] - ids[0]}")
    return ("⚠ " + " · ".join(flags)) if flags else ""

def _make_unit(
    utype, display_type, index_photo, index_source, batch_paths,
    member_files, flags, suggested_name, confidence,
    current_locations, suggestion_basis="", ocr_hint="", folder=None,
):
    ids = sorted(msg_id(f) for f in member_files if msg_id(f))
    images_ordered = []
    if index_photo and index_photo in member_files:
        images_ordered = [index_photo] + [f for f in sorted(member_files, key=msg_id) if f != index_photo]
    else:
        images_ordered = sorted(member_files, key=msg_id)

    hint_parts = []
    if ids:
        hint_parts.append(f"msg {ids[0]}–{ids[-1]}")
    if suggestion_basis:
        hint_parts.append(suggestion_basis)
    if ocr_hint:
        hint_parts.append(f"OCR: {ocr_hint}")
    if flags:
        hint_parts.append("flags: " + ", ".join(flags))

    return {
        "type":             utype,
        "display_type":     display_type,
        "unit_id":          make_unit_id(member_files),
        "index_photo":      index_photo,
        "index_source":     index_source,
        "images":           images_ordered,
        "folder":           folder,
        "member_files":     list(member_files),
        "batch_paths":      list(batch_paths),
        "current_locations": list(current_locations),
        "suggested_name":   suggested_name,
        "suggestion_basis": suggestion_basis,
        "confidence":       confidence,
        "flags":            list(flags),
        "msg_id_lo":        ids[0] if ids else 0,
        "msg_id_hi":        ids[-1] if ids else 0,
        "hint":             "   ·   ".join(hint_parts),
    }

def _get_ocr_hint(files: list, ocr_hints: dict) -> str:
    for f in sorted(files, key=msg_id, reverse=True):
        h = ocr_hints.get(f.name) or ocr_hints.get(f.stem)
        if h:
            return h
    return ""

def build_units_unsorted(ocr_hints: dict) -> list:
    terminal_basenames, terminal_unit_ids = load_terminal_state()

    # --- Stage 0: Build index of single-file folders (each has exactly 1 JPG) ---
    single_file_map: dict = {}   # folder_name → jpg_path
    for d in Path(OUTPUT_DIR).iterdir():
        if not d.is_dir() or d.name.startswith("_") or d.name in SKIP_DIRS:
            continue
        files = image_paths(d, recursive=True)
        if len(files) == 1 and is_jpg(files[0]) and files[0].name not in terminal_basenames:
            single_file_map[d.name] = files[0]

    # --- Stage 1: Cluster _unsorted (GAP=3) ---
    clusters = _cluster_unsorted(terminal_basenames)
    units = []
    claimed_single_file_folders: set = set()  # folder names consumed by Type A matches

    for cluster in clusters:
        if not cluster:
            continue

        jpgs  = [f for f in cluster if is_jpg(f)]
        heics = [f for f in cluster if not is_jpg(f)]
        n_hi  = max(msg_id(f) for f in cluster)
        n_lo  = min(msg_id(f) for f in cluster)
        flags = []

        # --- Stage 2: Index inside the cluster? ---
        if jpgs and heics:
            # Mixed cluster: JPG is the index photo
            idx = index_candidate(cluster)
            if idx and msg_id(idx) != n_hi:
                flags.append("JPG_NOT_LAST")
            if len(jpgs) > 1:
                flags.append("MULTI_JPG")
            batch = [f for f in cluster if f != idx]
            unit = _make_unit(
                "B_INDEX_IN_CLUSTER", "READ_NUMBER",
                index_photo=idx, index_source="IN_CLUSTER",
                batch_paths=batch, member_files=cluster, flags=flags,
                suggested_name=None, confidence="LOW",
                current_locations=["_unsorted"],
                ocr_hint=_get_ocr_hint(cluster, ocr_hints),
            )
            units.append(unit)
            continue

        if jpgs and not heics:
            # All-JPG cluster: ambiguous — treat highest-msg_id JPG as index
            idx = index_candidate(cluster)
            if len(cluster) == 1:
                flags.append("SIZE_1")
            if len(jpgs) > 1:
                flags.append("MULTI_JPG")
            batch = [f for f in cluster if f != idx]
            unit = _make_unit(
                "B_INDEX_IN_CLUSTER", "READ_NUMBER",
                index_photo=idx, index_source="IN_CLUSTER",
                batch_paths=batch, member_files=cluster, flags=flags,
                suggested_name=None, confidence="LOW",
                current_locations=["_unsorted"],
                ocr_hint=_get_ocr_hint(cluster, ocr_hints),
            )
            units.append(unit)
            continue

        # --- Stage 3: HEIC-only cluster — pair with adjacent single-file index folder ---
        if len(cluster) == 1:
            flags.append("SIZE_1")

        forward_candidates = []
        for fname, jpg_path in single_file_map.items():
            if fname in claimed_single_file_folders:
                continue
            fid = msg_id(jpg_path)
            if n_hi + 1 <= fid <= n_hi + INDEX_WINDOW:
                forward_candidates.append((fid - n_hi, fname, jpg_path))

        backward_candidates = []
        if not forward_candidates:
            for fname, jpg_path in single_file_map.items():
                if fname in claimed_single_file_folders:
                    continue
                fid = msg_id(jpg_path)
                if n_lo - INDEX_WINDOW <= fid <= n_lo - 1:
                    backward_candidates.append((n_lo - fid, fname, jpg_path))

        candidates = forward_candidates or backward_candidates
        candidates.sort(key=lambda x: x[0])

        if candidates:
            best_gap, best_fname, best_jpg = candidates[0]
            if len(candidates) > 1 and candidates[1][0] == best_gap:
                conf = "MEDIUM"
                alts = [c[1] for c in candidates[:3]]
                basis = f"tied: {', '.join(alts)} (gap {best_gap}) — pick one"
            else:
                conf = "HIGH"
                basis = f"folder {best_fname} adjacent (msg gap {best_gap})"

            member_files = cluster + [best_jpg]
            uid = make_unit_id(member_files)
            claimed_single_file_folders.add(best_fname)
            unit = _make_unit(
                "A_HIGH_CONF", "CONFIRM",
                index_photo=best_jpg, index_source="IN_FOLDER",
                batch_paths=cluster, member_files=member_files, flags=flags,
                suggested_name=best_fname, confidence=conf,
                current_locations=["_unsorted", f"sorted/{best_fname}"],
                suggestion_basis=basis,
                ocr_hint=_get_ocr_hint(cluster, ocr_hints),
            )
            units.append(unit)
            continue

        # --- Stage 5: No match — orphan ---
        unit = _make_unit(
            "D_ORPHAN", "IDENTIFY",
            index_photo=None, index_source="NONE",
            batch_paths=cluster, member_files=cluster, flags=flags,
            suggested_name=None, confidence="NONE",
            current_locations=["_unsorted"],
            ocr_hint=_get_ocr_hint(cluster, ocr_hints),
        )
        units.append(unit)

    # --- Stage 4: Named folders (non-numeric, non-_-prefixed) ---
    done_folder_names = load_done_folder_names("folders")
    for d in sorted(Path(OUTPUT_DIR).iterdir(), key=lambda p: folder_sort_key(p.name)):
        if not d.is_dir() or d.name.startswith("_") or d.name in SKIP_DIRS:
            continue
        if is_numeric(d.name):
            continue
        if d.name in done_folder_names:
            continue

        files = [
            f for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS

        ]
        if not files:
            continue

        idx = index_candidate(files)
        flags = []
        if len(files) == 1:
            flags.append("SIZE_1")
        if not idx:
            flags.append("NO_JPG")

        utype     = "C_NAMED_FOLDER" if idx else "D_ORPHAN"
        disp_type = "READ_NUMBER"    if idx else "IDENTIFY"
        src       = "NAMED_FOLDER_LAST_JPG" if idx else "NONE"

        unit = _make_unit(
            utype, disp_type,
            index_photo=idx, index_source=src,
            batch_paths=[f for f in files if f != idx],
            member_files=files, flags=flags,
            suggested_name=None, confidence="NONE",
            current_locations=[str(d)],
            suggestion_basis=f'named folder "{d.name}"',
            ocr_hint=_get_ocr_hint(files, ocr_hints),
            folder=d,
        )
        units.append(unit)

    # --- Sort effort-ascending: A-HIGH → A-MEDIUM → B-clean → B-flagged → C → D-large → D-tiny ---
    type_order = {
        "A_HIGH_CONF": 0, "B_INDEX_IN_CLUSTER": 2,
        "C_NAMED_FOLDER": 3, "D_ORPHAN": 4,
    }
    conf_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}

    def _sort_key(u):
        t = type_order.get(u["type"], 5)
        c = conf_order.get(u["confidence"], 3)
        b_flag = 1 if u["type"] == "B_INDEX_IN_CLUSTER" and (
            "JPG_NOT_LAST" in u["flags"] or "MULTI_JPG" in u["flags"]
        ) else 0
        # D_ORPHAN: larger batches first (more visual information)
        d_size = -len(u["batch_paths"]) if u["type"] == "D_ORPHAN" else 0
        name = u.get("suggested_name") or ""
        name_num = int(name) if name.isdigit() else 9999
        return (t, c, b_flag, d_size, name_num)

    units.sort(key=_sort_key)
    return units

def build_units_folders(ocr_hints: dict) -> list:
    """Phase B: walk numbered folders, flag suspicious ones for review."""
    done = load_done_folder_names("folders")
    units = []

    severity = {
        "⚠ EMPTY": 0,
        "SPREAD": 0,        # partial match
        "NO JPG INDEX": 1,
        "SINGLE FILE": 1,
        "MULTI_JPG": 2,
    }

    for d in sorted(Path(OUTPUT_DIR).iterdir(), key=lambda p: folder_sort_key(p.name)):
        if not d.is_dir() or d.name.startswith("_") or d.name in SKIP_DIRS:
            continue
        if not is_numeric(d.name):
            continue
        if d.name in done:
            continue
        imgs = iter_images(d)
        flags_str = _suspicious_flags(imgs)
        if not flags_str:
            continue  # healthy folder — auto-pass, don't enqueue

        ids = sorted(msg_id(f) for f in imgs if msg_id(f))
        sev = 2
        for k, v in severity.items():
            if k in flags_str:
                sev = min(sev, v)

        unit = {
            "type":          "FOLDER_FLAG",
            "display_type":  "CONFIRM",
            "unit_id":       f"F:{d.name}",
            "index_photo":   index_candidate(imgs),
            "index_source":  "IN_FOLDER",
            "images":        imgs,
            "folder":        d,
            "member_files":  imgs,
            "batch_paths":   [f for f in imgs if not is_jpg(f)],
            "current_locations": [str(d)],
            "suggested_name": d.name,
            "suggestion_basis": "",
            "confidence":    "HIGH",
            "flags":         [flags_str],
            "msg_id_lo":     ids[0] if ids else 0,
            "msg_id_hi":     ids[-1] if ids else 0,
            "hint":          flags_str + (f"   msg {ids[0]}–{ids[-1]}" if ids else ""),
            "severity":      sev,
        }
        units.append(unit)

    # Add named (non-numeric) folders — let user confirm as-is or rename to a number
    named_done = load_done_folder_names("folders")
    for d in sorted(Path(OUTPUT_DIR).iterdir(), key=lambda p: p.name.lower()):
        if not d.is_dir() or d.name.startswith("_") or d.name in SKIP_DIRS:
            continue
        if is_numeric(d.name) or d.name.startswith("_"):
            continue
        if not any(True for _ in d.iterdir()):
            continue
        if d.name in named_done:
            continue
        imgs = iter_images(d)
        ids = sorted(msg_id(f) for f in imgs if msg_id(f))
        units.append({
            "type":           "NAMED_FOLDER",
            "display_type":   "CONFIRM",
            "unit_id":        f"F:{d.name}",
            "index_photo":    index_candidate(imgs),
            "index_source":   "IN_FOLDER",
            "images":         imgs,
            "folder":         d,
            "member_files":   imgs,
            "batch_paths":    [f for f in imgs if not is_jpg(f)],
            "current_locations": [str(d)],
            "suggested_name": d.name,
            "suggestion_basis": "named folder",
            "confidence":     "LOW",
            "flags":          ["NAMED — assign number or confirm as-is"],
            "msg_id_lo":      ids[0] if ids else 0,
            "msg_id_hi":      ids[-1] if ids else 0,
            "hint":           f"Named folder '{d.name}' — type a number to reassign, or Enter to keep name",
            "severity":       -1,
        })

    units.sort(key=lambda u: (u.get("severity", 2), u["msg_id_lo"]))
    return units

def build_units_audit() -> list:
    """Phase C: every numeric folder, strict numeric order, skip already-confirmed."""
    done = load_done_folder_names("audit")
    units = []
    for d in sorted(
        Path(OUTPUT_DIR).iterdir(),
        key=lambda p: (0, int(p.name)) if p.name.isdigit() else (1,),
    ):
        if not d.is_dir() or d.name in SKIP_DIRS or not d.name.isdigit():
            continue
        if d.name in done:
            continue
        imgs = iter_images(d)
        flags_str = _suspicious_flags(imgs)
        ids = sorted(msg_id(f) for f in imgs if msg_id(f))
        units.append({
            "type":           "AUDIT",
            "display_type":   "AUDIT",
            "unit_id":        f"A:{d.name}",
            "index_photo":    index_candidate(imgs),
            "index_source":   "IN_FOLDER",
            "images":         imgs,
            "folder":         d,
            "member_files":   imgs,
            "batch_paths":    [f for f in imgs if not is_jpg(f)],
            "current_locations": [str(d)],
            "suggested_name": d.name,
            "suggestion_basis": "",
            "confidence":     "HIGH",
            "flags":          [flags_str] if flags_str else [],
            "msg_id_lo":      ids[0] if ids else 0,
            "msg_id_hi":      ids[-1] if ids else 0,
            "hint":           (flags_str + "   " if flags_str else "") + (f"msg {ids[0]}–{ids[-1]}   {len(imgs)} photo{'s' if len(imgs)!=1 else ''}" if ids else f"{len(imgs)} photo{'s' if len(imgs)!=1 else ''}"),
            "severity":       0,
        })
    return units

def build_queue(mode: str, ocr_hints: dict) -> list:
    if mode == "unsorted":
        return build_units_unsorted(ocr_hints)
    if mode == "audit":
        return build_units_audit()
    return build_units_folders(ocr_hints)

def build_folder_id_map() -> dict:
    result: dict = {}
    for d in Path(OUTPUT_DIR).iterdir():
        if not d.is_dir() or d.name.startswith("_") or d.name in SKIP_DIRS:
            continue
        for f in d.iterdir():
            if f.is_file():
                fid = msg_id(f)
                if fid:
                    result[fid] = d.name
    return result

def phase_a_complete() -> tuple:
    unsorted_count = len(image_paths(Path(UNSORTED_DIR), recursive=True))
    root = Path(OUTPUT_DIR)
    named_count = sum(1 for d in root.iterdir() if d.is_dir() and not d.name.startswith("_") and not is_numeric(d.name) and image_paths(d)) if root.exists() else 0
    return unsorted_count == 0, unsorted_count, named_count

def safe_move(src: Path, dest_dir: Path) -> Path:
    return move_batch([src], dest_dir, Path(OUTPUT_DIR))[0][1]

def do_rename_or_merge(src: Path, new_name: str) -> str:
    return rename_or_merge(src, new_name, Path(OUTPUT_DIR))

def cleanup_empty_sources(member_files: list, target_dir: Path) -> None:
    root = Path(OUTPUT_DIR).resolve()
    for source in {Path(f).parent for f in member_files}:
        source = source.resolve()
        if source == root or source == target_dir.resolve() or not source.is_relative_to(root):
            continue
        while source != root and source.name not in SKIP_DIRS:
            try:
                source.rmdir()
            except OSError:
                break
            source = source.parent

