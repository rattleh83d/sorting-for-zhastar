"""Desktop-only smoke check using a withdrawn window and disposable demo files."""
import sys
from pathlib import Path
import tempfile
import time
import tkinter as tk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from make_demo import create_demo
import visual_review as review
import index_browser as browser
from verify_completeness import inspect_local
from shared import enable_heif
from PIL import Image


def main():
    errors = []
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
        data = create_demo(Path(directory) / "sorted")
        if enable_heif():
            source = data / "_unsorted/batch_001/msg_200.png"
            with Image.open(source) as image:
                image.save(source.with_suffix(".heic"), format="HEIF")
            source.unlink()
        review.configure_paths(data)
        root = tk.Tk()
        root.withdraw()
        root.report_callback_exception = lambda *args: errors.append(str(args[1]))
        try:
            app = review.VisualReviewApp(root)
            root.update()
            assert app.queue[0]["suggested_name"] == "2"
            app.entry_var.set("2")
            app.confirm()
            root.update()
            app.entry_var.set("3")
            app.confirm()
            root.update()
            app.entry_var.set("4")
            app.confirm()
            root.update()
            assert not inspect_local(data)["unsorted"]
            app.mode.set("folders")
            app._reload_queue()
            root.update()
            if app.queue:
                app.confirm()
            app.mode.set("audit")
            app._reload_queue()
            root.update()
            while app.queue:
                app.confirm()
                root.update()
            assert inspect_local(data)["image_count"] == 8
        finally:
            for job in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(job)
            root.destroy()
        browser.OUTPUT_DIR = data
        gallery = browser.IndexBrowser()
        gallery.withdraw()
        gallery.report_callback_exception = lambda *args: errors.append(str(args[1]))
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                gallery.update()
                if gallery._total and len(gallery._pil) == gallery._total:
                    break
                time.sleep(0.02)
            assert gallery._total == 4
            assert len(gallery._pil) == 4
        finally:
            for job in gallery.tk.splitlist(gallery.tk.call("after", "info")):
                gallery.after_cancel(job)
            gallery.destroy()
    if errors:
        raise AssertionError(errors)
    print("GUI smoke passed: assign demo batches, folder pass, audit pass, gallery loading. No network used.")


if __name__ == "__main__":
    main()
