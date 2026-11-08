"""Independent reviewer regressions. Synthetic data only; no private hands.

Run from project root: python3 -m unittest tests.test_independent -v
These tests specify observed requirements rather than duplicating the implementation.
"""
import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import engine
from app.engine import Options, convert
from app.storage import load_settings
from tests.fixtures import regular, splash


class ReviewChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.b = Path(self.tmp.name)
        self.src = self.b / "hands.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def conversion(self, hands, rules=None):
        self.src.write_text("\n\n".join(hands), "utf-8")
        options = Options("ReviewHero")
        if rules is not None: options.rules = rules
        return convert([self.src], self.b/"out", options, self.b/"history.sqlite")

    def test_street_and_summary_boards_must_agree(self):
        bad = splash().replace("*** RIVER *** [2c 3d 7h 9s] [Jc]", "*** RIVER *** [2c 3d 7h 9s] [Kc]")
        r = self.conversion([bad, regular("999999")])
        self.assertEqual(r["exported_hands"], 1)
        self.assertEqual(r["excluded_records"], 1)

    def test_folded_player_cannot_collect(self):
        bad = regular().replace("Rival collected ₮0.10 from pot", "Hero collected ₮0.10 from pot")
        r = self.conversion([bad, regular("999999")])
        self.assertEqual(r["exported_hands"], 1)
        self.assertEqual(r["excluded_records"], 1)

    def test_hole_card_validation_without_board(self):
        bad = regular().replace("[As Kd]", "[ZZ XX]").replace("Board []\n", "")
        r = self.conversion([bad, regular("999999")])
        self.assertEqual(r["exported_hands"], 1)
        self.assertEqual(r["excluded_records"], 1)

    def test_oversized_id_is_per_hand_rejection(self):
        r = self.conversion([regular(hid="1"*5000), regular("999999")])
        self.assertEqual(r["exported_hands"], 1)
        self.assertEqual(r["excluded_records"], 1)

    def test_structurally_invalid_settings_raise_valueerror(self):
        for body in [[], {"schema":1,"profiles":{"ReviewHero":{"rules":[None]}}},
                     {"schema":1,"profiles":{"ReviewHero":{"rules":"wrong"}}}]:
            p = self.b/"settings.json"
            p.write_text(json.dumps(body), "utf-8")
            with self.assertRaises(ValueError): load_settings(p)

    def test_zero_contested_marker_not_exported_as_regular(self):
        hand = regular().replace("*** HOLE CARDS ***", "SPLASH dropped ₮0.00\n*** HOLE CARDS ***")
        r = self.conversion([hand], [{"bb_cents":10,"shared_percent":100,"since":"1900-01-01"}])
        self.assertEqual(r["exported_hands"], 0)
        self.assertEqual(r["excluded_records"], 1)

    def test_archive_survives_history_insert_failure_and_is_reported(self):
        real_connect = engine.connect_history
        class FaultDB:
            def __init__(self, db): self.db = db
            def execute(self, sql, *args):
                if sql.startswith("INSERT INTO exports"):
                    raise sqlite3.OperationalError("simulated disk full on INSERT")
                return self.db.execute(sql, *args)
            def __getattr__(self, key): return getattr(self.db, key)
        with patch("app.engine.connect_history", side_effect=lambda p: FaultDB(real_connect(p))):
            r = self.conversion([regular()])
        self.assertTrue(Path(r["archive"]).is_file())
        self.assertTrue(r.get("history_warning"))
        with sqlite3.connect(self.b/"history.sqlite") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exports").fetchone()[0], 0)

    def test_duplicate_conflict_variants_count_once_each(self):
        variant = regular().replace("As Kd", "Qh Kd")
        r = self.conversion([regular(), variant, variant])
        self.assertEqual(r["exact_duplicates"], 1)
        self.assertEqual(r["excluded_records"], 2)

    def test_action_after_fold_is_rejected(self):
        bad = regular().replace("Rival collected", "Hero: checks\nRival collected", 1)
        r = self.conversion([bad, regular("999999")])
        self.assertEqual((r["exported_hands"], r["excluded_records"]), (1, 1))

    def test_known_hu_showdown_loser_cannot_get_splash(self):
        bad = splash().replace("Hero collected ₮3.80 from pot", "Rival collected ₮3.80 from pot")
        r = self.conversion([bad, regular("999999")])
        self.assertEqual((r["exported_hands"], r["excluded_records"]), (1, 1))

    def test_non_allin_undercall_is_rejected(self):
        bad = regular().replace("Hero: folds", "Hero: calls ₮0.01").replace(
            "Rival: RETURN ₮0.05", "Rival: RETURN ₮0.04\n*** FLOP *** [2c 3d 4h]\nHero: folds").replace(
            "₮0.10 from pot", "₮0.12 from pot").replace("Total pot ₮0.10", "Total pot ₮0.12").replace("Board []", "Board [2c 3d 4h]")
        r = self.conversion([bad, regular("999999")])
        self.assertEqual((r["exported_hands"], r["excluded_records"]), (1, 1))


if __name__ == "__main__": unittest.main(verbosity=2)
