"""
visual_review.py — index-centric photo sort tool.

Every album has an INDEX PHOTO (JPG with a painted number) and a BATCH (HEIC photos).
This tool reunites them into numbered folders (1–405).

Phase A — Unsorted: resolve all unplaced albums in effort-ascending order:
  Type A  HIGH CONF  — HEIC batch matched to adjacent single-file index folder (just confirm)
  Type B  READ NUM   — index JPG is inside the cluster; read the number off it
  Type C  NAMED      — named folder with a JPG; read the number off it
  Type D  ORPHAN     — no identifiable index; identify visually

Phase B — Folders: full pass of every numbered folder (gated: Phase A must be complete).

CLI:
  python visual_review.py --report   → write sorted/manifest.csv, no GUI
"""

import csv
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
import tkinter as tk

from PIL import Image, ImageOps, ImageTk

from shared import DEFAULT_DATA_DIR, IMAGE_EXTS, enable_heif, safe_folder, move_batch
import sorting_core as core
from sorting_core import msg_id, is_numeric, index_candidate, folder_sort_key, load_batch_ocr, load_ocr_hints, load_done_folder_names, append_log, build_queue, build_folder_id_map, phase_a_complete, do_rename_or_merge, cleanup_empty_sources
enable_heif()
THUMB_SIZE = (88, 88)
MAIN_MAX_W, MAIN_MAX_H = 560, 480
COMPARE_W = 240
THUMB_CACHE_MAX = 500
CLUSTER_GAP, INDEX_WINDOW, NEARBY_RADIUS, SPREAD_WARN = 3, 8, 60, 500
NUM_RANGE = (1, 405)

def configure_paths(data_dir):
    core.configure_paths(data_dir)
    globals().update({key: getattr(core, key) for key in ("OUTPUT_DIR", "UNSORTED_DIR", "VISUAL_LOG", "SORT_LOG", "BATCH_OCR_CACHE")})

configure_paths(DEFAULT_DATA_DIR)

_thumb_cache: OrderedDict = OrderedDict()


def _get_thumb(path: Path, size=(88, 88)):
    key = (path, size)
    if key in _thumb_cache:
        _thumb_cache.move_to_end(key)
        return _thumb_cache[key]
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail(size, Image.BILINEAR)
        ref = ImageTk.PhotoImage(img)
    except Exception:
        ref = None
    _thumb_cache[key] = ref
    if len(_thumb_cache) > THUMB_CACHE_MAX:
        _thumb_cache.popitem(last=False)
    return ref




# UI helpers

def _all_widgets(widget) -> list:
    result = [widget]
    for child in widget.winfo_children():
        result.extend(_all_widgets(child))
    return result


# Main GUI

class VisualReviewApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.mode = tk.StringVar(value="unsorted")
        self.ocr_hints  = load_ocr_hints()
        self.batch_ocr  = load_batch_ocr()
        self._folder_id_map: dict = {}
        self._compare_refs: list = []

        self.queue: list = []
        self.idx     = 0
        self.img_idx = 0
        self.images: list = []
        self.selected: set = set()
        self._main_ref = None
        self._pending_path = None
        self._item_hint = ""
        self._preview_after: str = ""   # after() id for debounced preview

        self._build_ui()
        self._reload_queue()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = self.root
        root.configure(bg="#1e1e1e")
        root.resizable(True, True)
        root.minsize(920, 700)

        mono  = tkfont.Font(family="Consolas", size=11)
        large = tkfont.Font(weight="bold", size=13)
        small = tkfont.Font(size=9)

        # ── top bar ──────────────────────────────────────────────────
        top = tk.Frame(root, bg="#2d2d2d", pady=4)
        top.pack(fill="x")

        for val, txt in (
            ("unsorted", "Phase A — Resolve albums"),
            ("folders",  "Phase B — Verify folders"),
            ("audit",    "Phase C — Audit every folder"),
        ):
            tk.Radiobutton(
                top, text=txt, variable=self.mode, value=val,
                bg="#2d2d2d", fg="white", selectcolor="#444",
                activebackground="#2d2d2d", activeforeground="white",
                command=self._reload_queue,
            ).pack(side="left", padx=(10, 2))

        self.lbl_progress = tk.Label(top, text="", bg="#2d2d2d", fg="#aaa", font=small)
        self.lbl_progress.pack(side="right", padx=10)
        self.lbl_folder = tk.Label(top, text="", bg="#2d2d2d", fg="white", font=large)
        self.lbl_folder.pack(side="left", padx=20)

        # ── MIDDLE: left image + right compare ───────────────────────
        mid = tk.Frame(root, bg="#1e1e1e")
        mid.pack(fill="both", expand=True)

        left = tk.Frame(mid, bg="#1e1e1e")
        left.pack(side="left", fill="both", expand=True)

        img_frame = tk.Frame(left, bg="#1e1e1e", height=MAIN_MAX_H)
        img_frame.pack(fill="both", expand=True, padx=(10, 4), pady=(6, 0))
        img_frame.pack_propagate(False)
        self.lbl_image = tk.Label(img_frame, bg="#1e1e1e")
        self.lbl_image.pack(expand=True)

        self.lbl_img_counter = tk.Label(left, text="", bg="#1e1e1e", fg="#888", font=small)
        self.lbl_img_counter.pack()

        self.frm_thumbs = tk.Frame(left, bg="#2a2a2a", pady=4)
        self.frm_thumbs.pack(fill="x", padx=(10, 4), pady=(4, 0))

        self.frm_compare = tk.Frame(mid, bg="#161616", width=COMPARE_W)
        self.frm_compare.pack_propagate(False)
        self.frm_compare.pack(side="right", fill="y", padx=(2, 6), pady=(6, 0))

        # ── hint bar ─────────────────────────────────────────────────
        self.lbl_hint = tk.Label(
            root, text="", bg="#1e1e1e", fg="#e0a830", font=small, anchor="w", wraplength=880,
        )
        self.lbl_hint.pack(fill="x", padx=12, pady=(4, 0))

        # ── type badge ───────────────────────────────────────────────
        self.lbl_type = tk.Label(root, text="", bg="#1e1e1e", font=small, anchor="w")
        self.lbl_type.pack(fill="x", padx=12)

        # ── name entry ───────────────────────────────────────────────
        row1 = tk.Frame(root, bg="#1e1e1e", pady=4)
        row1.pack(fill="x", padx=10)
        tk.Label(row1, text="Folder name:", bg="#1e1e1e", fg="#ccc").pack(side="left")
        self.entry_var = tk.StringVar()
        self.entry = tk.Entry(
            row1, textvariable=self.entry_var, font=mono,
            width=18, bg="#333", fg="white", insertbackground="white",
        )
        self.entry.pack(side="left", padx=(6, 8))
        self.lbl_err = tk.Label(row1, text="", fg="#f66", bg="#1e1e1e", font=small)
        self.lbl_err.pack(side="left")
        tk.Button(row1, text="Confirm  ↵", width=12, command=self.confirm,
                  bg="#3a7d44", fg="white", activebackground="#2e6438",
                  takefocus=0).pack(side="left", padx=4)
        tk.Button(row1, text="Skip  Esc", width=10, command=self.skip,
                  bg="#555", fg="white", activebackground="#666",
                  takefocus=0).pack(side="left", padx=4)

        # ── scatter row ──────────────────────────────────────────────
        row2 = tk.Frame(root, bg="#1e1e1e", pady=2)
        row2.pack(fill="x", padx=10)
        tk.Label(row2, text="Move selected to:", bg="#1e1e1e", fg="#ccc").pack(side="left")
        self.move_var = tk.StringVar()
        self.move_var.trace_add("write", self._on_move_var_change)
        self.move_entry = tk.Entry(
            row2, textvariable=self.move_var, font=mono,
            width=14, bg="#333", fg="white", insertbackground="white",
        )
        self.move_entry.pack(side="left", padx=(6, 8))
        tk.Button(row2, text="Move  Ctrl+M", command=self.move_selected,
                  bg="#7d5a3a", fg="white", activebackground="#634828",
                  takefocus=0).pack(side="left", padx=4)
        self.lbl_sel = tk.Label(row2, text="", bg="#1e1e1e", fg="#aaa", font=small)
        self.lbl_sel.pack(side="left", padx=8)

        # ── nav ──────────────────────────────────────────────────────
        nav = tk.Frame(root, bg="#1e1e1e", pady=6)
        nav.pack(fill="x", padx=10, pady=(2, 8))
        tk.Button(nav, text="◄◄ Prev  Ctrl+←",
                  command=lambda: self.navigate_folder(-1),
                  bg="#333", fg="white", takefocus=0).pack(side="left")
        tk.Button(nav, text="Next  Ctrl+→ ►►",
                  command=lambda: self.navigate_folder(1),
                  bg="#333", fg="white", takefocus=0).pack(side="right")

        # ── bindings ─────────────────────────────────────────────────
        root.bind("<Return>",        lambda e: self.confirm())
        root.bind("<Escape>",        lambda e: self.skip())
        root.bind("<Left>",          lambda e: self._show_image(self.img_idx - 1))
        root.bind("<Right>",         lambda e: self._show_image(self.img_idx + 1))
        root.bind("<Control-Left>",  lambda e: self.navigate_folder(-1))
        root.bind("<Control-Right>", lambda e: self.navigate_folder(1))
        root.bind("<Control-m>",     lambda e: self.move_selected())
        root.bind("<Control-M>",     lambda e: self.move_selected())
        root.bind("<Control-b>",     lambda e: self._open_folder_browser())
        root.bind("<Control-B>",     lambda e: self._open_folder_browser())
        root.bind("<Control-a>",     lambda e: self.select_all())
        root.bind("<Control-A>",     lambda e: self.select_all())
        root.bind("<Control-j>",     lambda e: self._jump_to_folder())
        root.bind("<Control-J>",     lambda e: self._jump_to_folder())
        root.bind("<Key>",           self._on_key)
        for entry in (self.entry, self.move_entry):
            entry.bind("<Left>",          self._entry_left)
            entry.bind("<Right>",         self._entry_right)
            entry.bind("<Control-Left>",  lambda e: (self.navigate_folder(-1), "break")[1])
            entry.bind("<Control-Right>", lambda e: (self.navigate_folder(1),  "break")[1])

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def _reload_queue(self):
        mode = self.mode.get()
        if mode == "unsorted":
            self._folder_id_map = build_folder_id_map()
            self.frm_compare.pack(side="right", fill="y", padx=(2, 6), pady=(6, 0))
        elif mode == "audit":
            self._folder_id_map = build_folder_id_map()
            self.frm_compare.pack(side="right", fill="y", padx=(2, 6), pady=(6, 0))
        else:
            # Phase B gate
            ok, unsorted_n, named_n = phase_a_complete()
            if not ok:
                self.frm_compare.pack_forget()
                self._show_gate_warning(unsorted_n, named_n)
                return
            self._folder_id_map = build_folder_id_map()
            self.frm_compare.pack(side="right", fill="y", padx=(2, 6), pady=(6, 0))

        self.queue = build_queue(mode, self.ocr_hints)
        self.idx = 0
        if self.queue:
            self._load_item(0)
        else:
            self._show_empty()

    def _show_gate_warning(self, unsorted_n: int, named_n: int):
        self.lbl_folder.config(text="Phase A not complete", fg="#f66")
        parts = []
        if unsorted_n:
            parts.append(f"{unsorted_n} photos still in _unsorted")
        self.lbl_hint.config(text="Finish Phase A first: " + " · ".join(parts))
        self.lbl_type.config(text="")
        self.lbl_progress.config(text="")
        self.entry_var.set("")
        self.lbl_image.config(image="", text="Complete Phase A first",
                               fg="#f66", font=tkfont.Font(size=14))
        self._main_ref = None
        for w in self.frm_thumbs.winfo_children():
            w.destroy()
        for w in self.frm_compare.winfo_children():
            w.destroy()

    def _show_empty(self):
        self.lbl_folder.config(text="— all done —", fg="#5f5")
        self.lbl_progress.config(text="")
        self.lbl_type.config(text="")
        self.entry_var.set("")
        self._main_ref = None
        for w in self.frm_thumbs.winfo_children():
            w.destroy()
        for w in self.frm_compare.winfo_children():
            w.destroy()

        if self.mode.get() == "audit":
            # Build audit summary from log
            ok_folders, flagged = [], []
            p = Path(VISUAL_LOG)
            if p.exists():
                with open(p, newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        if row.get("mode") == "audit":
                            if row.get("action") == "confirmed":
                                ok_folders.append(row.get("folder_name", ""))
                            elif row.get("action") == "flagged":
                                note = row.get("new_name", "")
                                flagged.append(f"  {row.get('folder_name', '')}  {('— ' + note) if note else ''}")
            summary = f"Audit complete\n{len(ok_folders)} confirmed   {len(flagged)} flagged"
            if flagged:
                summary += "\n\nFlagged folders:\n" + "\n".join(flagged)
            self.lbl_image.config(
                image="", text=summary,
                fg="white", font=tkfont.Font(size=13),
            )
            self.lbl_hint.config(text="Run --report for the final deliverable manifest.")
        else:
            self.lbl_image.config(
                image="", text="All done!",
                fg="white", font=tkfont.Font(size=18),
            )
            self.lbl_hint.config(text="")

    # ------------------------------------------------------------------
    # Item loading
    # ------------------------------------------------------------------

    TYPE_LABELS = {
        "A_HIGH_CONF":        ("✓ HIGH CONFIDENCE MATCH",  "#4ade80", "CONFIRM"),
        "B_INDEX_IN_CLUSTER": ("📷 READ INDEX PHOTO",       "#facc15", "READ_NUMBER"),
        "C_NAMED_FOLDER":     ("📁 NAMED FOLDER — READ #",  "#fb923c", "READ_NUMBER"),
        "D_ORPHAN":           ("❓ NO INDEX — IDENTIFY",    "#f87171", "IDENTIFY"),
        "FOLDER_FLAG":        ("⚠ FLAGGED FOLDER",          "#f0d060", "CONFIRM"),
        "AUDIT":              ("AUDIT",                      "#7dd3fc", "AUDIT"),
    }

    def _load_item(self, idx: int):
        if idx < 0 or idx >= len(self.queue):
            self._show_empty()
            return

        self.idx    = idx
        item        = self.queue[idx]
        self.images = item["images"]
        self.selected.clear()

        # Set img_idx to show index photo first
        idx_photo = item.get("index_photo")
        if idx_photo and idx_photo in self.images:
            self.img_idx = self.images.index(idx_photo)
        else:
            self.img_idx = 0

        folder = item.get("folder")
        utype  = item.get("type", "")
        mode   = self.mode.get()

        # Progress
        done_count = len(load_done_folder_names(mode))
        self.lbl_progress.config(text=f"{idx+1} / {len(self.queue)}  ✓ {done_count}")

        # Type badge
        label_text, label_fg, _ = self.TYPE_LABELS.get(utype, ("", "#aaa", ""))
        self.lbl_type.config(text=label_text, fg=label_fg, bg="#1e1e1e")

        # Folder label
        if utype == "A_HIGH_CONF":
            sugg = item.get("suggested_name", "?")
            conf = item.get("confidence", "")
            color = "#4ade80" if conf == "HIGH" else "#facc15"
            self.lbl_folder.config(
                text=f"→ folder {sugg}   ({conf})",
                fg=color,
            )
        elif utype == "C_NAMED_FOLDER" and folder:
            self.lbl_folder.config(text=f'"{folder.name}"', fg="#fb923c")
        elif utype == "FOLDER_FLAG" and folder:
            self.lbl_folder.config(text=f"📁 {folder.name}", fg="#f0d060")
        elif utype == "AUDIT" and folder:
            flags = item.get("flags", [])
            flag_txt = ("  [" + flags[0] + "]") if flags and flags[0] else ""
            color = "#f0d060" if flag_txt else "#7dd3fc"
            self.lbl_folder.config(text=f"{folder.name}{flag_txt}", fg=color)
        else:
            n = len(self.images)
            self.lbl_folder.config(
                text=f"{'📷' if idx_photo else '❓'} {n} photo{'s' if n != 1 else ''}",
                fg="white",
            )

        # Hint bar (base — may be overridden per-photo in _show_image)
        self._item_hint = item.get("hint", "")
        self.lbl_hint.config(text=self._item_hint)

        # Entry pre-fill
        sugg = item.get("suggested_name") or ""
        if mode == "folders" and folder:
            sugg = folder.name

        # For D_ORPHAN: check batch_ocr consensus across member photos
        if utype == "D_ORPHAN" and self.batch_ocr:
            hits: dict = {}
            for f in item.get("member_files", []):
                for n in self.batch_ocr.get(f.name, {}).get("nums", []):
                    hits[n] = hits.get(n, 0) + 1
            if hits:
                best  = max(hits, key=hits.get)
                count = hits[best]
                total = len(item.get("member_files", []))
                sugg  = str(best)
                self._item_hint = (
                    f"Vision found {best} in {count}/{total} photos"
                    + (f"  (also seen: {sorted(k for k in hits if k != best)})"
                       if len(hits) > 1 else "")
                )
                self.lbl_hint.config(text=self._item_hint)

        self.entry_var.set(sugg)
        self.lbl_err.config(text="")
        self.move_var.set("")
        self.lbl_sel.config(text="")

        self._build_thumbs()
        self._build_compare_panel(item)

        if not self.images:
            self.lbl_image.config(image="", text="(no photos)",
                                   fg="#888", font=tkfont.Font(size=14))
            self._main_ref = None
            self.lbl_img_counter.config(text="")
        else:
            self._show_image(self.img_idx)

        self.entry.focus_set()

    # ------------------------------------------------------------------
    # Compare panel
    # ------------------------------------------------------------------

    def _on_move_var_change(self, *_):
        """Debounced trace: preview the typed folder number in the compare panel."""
        if self._preview_after:
            try:
                self.root.after_cancel(self._preview_after)
            except Exception:
                pass
        self._preview_after = self.root.after(120, self._apply_preview)

    def _apply_preview(self):
        val = self.move_var.get().strip()
        if val and is_numeric(val):
            self._build_compare_folder_preview(val)
        elif not val and self.queue:
            self._build_compare_panel(self.queue[self.idx])

    def _build_compare_audit_initial(self, item: dict):
        """Audit mode initial panel: 7 single-file folders nearest to this folder's midpoint."""
        for w in self.frm_compare.winfo_children():
            w.destroy()
        self._compare_refs.clear()

        hdr = tk.Frame(self.frm_compare, bg="#161616")
        hdr.pack(fill="x", pady=(8, 4), padx=6)
        tk.Label(
            hdr, text="Nearby single-file folders",
            bg="#161616", fg="#4a9eff",
            font=tkfont.Font(size=9, weight="bold"),
        ).pack(side="left")

        if not self._folder_id_map:
            tk.Label(self.frm_compare, text="No folder data.",
                     bg="#161616", fg="#444", font=tkfont.Font(size=9)).pack(
                padx=8, pady=4, anchor="w")
            return

        file_count = Counter(self._folder_id_map.values())
        current_folder = item.get("folder")
        current_name = current_folder.name if current_folder else None

        # single-file folder name → its one msg_id (exclude the folder being audited)
        single_file_ids: dict = {}
        for fid, name in self._folder_id_map.items():
            if file_count[name] == 1 and name != current_name:
                single_file_ids[name] = fid

        if not single_file_ids:
            tk.Label(self.frm_compare, text="No single-file folders.",
                     bg="#161616", fg="#444", font=tkfont.Font(size=9)).pack(
                padx=8, pady=4, anchor="w")
            return

        lo = item.get("msg_id_lo", 0)
        hi = item.get("msg_id_hi", 0)
        midpoint = (lo + hi) // 2
        if lo == 0 and hi == 0:
            nearby = sorted(single_file_ids, key=lambda n: int(n) if n.isdigit() else 9999)[:7]
        else:
            nearby = sorted(single_file_ids, key=lambda n: abs(single_file_ids[n] - midpoint))[:7]

        CARD_THUMB = (110, 82)
        for name in nearby:
            folder_path = Path(OUTPUT_DIR) / name
            imgs = sorted(
                [f for f in folder_path.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
                key=msg_id,
            ) if folder_path.exists() else []

            card_bg = "#222"
            card = tk.Frame(self.frm_compare, bg=card_bg, cursor="hand2", padx=4, pady=4)
            card.pack(fill="x", padx=6, pady=2)

            ref = None
            for candidate in reversed(imgs):
                ref = _get_thumb(candidate, size=CARD_THUMB)
                if ref:
                    break
            self._compare_refs.append(ref)

            thumb_lbl = tk.Label(
                card, image=ref if ref else None,
                text="?" if not ref else "",
                width=CARD_THUMB[0], height=CARD_THUMB[1], bg=card_bg,
            )
            if ref:
                thumb_lbl.image = ref
            thumb_lbl.pack(side="left", padx=(0, 6))

            info = tk.Frame(card, bg=card_bg)
            info.pack(side="left", fill="both", expand=True)
            dist = abs(single_file_ids[name] - midpoint)
            tk.Label(info, text=name, bg=card_bg, fg="white",
                     font=tkfont.Font(weight="bold", size=11), anchor="w").pack(fill="x")
            tk.Label(info, text=f"msg dist {dist}", bg=card_bg, fg="#666",
                     font=tkfont.Font(size=9), anchor="w").pack(fill="x")

            def _enter(e, c=card):
                for w in _all_widgets(c):
                    try: w.config(bg="#2e2e2e")
                    except Exception: pass

            def _leave(e, c=card, bg=card_bg):
                for w in _all_widgets(c):
                    try: w.config(bg=bg)
                    except Exception: pass

            def _click(e, n=name):
                self.move_var.set(n)
                self.move_entry.focus_set()

            for w in _all_widgets(card):
                w.bind("<Button-1>", _click)
                w.bind("<Enter>",    _enter)
                w.bind("<Leave>",    _leave)

    def _build_compare_folder_preview(self, folder_name: str):
        """Replace the compare panel with a detailed view of one specific folder."""
        for w in self.frm_compare.winfo_children():
            w.destroy()
        self._compare_refs.clear()

        folder_path = Path(OUTPUT_DIR) / folder_name
        imgs = sorted(
            [f for f in folder_path.iterdir()
             if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
            key=msg_id,
        ) if folder_path.exists() else []

        # Header
        exists_color = "white" if imgs else "#4ade80"
        exists_text  = f"{len(imgs)} file{'s' if len(imgs) != 1 else ''}" if imgs else "new folder — will be created"
        tk.Label(
            self.frm_compare,
            text=f"Previewing folder {folder_name}",
            bg="#161616", fg="#4ade80",
            font=tkfont.Font(size=10, weight="bold"),
        ).pack(pady=(8, 0), padx=6, anchor="w")
        tk.Label(
            self.frm_compare, text=exists_text,
            bg="#161616", fg=exists_color,
            font=tkfont.Font(size=9),
        ).pack(padx=6, anchor="w")

        if not imgs:
            tk.Label(
                self.frm_compare, text="Will be created on Move/Confirm",
                bg="#161616", fg="#4ade80", font=tkfont.Font(size=9),
            ).pack(padx=8, pady=8, anchor="w")
            return

        # Large main thumbnail — index photo (highest-msg_id JPG, else last file)
        idx_photo = index_candidate(imgs) or imgs[-1]
        MAIN_PREV = (COMPARE_W - 20, 160)
        ref_main = _get_thumb(idx_photo, size=MAIN_PREV)
        self._compare_refs.append(ref_main)
        lbl_main = tk.Label(
            self.frm_compare,
            image=ref_main if ref_main else None,
            text="(loading)" if not ref_main else "",
            width=MAIN_PREV[0], height=MAIN_PREV[1],
            bg="#161616", fg="#888",
        )
        if ref_main:
            lbl_main.image = ref_main
        lbl_main.pack(padx=6, pady=(6, 2))
        tk.Label(
            self.frm_compare,
            text=f"[INDEX]  {idx_photo.name}",
            bg="#161616", fg="#4ade80",
            font=tkfont.Font(size=8),
        ).pack(padx=6, anchor="w")

        # Remaining files as a thumb strip
        batch = [f for f in imgs if f != idx_photo]
        if batch:
            strip = tk.Frame(self.frm_compare, bg="#161616")
            strip.pack(fill="x", padx=6, pady=(6, 4))
            STRIP_THUMB = (56, 42)
            for f in batch[:5]:
                ref = _get_thumb(f, size=STRIP_THUMB)
                self._compare_refs.append(ref)
                lbl = tk.Label(
                    strip,
                    image=ref if ref else None,
                    text="?" if not ref else "",
                    width=STRIP_THUMB[0], height=STRIP_THUMB[1],
                    bg="#222",
                )
                if ref:
                    lbl.image = ref
                lbl.pack(side="left", padx=2)
            if len(batch) > 5:
                tk.Label(
                    strip, text=f"+{len(batch)-5}",
                    bg="#161616", fg="#666",
                    font=tkfont.Font(size=9),
                ).pack(side="left", padx=4)

    def _open_folder_browser(self):
        """Popup: scrollable grid of ALL numbered folders' index photos.
        Click a cell → fills 'Move selected to:' and closes the window."""
        win = tk.Toplevel(self.root)
        win.title("Browse all folders — click to select")
        win.configure(bg="#1a1a1a")
        win.geometry("620x720")
        win.resizable(True, True)

        # ── Filter bar ────────────────────────────────────────────────
        top = tk.Frame(win, bg="#2a2a2a", pady=6)
        top.pack(fill="x")
        tk.Label(top, text="Filter:", bg="#2a2a2a", fg="#ccc",
                 font=tkfont.Font(size=10)).pack(side="left", padx=(10, 4))
        filter_var = tk.StringVar()
        filter_entry = tk.Entry(top, textvariable=filter_var,
                                width=12, bg="#333", fg="white",
                                insertbackground="white",
                                font=tkfont.Font(family="Consolas", size=11))
        filter_entry.pack(side="left", padx=(0, 8))
        tk.Label(top, text="type number prefix to narrow",
                 bg="#2a2a2a", fg="#666", font=tkfont.Font(size=9)).pack(side="left")
        tk.Button(top, text="✕ Close", bg="#444", fg="white", relief="flat",
                  command=win.destroy).pack(side="right", padx=10)

        # ── Scrollable canvas ─────────────────────────────────────────
        canvas_frame = tk.Frame(win, bg="#1a1a1a")
        canvas_frame.pack(fill="both", expand=True)

        canvas = tk.Canvas(canvas_frame, bg="#1a1a1a", highlightthickness=0)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg="#1a1a1a")
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_resize(e):
            canvas.itemconfig(inner_id, width=e.width)
        canvas.bind("<Configure>", _on_resize)

        def _on_inner_resize(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_inner_resize)

        # Mousewheel scroll
        def _on_wheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        win.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>"))

        # Escape closes
        win.bind("<Escape>", lambda e: win.destroy())

        # ── Build folder list ─────────────────────────────────────────
        out = Path(OUTPUT_DIR)
        all_folders = sorted(
            [d for d in out.iterdir()
             if d.is_dir() and is_numeric(d.name) and not d.name.startswith("_")],
            key=lambda d: folder_sort_key(d.name),
        )

        COLS       = 3
        CELL_W     = 160
        CELL_H     = 140
        THUMB_SIZE = (148, 108)

        cells: list = []

        def _pick(name: str):
            self.move_var.set(name)
            self.move_entry.focus_set()
            win.destroy()

        # ── Lazy thumbnail loading ─────────────────────────────────────
        _lazy_job: list = []   # holds the after() id so we can cancel

        def _load_thumbs_lazy(cell_list, start=0, batch=15):
            if _lazy_job:
                try: win.after_cancel(_lazy_job[0])
                except Exception: pass
                _lazy_job.clear()
            if start >= len(cell_list):
                return
            for cell, d in cell_list[start:start + batch]:
                imgs = sorted(
                    [f for f in d.iterdir()
                     if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
                    key=msg_id,
                ) if d.exists() else []
                idx_photo = index_candidate(imgs) or (imgs[-1] if imgs else None)
                if idx_photo:
                    ref = _get_thumb(idx_photo, size=THUMB_SIZE)
                    if ref:
                        # Find the img_lbl inside the cell and update it
                        for w in cell.winfo_children():
                            if isinstance(w, tk.Label) and w.cget("width") == THUMB_SIZE[0]:
                                w.config(image=ref, text="")
                                w.image = ref
                                break
            job = win.after(0, lambda: _load_thumbs_lazy(cell_list, start + batch, batch))
            _lazy_job.clear()
            _lazy_job.append(job)

        def _build_cells_lazy(folders):
            for w in inner.winfo_children():
                w.destroy()
            cells.clear()
            if _lazy_job:
                try: win.after_cancel(_lazy_job[0])
                except Exception: pass
                _lazy_job.clear()

            cell_list = []
            for i, d in enumerate(folders):
                row, col = divmod(i, COLS)
                cell = tk.Frame(inner, bg="#222", padx=2, pady=2,
                                width=CELL_W, height=CELL_H)
                cell.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")
                cell.grid_propagate(False)

                # Placeholder shown while thumbnail loads
                img_lbl = tk.Label(
                    cell, text="…",
                    width=THUMB_SIZE[0], height=THUMB_SIZE[1],
                    bg="#2a2a2a", fg="#555",
                )
                img_lbl.pack()
                num_lbl = tk.Label(cell, text=d.name, bg="#222", fg="white",
                                   font=tkfont.Font(weight="bold", size=11))
                num_lbl.pack()

                def _enter(e, c=cell):
                    for w in _all_widgets(c):
                        try: w.config(bg="#3a3a3a")
                        except: pass
                def _leave(e, c=cell):
                    for w in _all_widgets(c):
                        try: w.config(bg="#222")
                        except: pass
                def _click(e, n=d.name):
                    _pick(n)
                for w in _all_widgets(cell) + [cell]:
                    w.bind("<Button-1>", _click)
                    w.bind("<Enter>",    _enter)
                    w.bind("<Leave>",    _leave)

                cells.append(cell)
                cell_list.append((cell, d))

            for c in range(COLS):
                inner.columnconfigure(c, weight=1)

            # Start loading thumbnails after layout is painted
            win.after(50, lambda: _load_thumbs_lazy(cell_list))

        # ── Filter logic ──────────────────────────────────────────────
        _filter_after: list = []

        def _refresh(*_):
            if _filter_after:
                try: win.after_cancel(_filter_after[0])
                except Exception: pass
                _filter_after.clear()
            def _do():
                prefix = filter_var.get().strip()
                visible = [d for d in all_folders
                           if not prefix or d.name.startswith(prefix)]
                _build_cells_lazy(visible)
            _filter_after.append(win.after(150, _do))

        filter_var.trace_add("write", _refresh)
        _build_cells_lazy(all_folders)

        filter_entry.focus_set()

    def _build_compare_panel(self, item: dict):
        for w in self.frm_compare.winfo_children():
            w.destroy()
        self._compare_refs.clear()

        if self.mode.get() == "audit":
            self._build_compare_audit_initial(item)
            return
        if self.mode.get() != "unsorted":
            return

        # Header row with "Browse all" button
        hdr = tk.Frame(self.frm_compare, bg="#161616")
        hdr.pack(fill="x", pady=(8, 4), padx=6)
        tk.Label(
            hdr, text="Compare nearby folders",
            bg="#161616", fg="#888", font=tkfont.Font(size=9, weight="bold"),
        ).pack(side="left")
        tk.Button(
            hdr, text="Browse all  Ctrl+B",
            bg="#2a2a2a", fg="#aaa",
            font=tkfont.Font(size=8),
            relief="flat", cursor="hand2",
            command=self._open_folder_browser,
        ).pack(side="right")

        # Collect nearby folder names; put the suggested match first
        ids = [item["msg_id_lo"], item["msg_id_hi"]]
        lo, hi = min(ids), max(ids)
        nearby = sorted(
            {nm for fid, nm in self._folder_id_map.items()
             if lo - NEARBY_RADIUS <= fid <= hi + NEARBY_RADIUS},
            key=folder_sort_key,
        )

        sugg = item.get("suggested_name")
        ordered = []
        if sugg and sugg in nearby:
            ordered.append(sugg)
        ordered += [n for n in nearby if n != sugg]
        ordered = ordered[:8]

        if not ordered:
            tk.Label(self.frm_compare, text="No nearby folders.",
                     bg="#161616", fg="#444", font=tkfont.Font(size=9),
                     justify="left").pack(padx=8, pady=4, anchor="w")
            return

        CARD_THUMB = (110, 82)
        for name in ordered:
            folder_path = Path(OUTPUT_DIR) / name
            imgs = sorted(
                [f for f in folder_path.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
                key=msg_id,
            ) if folder_path.exists() else []

            is_match = name == sugg
            card_bg = "#1a3a1a" if is_match else "#222"

            card = tk.Frame(self.frm_compare, bg=card_bg, cursor="hand2", padx=4, pady=4)
            card.pack(fill="x", padx=6, pady=(4 if is_match else 2))

            # Show the index photo (last JPG) as the thumbnail
            ref = None
            for candidate in reversed(imgs):
                ref = _get_thumb(candidate, size=CARD_THUMB)
                if ref:
                    break
            self._compare_refs.append(ref)

            thumb_lbl = tk.Label(
                card, image=ref if ref else None,
                text="?" if not ref else "",
                width=CARD_THUMB[0], height=CARD_THUMB[1], bg=card_bg,
            )
            if ref:
                thumb_lbl.image = ref
            thumb_lbl.pack(side="left", padx=(0, 6))

            info = tk.Frame(card, bg=card_bg)
            info.pack(side="left", fill="both", expand=True)

            name_color = "#4ade80" if is_match else "white"
            tk.Label(info, text=name + (" ← MATCH" if is_match else ""),
                     bg=card_bg, fg=name_color,
                     font=tkfont.Font(weight="bold", size=11), anchor="w").pack(fill="x")
            tk.Label(info, text=f"{len(imgs)} file{'s' if len(imgs) != 1 else ''}",
                     bg=card_bg, fg="#666", font=tkfont.Font(size=9), anchor="w").pack(fill="x")

            def _enter(e, c=card, bg=card_bg):
                lighter = "#2e5a2e" if is_match else "#2e2e2e"
                for w in _all_widgets(c):
                    try: w.config(bg=lighter)
                    except: pass

            def _leave(e, c=card, bg=card_bg):
                for w in _all_widgets(c):
                    try: w.config(bg=bg)
                    except: pass

            def _click(e, n=name):
                self.entry_var.set(n)
                self.lbl_err.config(text=f"→ {n}")
                self.entry.focus_set()

            for w in _all_widgets(card):
                w.bind("<Button-1>", _click)
                w.bind("<Enter>",    _enter)
                w.bind("<Leave>",    _leave)

    # ------------------------------------------------------------------
    # Image display
    # ------------------------------------------------------------------

    def _show_image(self, idx: int):
        if not self.images:
            return
        idx = max(0, min(idx, len(self.images) - 1))
        self.img_idx = idx
        path = self.images[idx]
        is_idx = (path == self.queue[self.idx].get("index_photo"))

        self.lbl_img_counter.config(
            text=f"{'[INDEX] ' if is_idx else ''}image {idx+1}/{len(self.images)}   ← → browse"
        )
        self._refresh_thumb_borders()

        self.lbl_image.config(image="", text="Loading…", fg="#666", font=tkfont.Font(size=12))
        self._main_ref = None
        self._pending_path = path
        self.root.after(1, lambda p=path: self._load_main(p))

        # Per-photo Vision hint (batch_ocr.json)
        if self.batch_ocr and self.mode.get() == "unsorted":
            entry = self.batch_ocr.get(path.name, {})
            nums  = entry.get("nums", [])
            raw   = entry.get("raw", "")
            if nums:
                photo_hint = f"Vision this photo: {nums}  —  {raw[:90]}"
            elif raw and not raw.startswith("ERROR"):
                photo_hint = f"Vision this photo (no number): {raw[:90]}"
            else:
                photo_hint = self._item_hint
            self.lbl_hint.config(text=photo_hint)

    def _load_main(self, path: Path):
        if self._pending_path is not path:
            return
        try:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            img.thumbnail((MAIN_MAX_W, MAIN_MAX_H), Image.BILINEAR)
            self._main_ref = ImageTk.PhotoImage(img)
            self.lbl_image.config(image=self._main_ref, text="")
        except Exception as e:
            self._main_ref = None
            self.lbl_image.config(image="", text=f"[cannot open: {e}]",
                                   fg="#f66", font=tkfont.Font(size=11))

    # ------------------------------------------------------------------
    # Thumbnail strip
    # ------------------------------------------------------------------

    def _build_thumbs(self):
        for w in self.frm_thumbs.winfo_children():
            w.destroy()
        item = self.queue[self.idx] if self.queue else {}
        idx_photo = item.get("index_photo")
        images_to_show = self.images
        if self.mode.get() == "audit" and len(self.images) > 12:
            images_to_show = self.images[:12]
        for i, path in enumerate(images_to_show):
            ref = _get_thumb(path, THUMB_SIZE)
            is_index = path == idx_photo
            btn = tk.Label(
                self.frm_thumbs,
                image=ref if ref else None,
                text="IDX" if (not ref and is_index) else ("?" if not ref else ""),
                bg="#1a3a1a" if is_index else "#2a2a2a",
                relief="solid", bd=2,
                highlightthickness=2, highlightbackground="#4ade80" if is_index else "#1a1a1a",
                cursor="hand2", takefocus=0,
            )
            if ref:
                btn.image = ref
            btn.pack(side="left", padx=3, pady=3)
            btn.bind("<Button-1>", lambda e, i=i, p=path: self._thumb_click(i, p))
        if self.mode.get() == "audit" and len(self.images) > 12:
            tk.Label(
                self.frm_thumbs,
                text=f"+{len(self.images) - 12} more\n← → to browse",
                bg="#2a2a2a", fg="#888", font=tkfont.Font(size=8),
            ).pack(side="left", padx=6)

    def _refresh_thumb_borders(self):
        item = self.queue[self.idx] if self.queue else {}
        idx_photo = item.get("index_photo")
        for i, w in enumerate(self.frm_thumbs.winfo_children()):
            if i >= len(self.images):
                break
            path = self.images[i]
            is_index = path == idx_photo
            if path in self.selected:
                color = "#e05252"
            elif i == self.img_idx:
                color = "#4a9eff"
            elif is_index:
                color = "#4ade80"
            else:
                color = "#1a1a1a"
            w.config(highlightbackground=color)

    def _thumb_click(self, idx: int, path: Path):
        if path in self.selected:
            self.selected.discard(path)
        else:
            self.selected.add(path)
        self._show_image(idx)
        self.lbl_sel.config(text=f"{len(self.selected)} selected" if self.selected else "")

        # Refresh compare panel based on mode and selection state
        if not self.move_var.get().strip():
            item = self.queue[self.idx] if self.queue else {}
            if self.mode.get() == "audit":
                if path in self.selected:
                    self._compare_for_photo(path)
                else:
                    self._build_compare_audit_initial(item)
            else:
                idx_photo = item.get("index_photo")
                if path in self.selected and path != idx_photo:
                    self._compare_for_photo(path)
                elif not self.selected or path == idx_photo:
                    self._build_compare_panel(item)

    def _compare_for_photo(self, path: Path):
        """Rebuild compare panel showing folders nearest to this photo's msg-ID."""
        fid = msg_id(path)
        if not fid:
            if self.queue:
                self._build_compare_panel(self.queue[self.idx])
            return

        # One O(n) pass: compute min distance to fid for each folder name
        min_dist: dict = {}
        for file_id, nm in self._folder_id_map.items():
            d = abs(file_id - fid)
            if d <= NEARBY_RADIUS:
                if nm not in min_dist or d < min_dist[nm]:
                    min_dist[nm] = d
        nearby = sorted(min_dist, key=lambda nm: (min_dist[nm], folder_sort_key(nm)))

        for w in self.frm_compare.winfo_children():
            w.destroy()
        self._compare_refs.clear()

        hdr = tk.Frame(self.frm_compare, bg="#161616")
        hdr.pack(fill="x", pady=(8, 4), padx=6)
        tk.Label(
            hdr, text=f"Nearest to msg {fid}",
            bg="#161616", fg="#4a9eff",
            font=tkfont.Font(size=9, weight="bold"),
        ).pack(side="left")
        tk.Button(
            hdr, text="Browse all  Ctrl+B",
            bg="#2a2a2a", fg="#aaa",
            font=tkfont.Font(size=8),
            relief="flat", cursor="hand2",
            command=self._open_folder_browser,
        ).pack(side="right")

        if not nearby:
            tk.Label(self.frm_compare, text="No folders within radius.",
                     bg="#161616", fg="#444",
                     font=tkfont.Font(size=9)).pack(padx=8, pady=4, anchor="w")
            return

        CARD_THUMB = (110, 82)
        sugg = self.queue[self.idx].get("suggested_name") if self.queue else None
        for name in nearby[:8]:
            folder_path = Path(OUTPUT_DIR) / name
            imgs = sorted(
                [f for f in folder_path.iterdir()
                 if f.is_file() and f.suffix.lower() in IMAGE_EXTS],
                key=msg_id,
            ) if folder_path.exists() else []

            is_match = name == sugg
            card_bg = "#1a3a1a" if is_match else "#222"
            card = tk.Frame(self.frm_compare, bg=card_bg, cursor="hand2", padx=4, pady=4)
            card.pack(fill="x", padx=6, pady=2)

            ref = None
            for candidate in reversed(imgs):
                ref = _get_thumb(candidate, size=CARD_THUMB)
                if ref:
                    break
            self._compare_refs.append(ref)

            thumb_lbl = tk.Label(
                card, image=ref if ref else None,
                text="?" if not ref else "",
                width=CARD_THUMB[0], height=CARD_THUMB[1], bg=card_bg,
            )
            if ref:
                thumb_lbl.image = ref
            thumb_lbl.pack(side="left", padx=(0, 6))

            info = tk.Frame(card, bg=card_bg)
            info.pack(side="left", fill="both", expand=True)
            tk.Label(info, text=name, bg=card_bg, fg="white",
                     font=tkfont.Font(weight="bold", size=11), anchor="w").pack(fill="x")
            tk.Label(info, text=f"{len(imgs)} file{'s' if len(imgs)!=1 else ''}",
                     bg=card_bg, fg="#666",
                     font=tkfont.Font(size=9), anchor="w").pack(fill="x")

            def _click(e, n=name):
                self.move_var.set(n)
                self.move_entry.focus_set()
            for w in _all_widgets(card):
                w.bind("<Button-1>", _click)

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _entry_left(self, event):
        if event.widget.index("insert") == 0:
            self._show_image(self.img_idx - 1)
            return "break"

    def _entry_right(self, event):
        if event.widget.index("insert") >= len(event.widget.get()):
            self._show_image(self.img_idx + 1)
            return "break"

    def _on_key(self, event):
        if isinstance(self.root.focus_get(), tk.Entry):
            return
        if event.char == "f" and self.mode.get() == "audit":
            self.flag_folder()
            return
        if event.char and event.char.isprintable() and not (event.state & 0x4):
            self.entry.focus_set()
            self.entry.delete(0, "end")
            self.entry.insert(0, event.char)

    def navigate_folder(self, delta: int):
        new_idx = max(0, min(self.idx + delta, len(self.queue) - 1))
        self._load_item(new_idx)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def confirm(self):
        if not self.queue:
            return
        item     = self.queue[self.idx]
        new_name = self.entry_var.get().strip()
        if not new_name:
            self.lbl_err.config(text="Enter a folder number first")
            return

        mode   = self.mode.get()
        folder = item.get("folder")
        ts     = datetime.now().isoformat()
        uid    = item.get("unit_id", "")

        # ── Phase C audit: read-only confirm ─────────────────────────
        if mode == "audit":
            append_log([{
                "mode": "audit", "unit_id": uid,
                "folder_name": folder.name if folder else new_name,
                "filename": "", "action": "confirmed",
                "new_name": "", "timestamp": ts,
            }])
            self.lbl_err.config(text="")
            self._advance()
            return

        # ── Pre-move validation ──────────────────────────────────────
        if new_name.isdigit():
            n = int(new_name)
            lo, hi = NUM_RANGE
            if not (lo <= n <= hi):
                if not self._confirmed_warn(
                    f"{new_name} is outside 1–{hi}. Assign anyway?"
                ):
                    return

        try:
            target = safe_folder(Path(OUTPUT_DIR), new_name)
        except ValueError as exc:
            self.lbl_err.config(text=str(exc))
            return

        # Spread-merge guard: merging two disjoint id-ranges into one folder
        if target.exists() and target.is_dir():
            existing_ids = sorted(
                msg_id(f) for f in target.iterdir()
                if f.is_file() and msg_id(f)
            )
            incoming_ids = sorted(msg_id(f) for f in item["member_files"] if msg_id(f))
            if existing_ids and incoming_ids:
                combined_lo = min(existing_ids[0], incoming_ids[0])
                combined_hi = max(existing_ids[-1], incoming_ids[-1])
                if combined_hi - combined_lo > SPREAD_WARN:
                    if not self._confirmed_warn(
                        f"Folder {new_name} already holds msg {existing_ids[0]}–{existing_ids[-1]}.\n"
                        f"You're adding msg {incoming_ids[0]}–{incoming_ids[-1]}.\n"
                        f"Spread > {SPREAD_WARN} — possible two buildings. Merge anyway?"
                    ):
                        return

        try:
            if mode == "folders":
                # Phase B: rename/merge the folder
                if not folder or not folder.exists():
                    self._advance()
                    return
                action = (
                    "confirmed" if new_name == folder.name
                    else do_rename_or_merge(folder, new_name)
                )
                append_log([{
                    "mode": "folders", "unit_id": uid,
                    "folder_name": folder.name, "filename": "",
                    "action": action, "new_name": new_name, "timestamp": ts,
                }])
            else:
                # Phase A: move all member_files to target
                target.mkdir(parents=True, exist_ok=True)
                files = list(item["member_files"])
                moved = move_batch(files, target, Path(OUTPUT_DIR))
                rows = [{
                    "mode": "unsorted", "unit_id": uid,
                    "folder_name": src.parent.name, "filename": dst.name,
                    "action": "moved_to", "new_name": new_name, "timestamp": ts,
                    "source_path": str(src), "destination_path": str(dst),
                    "original_filename": src.name,
                } for src, dst in moved]
                append_log(rows)
                # Clean up any now-empty source dirs (named folders get deleted this way)
                cleanup_empty_sources(item["member_files"], target)

        except Exception as e:
            self.lbl_err.config(text=f"Error: {e}")
            return

        self.lbl_err.config(text="")
        self._advance()

    def _confirmed_warn(self, message: str) -> bool:
        """Show a warning dialog, return True if user clicks OK."""
        import tkinter.messagebox as mb
        return mb.askokcancel("Warning", message, parent=self.root)

    def flag_folder(self):
        """Phase C only: flag current folder with a note and advance."""
        if self.mode.get() != "audit" or not self.queue:
            return
        import tkinter.simpledialog as sd
        note = sd.askstring("Flag folder", "Note (optional):", parent=self.root) or ""
        item   = self.queue[self.idx]
        folder = item.get("folder")
        ts     = datetime.now().isoformat()
        append_log([{
            "mode": "audit", "unit_id": item.get("unit_id", ""),
            "folder_name": folder.name if folder else "",
            "filename": "", "action": "flagged",
            "new_name": note, "timestamp": ts,
        }])
        self.lbl_err.config(text=f"Flagged: {note}" if note else "Flagged")
        self._advance()

    def _jump_to_folder(self):
        """Phase C only: jump to a folder by number."""
        if self.mode.get() != "audit" or not self.queue:
            return
        import tkinter.simpledialog as sd
        num = sd.askstring("Jump to folder", "Folder number:", parent=self.root)
        if not num:
            return
        num = num.strip()
        target_uid = f"A:{num}"
        for i, u in enumerate(self.queue):
            if u.get("unit_id") == target_uid:
                self._load_item(i)
                return
        self.lbl_err.config(text=f"Folder {num} not in audit queue (already confirmed or doesn't exist)")

    def skip(self):
        if not self.queue:
            return
        item   = self.queue[self.idx]
        folder = item.get("folder")
        uid    = item.get("unit_id", "")
        ts     = datetime.now().isoformat()
        try:
            if folder:
                append_log([{
                    "mode": self.mode.get(), "unit_id": uid,
                    "folder_name": folder.name, "filename": "",
                    "action": "skipped", "new_name": "", "timestamp": ts,
                }])
            else:
                append_log([{
                    "mode": "unsorted", "unit_id": uid,
                    "folder_name": f.name, "filename": f.name,
                    "action": "skipped", "new_name": "", "timestamp": ts,
                } for f in item.get("member_files", [])])
        except Exception as e:
            self.lbl_err.config(text=f"Log error: {e}")
        self._advance()

    def move_selected(self):
        if not self.selected:
            self.lbl_err.config(text="No images selected — click thumbnails first")
            return
        dest_name = self.move_var.get().strip()
        if not dest_name:
            self.lbl_err.config(text="Enter destination in 'Move to' box")
            return

        item        = self.queue[self.idx]
        folder_name = item["folder"].name if item.get("folder") else "_unsorted"
        uid         = item.get("unit_id", "")
        ts          = datetime.now().isoformat()

        try:
            dest = safe_folder(Path(OUTPUT_DIR), dest_name)
            dest.mkdir(parents=True, exist_ok=True)
            moved_files = move_batch(list(self.selected), dest, Path(OUTPUT_DIR))
            append_log([{
                "mode": self.mode.get(), "unit_id": uid,
                "folder_name": folder_name, "filename": dst.name,
                "action": "moved_to", "new_name": dest_name, "timestamp": ts,
                "source_path": str(src), "destination_path": str(dst),
                "original_filename": src.name,
            } for src, dst in moved_files])
        except Exception as e:
            self.lbl_err.config(text=f"Error: {e}")
            return

        moved = set(self.selected)
        self.selected.clear()
        self.images = [p for p in self.images if p not in moved]
        self.queue[self.idx]["images"] = self.images
        self.queue[self.idx]["member_files"] = [f for f in item["member_files"] if f not in moved]
        self.img_idx = max(0, min(self.img_idx, len(self.images) - 1))
        self.lbl_sel.config(text=f"Moved {len(moved)} → {dest_name}/")
        self.lbl_err.config(text="")

        if not self.images:
            self._advance()
        else:
            self._build_thumbs()
            self._show_image(self.img_idx)

    def select_all(self):
        if self.selected == set(self.images):
            self.selected.clear()
        else:
            self.selected = set(self.images)
        self.lbl_sel.config(text=f"{len(self.selected)} selected" if self.selected else "")
        self._refresh_thumb_borders()

    def _advance(self):
        if self.idx < len(self.queue):
            self.queue.pop(self.idx)
        if not self.queue:
            self._show_empty()
            return
        self._load_item(min(self.idx, len(self.queue) - 1))


# Report mode

def run_report() -> None:
    from verify_completeness import inspect_local, write_manifest
    result = inspect_local(Path(OUTPUT_DIR))
    path = write_manifest(Path(OUTPUT_DIR), result)
    print(f"Folders: {len(result['folders'])}; images: {result['image_count']}; unsorted: {len(result['unsorted'])}")
    print(f"Manifest: {path}")


# Entry point

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Review and organise local photos. File moves require a GUI action.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--report", action="store_true", help="Write a local manifest without opening a window")
    args = parser.parse_args(argv)
    data = args.data_dir.expanduser().resolve()
    if not data.is_dir():
        parser.error("Data directory does not exist. Run python make_demo.py or supply --data-dir.")
    configure_paths(data)
    if args.report:
        run_report()
        return 0
    Path(UNSORTED_DIR).mkdir(exist_ok=True)
    root = tk.Tk()
    root.title("Zhastar Media Toolkit — Photo Review")
    root.configure(bg="#1e1e1e")
    VisualReviewApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
