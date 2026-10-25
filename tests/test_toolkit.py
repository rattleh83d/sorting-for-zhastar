"""Offline regression tests. Never use an account or real event media."""
import argparse
import asyncio
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
import sorting_core as core
import shared
import ocr_batch as ocr
import verify_completeness as verify
import redownload_missing as recover
from make_demo import create_demo


class LocalCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name) / "sorted"
        self.root.mkdir()
        core.configure_paths(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, name, content=b"fixture"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


class MoveTests(LocalCase):
    def test_rejects_path_traversal_and_reserved_names(self):
        for name in ("../outside", "..", "/absolute", "a/b", "a\\b", "CON", "_state", "name.", "name "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                shared.safe_folder(self.root, name)

    def test_collision_preserves_both_files(self):
        source = self.put("old/msg_1.jpg", b"original")
        other = self.put("new/msg_1.jpg", b"existing")
        pairs = shared.move_batch([source], other.parent, self.root)
        self.assertEqual(pairs[0][1].name, "msg_1_dup1.jpg")
        self.assertEqual(other.read_bytes(), b"existing")
        self.assertEqual(pairs[0][1].read_bytes(), b"original")

    def test_rolls_back_partial_batch(self):
        one, two = self.put("old/one.jpg"), self.put("old/two.jpg")
        actual_move = shared.shutil.move
        calls = 0
        def fail_second(src, dst):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk error")
            return actual_move(src, dst)
        with patch.object(shared.shutil, "move", side_effect=fail_second), self.assertRaises(OSError):
            shared.move_batch([one, two], self.root / "new", self.root)
        self.assertTrue(one.exists() and two.exists())
        states = [json.loads(line)["state"] for line in (self.root / "_state/moves.jsonl").read_text().splitlines()]
        self.assertEqual(states[-1], "rolled_back")

    def test_missing_source_fails_before_any_move(self):
        source = self.put("old/one.jpg")
        with self.assertRaises(ValueError):
            shared.move_batch([source, self.root / "missing.jpg"], self.root / "new", self.root)
        self.assertTrue(source.exists())

    def test_same_folder_move_is_noop(self):
        source = self.put("1/msg_1.jpg", b"same")
        pairs = shared.move_batch([source], source.parent, self.root)
        self.assertEqual(pairs[0], (source.resolve(), source.resolve()))
        self.assertEqual(source.read_bytes(), b"same")

    def test_rename_is_not_logged_as_merge(self):
        self.put("old/msg_1.jpg")
        action = shared.rename_or_merge(self.root / "old", "new", self.root)
        self.assertEqual(action, "renamed")
        self.assertTrue((self.root / "new/msg_1.jpg").exists())

    def test_merge_does_not_overwrite(self):
        self.put("1/msg_1.jpg", b"a")
        self.put("2/msg_1.jpg", b"b")
        self.assertEqual(shared.rename_or_merge(self.root / "1", "2", self.root), "merged")
        self.assertEqual((self.root / "2/msg_1.jpg").read_bytes(), b"b")
        self.assertEqual((self.root / "2/msg_1_dup1.jpg").read_bytes(), b"a")


class SortingTests(LocalCase):
    def test_no_unsorted_directory_is_valid(self):
        self.assertEqual(core.build_units_unsorted({}), [])

    def test_explicit_batch_boundaries_are_preserved(self):
        self.put("_unsorted/batch_A/msg_100.png")
        self.put("_unsorted/batch_B/msg_101.png")
        clusters = core._cluster_unsorted(set())
        self.assertEqual([len(c) for c in clusters], [1, 1])

    def test_adjacent_index_is_suggested(self):
        self.put("_unsorted/batch_A/msg_100.png")
        self.put("_unsorted/batch_A/msg_101.png")
        self.put("42/msg_103.jpg")
        units = core.build_units_unsorted({})
        self.assertEqual(units[0]["type"], "A_HIGH_CONF")
        self.assertEqual(units[0]["suggested_name"], "42")

    def test_real_unsorted_files_not_hidden_by_stale_log(self):
        file = self.put("_unsorted/msg_100.png")
        shared.append_csv(self.root / "visual_log.csv", core.LOG_FIELDS, [{"mode": "assign", "action": "moved_to", "filename": file.name, "unit_id": core.make_unit_id([file]), "new_name": "1"}])
        self.assertEqual(len(core.build_units_unsorted({})), 1)

    def test_legacy_review_is_resumed(self):
        shared.append_csv(self.root / "visual_log.csv", core.LOG_FIELDS, [{"mode": "review", "action": "confirmed", "folder_name": "42"}])
        self.assertEqual(core.load_done_folder_names("folders"), {"42"})
        self.assertEqual(core.load_done_folder_names("audit"), set())

    def test_phase_gate_counts_nested_images(self):
        self.put("_unsorted/batch_A/msg_1.png")
        self.assertEqual(core.phase_a_complete()[:2], (False, 1))

    def test_csv_retains_legacy_columns(self):
        log = self.root / "visual_log.csv"
        log.write_text("mode,custom\nreview,keep\n", encoding="utf-8")
        core.append_log([{"mode": "audit", "action": "confirmed"}])
        with log.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["custom"], "keep")
        self.assertEqual(rows[1]["mode"], "audit")


class InventoryTests(LocalCase):
    def test_duplicate_locations_are_retained(self):
        self.put("1/msg_777.jpg")
        self.put("2/msg_777_dup1.jpg")
        state = verify.inspect_local(self.root)
        self.assertEqual(state["image_count"], 2)
        self.assertEqual(len(state["duplicates"]["777"]), 2)

    def test_nested_unsorted_and_empty_files_reported(self):
        self.put("_unsorted/batch_A/msg_10.heif", b"")
        state = verify.inspect_local(self.root)
        self.assertEqual(len(state["unsorted"]), 1)
        self.assertEqual(len(state["invalid"]), 1)

    def test_split_album_includes_unsorted_locations(self):
        self.put("1/msg_10.jpg")
        self.put("_unsorted/batch_A/msg_11.png")
        result = verify.compare_inventory(verify.inspect_local(self.root), [{"group_id": "demo", "msg_ids": [10, 11, 12]}])
        self.assertEqual(result["missing_ids"], [12])
        self.assertEqual(len(result["split_albums"]), 1)

    def test_manifest_schema(self):
        self.put("1/msg_1.jpg")
        path = verify.write_manifest(self.root, verify.inspect_local(self.root))
        with path.open(newline="", encoding="utf-8") as stream:
            fields = next(csv.reader(stream))
        self.assertIn("flags", fields)
        self.assertNotIn("flag", fields)

    def test_image_decode_checks_corrupt_bytes(self):
        self.put("1/msg_1.jpg", b"not an image")
        self.assertEqual(len(verify.inspect_local(self.root, True)["invalid"]), 1)


class OCRTests(LocalCase):
    def test_heif_roundtrip(self):
        if not shared.enable_heif():
            self.skipTest("HEIF plugin not installed")
        file = self.root / "msg_1.heic"
        Image.new("RGB", (32, 32), "white").save(file, format="HEIF")
        with Image.open(file) as image:
            self.assertEqual(image.format, "HEIF")
        with Image.open(io.BytesIO(ocr.image_bytes(file))) as image:
            self.assertEqual(image.format, "JPEG")

    def test_year_does_not_become_folder(self):
        self.assertEqual(ocr.extract_numbers("Annual report 2026; building 123"), [123])

    def test_number_bounds(self):
        self.assertEqual(ocr.extract_numbers("No 0; No 405; No 406; No 12; No 12"), [405, 12])

    def test_valid_image_and_invalid_input(self):
        file = self.root / "msg_1.png"
        Image.new("RGB", (32, 32), "white").save(file)
        with Image.open(io.BytesIO(ocr.image_bytes(file))) as image:
            self.assertEqual(image.format, "JPEG")
        bad = self.put("bad.jpg", b"broken")
        with self.assertRaises(Exception):
            ocr.image_bytes(bad)

    def test_error_is_retried_and_success_cached(self):
        file = self.put("_unsorted/msg_1.png")
        cache = self.root / "_state/ocr_cache.json"
        attempts = 0
        def recognize(_):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return "Building 42"
        self.assertEqual(ocr.run_batch([file], self.root, cache, recognize)["error"], 1)
        self.assertEqual(ocr.run_batch([file], self.root, cache, recognize)["ok"], 1)
        self.assertEqual(ocr.run_batch([file], self.root, cache, recognize)["cached"], 1)
        self.assertEqual(attempts, 2)
        file.write_bytes(b"new version")
        self.assertEqual(ocr.run_batch([file], self.root, cache, recognize)["ok"], 1)

    def test_same_name_different_folders_has_separate_cache(self):
        a, b = self.put("a/msg_1.png"), self.put("b/msg_1.png")
        cache = self.root / "_state/ocr_cache.json"
        ocr.run_batch([a, b], self.root, cache, lambda _: "42")
        self.assertEqual(len(json.loads(cache.read_text())), 2)
        self.assertNotIn("msg_1.png", json.loads((self.root / "batch_ocr.json").read_text()))


class OptionalNetworkTests(LocalCase):
    def test_recovery_validates_download_before_adding_it(self):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(buffer, format="JPEG")
        image_bytes = buffer.getvalue()
        called = []
        class Client:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get_entity(self, group): called.append(group);return "fixture"
            async def get_messages(self, entity, ids): return types.SimpleNamespace(id=ids, photo=True, document=None)
            async def download_media(self, message, file):
                Path(file).write_bytes(image_bytes if message.id == 10 else b"")
                return file
        with patch.object(recover, "settings", return_value=(1, "test-only", "unused")):
            result = asyncio.run(recover.recover(self.root, "-100123", [10, 11], Client))
        self.assertEqual(called, [-100123])
        self.assertEqual(result, {"downloaded": 1, "skipped": 0, "failed": 1})
        self.assertTrue((self.root / "_unsorted/msg_10.jpg").exists())
        self.assertFalse(list(self.root.rglob("*.part")))

    def test_group_id_is_used_without_network(self):
        called = []
        class Client:
            def __init__(self, *args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get_entity(self, group): called.append(group);return object()
            async def iter_messages(self, entity, limit=None):
                yield types.SimpleNamespace(id=100, grouped_id=1, photo=True, document=None)
        with patch("telegram_config.settings", return_value=(1, "test-only", "unused")):
            albums = asyncio.run(verify.fetch_albums("-100123", Client))
        self.assertEqual(called, [-100123])
        self.assertEqual(albums[0]["msg_ids"], [100])

    def test_ids_are_explicit_and_validated(self):
        self.assertEqual(recover.parse_ids("10, 11,10"), [10, 11])
        for value in ("", "0", "-1", "10,abc"):
            with self.assertRaises(argparse.ArgumentTypeError):
                recover.parse_ids(value)

    def test_redownload_detects_existing_sorted_file(self):
        self.put("1/msg_10.jpg")
        self.put("_unsorted/msg_11.jpg", b"")
        self.assertEqual(recover.existing_ids(self.root), {10})

    def test_no_authentication_during_preview(self):
        with patch.object(recover, "settings", side_effect=AssertionError("must stay offline")), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(recover.main(["--data-dir", str(self.root), "--ids", "1", "--group", "demo"]), 0)


class DemoTests(LocalCase):
    def test_demo_is_synthetic_and_refuses_overwrite(self):
        create_demo(self.root)
        self.assertEqual(len(shared.image_paths(self.root)), 8)
        with self.assertRaises(ValueError):
            create_demo(self.root)


if __name__ == "__main__":
    unittest.main()
