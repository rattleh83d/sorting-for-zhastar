"""
index_browser.py — scrollable gallery of index photos for cross-reference.

Shows the index photo (JPG preferred, else first image by msg-ID) from every
numbered folder in sorted/. Click any photo → folder number shown at top and
copied to clipboard so you can paste it straight into the sorter.

Launch: python index_browser.py
"""

import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageOps, ImageTk

from shared import DEFAULT_DATA_DIR, SKIP_DIRS, enable_heif, image_paths
enable_heif()
OUTPUT_DIR = DEFAULT_DATA_DIR
COLS       = 3
PAD        = 8
PIL_MAX    = 700   # PIL cache size — large enough for any window width; avoids re-reading disk


# ── helpers ────────────────────────────────────────────────────────────────────

def _msg_id(path: Path) -> int:
    m = re.match(r"msg_(\d+)", path.name, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _index_photo(folder: Path):
    images = sorted(image_paths(folder), key=_msg_id)
    jpgs = [p for p in images if p.suffix.lower() in {".jpg", ".jpeg"}]
    return max(jpgs, key=_msg_id) if jpgs else (images[0] if images else None)


def _collect() -> list:
    """Return [(folder_name, index_photo_path), ...] sorted numerically."""
    base = Path(OUTPUT_DIR)
    if not base.exists():
        print(f"ERROR: {OUTPUT_DIR} not found — run from the project root.", file=sys.stderr)
        sys.exit(1)
    out = []
    for d in sorted(
        base.iterdir(),
        key=lambda p: (0, int(p.name)) if p.name.isdigit() else (1,),
    ):
        if not d.is_dir() or d.name in SKIP_DIRS or not d.name.isdigit():
            continue
        ip = _index_photo(d)
        if ip:
            out.append((d.name, ip))
    return out


# ── app ────────────────────────────────────────────────────────────────────────

class IndexBrowser(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Index Browser")
        self.configure(bg="#1a1a1a")
        self.geometry("920x720")
        self.minsize(400, 300)

        # ── top bar ──────────────────────────────────────────────────────────
        bar = tk.Frame(self, bg="#222222", pady=10)
        bar.pack(fill="x")

        self.lbl_result = tk.Label(
            bar,
            text="Click any photo — folder number will appear here",
            font=("Segoe UI", 17, "bold"),
            bg="#222222", fg="#f0f0f0",
            anchor="w",
        )
        self.lbl_result.pack(side="left", padx=16, fill="x", expand=True)

        self.lbl_ok = tk.Label(
            bar, text="", font=("Segoe UI", 12), bg="#222222", fg="#66bb6a",
        )
        self.lbl_ok.pack(side="left")

        self.lbl_prog = tk.Label(
            bar, text="Loading…", font=("Segoe UI", 10), bg="#222222", fg="#666666",
        )
        self.lbl_prog.pack(side="right", padx=16)

        # ── canvas + scrollbar ───────────────────────────────────────────────
        cv_frame = tk.Frame(self, bg="#1a1a1a")
        cv_frame.pack(fill="both", expand=True)

        self._sb = tk.Scrollbar(cv_frame, orient="vertical")
        self._sb.pack(side="right", fill="y")
        self._canvas = tk.Canvas(cv_frame, bg="#1a1a1a", highlightthickness=0,
                                 yscrollcommand=self._sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._sb.config(command=self._canvas.yview)

        self._inner = tk.Frame(self._canvas, bg="#1a1a1a")
        self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", self._on_canvas_resize)
        self.bind_all("<MouseWheel>",
                      lambda e: self._canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ── state ────────────────────────────────────────────────────────────
        self._folders: list       = []   # [(name, path), ...]
        self._pil: dict           = {}   # name -> PIL Image (pre-resized to PIL_MAX)
        self._lbl_imgs: dict      = {}   # name -> tk.Label (image widget)
        self._frm_imgs: dict      = {}   # name -> tk.Frame (fixed-size container)
        self._thumb_w: int        = 260
        self._q: queue.Queue      = queue.Queue()
        self._total: int          = 0
        self._resize_pending      = None

        self.after(80, self._start)

    # ── startup ───────────────────────────────────────────────────────────────

    def _start(self):
        self._folders = _collect()
        self._total   = len(self._folders)
        self._build_grid(self._thumb_w)
        threading.Thread(target=self._bg_load, daemon=True).start()
        self.after(60, self._drain)

    def _bg_load(self):
        """Background thread: decode each image and push PIL Image into queue."""
        for name, path in self._folders:
            try:
                img = Image.open(path)
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                img.thumbnail((PIL_MAX, PIL_MAX), Image.BILINEAR)
            except Exception:
                img = None
            self._q.put((name, img))

    def _drain(self):
        """Main-thread poller: pull loaded images from queue and update labels."""
        try:
            while True:
                name, pil_img = self._q.get_nowait()
                self._pil[name] = pil_img
                self._render(name, self._thumb_w)
        except queue.Empty:
            pass

        done = len(self._pil)
        if done < self._total:
            self.lbl_prog.config(text=f"Loading… {done} / {self._total}")
            self.after(60, self._drain)
        else:
            self.lbl_prog.config(text=f"{self._total} folders")

    # ── grid ──────────────────────────────────────────────────────────────────

    def _build_grid(self, tw: int):
        for i, (name, _) in enumerate(self._folders):
            row, col = divmod(i, COLS)

            cell = tk.Frame(self._inner, bg="#242424", padx=3, pady=3)
            cell.grid(row=row, column=col, padx=PAD, pady=PAD, sticky="nsew")

            # fixed-size container so placeholder and image take the same space
            frm = tk.Frame(cell, bg="#2e2e2e", width=tw, height=tw)
            frm.pack_propagate(False)
            frm.pack()
            self._frm_imgs[name] = frm

            lbl = tk.Label(frm, text="…", bg="#2e2e2e", fg="#555555",
                           font=("Segoe UI", 16))
            lbl.place(relx=0.5, rely=0.5, anchor="center")
            self._lbl_imgs[name] = lbl

            # bind click on every part of the cell
            for widget in (cell, frm, lbl):
                widget.bind("<Button-1>", lambda e, n=name: self._on_click(n))

            num_lbl = tk.Label(cell, text=name,
                               font=("Segoe UI", 10, "bold"),
                               bg="#242424", fg="#c0c0c0")
            num_lbl.pack(pady=(4, 2))
            num_lbl.bind("<Button-1>", lambda e, n=name: self._on_click(n))

        for c in range(COLS):
            self._inner.columnconfigure(c, weight=1)

    def _render(self, name: str, tw: int):
        """Create/update the ImageTk for one cell from the cached PIL image."""
        pil_img = self._pil.get(name)
        lbl     = self._lbl_imgs.get(name)
        frm     = self._frm_imgs.get(name)
        if not lbl or not frm:
            return
        if pil_img is None:
            lbl.config(text="✗", fg="#ef5350", image="")
            return
        resized = pil_img.copy()
        resized.thumbnail((tw, tw), Image.BILINEAR)
        ref = ImageTk.PhotoImage(resized)
        lbl._ref = ref          # keep strong reference; prevents GC
        lbl.config(image=ref, text="")
        lbl.place(relx=0.5, rely=0.5, anchor="center")
        frm.config(width=tw, height=tw)

    # ── resize ────────────────────────────────────────────────────────────────

    def _on_canvas_resize(self, event):
        self._canvas.itemconfig(self._win, width=event.width)
        avail  = event.width - (COLS + 1) * PAD * 2
        new_tw = max(80, avail // COLS)
        if abs(new_tw - self._thumb_w) < 15:
            return
        # debounce: apply 220 ms after the last resize event
        if self._resize_pending:
            self.after_cancel(self._resize_pending)
        self._resize_pending = self.after(220, lambda: self._apply_resize(new_tw))

    def _apply_resize(self, tw: int):
        self._thumb_w = tw
        for name, frm in self._frm_imgs.items():
            frm.config(width=tw, height=tw)
            if name in self._pil:
                self._render(name, tw)

    # ── click ─────────────────────────────────────────────────────────────────

    def _on_click(self, name: str):
        self.lbl_result.config(text=f"→   Folder  {name}")
        try:
            self.clipboard_clear()
            self.clipboard_append(name)
            self.lbl_ok.config(text="Copied!")
            self.after(2500, lambda: self.lbl_ok.config(text=""))
        except Exception:
            pass


def main(argv=None):
    import argparse
    global OUTPUT_DIR
    parser = argparse.ArgumentParser(description="Read-only index photo gallery")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    OUTPUT_DIR = args.data_dir.expanduser().resolve()
    if not OUTPUT_DIR.is_dir():
        parser.error("Data directory not found. Run python make_demo.py first.")
    IndexBrowser().mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
