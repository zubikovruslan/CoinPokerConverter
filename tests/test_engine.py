import copy
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.engine import Cancelled, DEFAULT_RULES, Options, Rejected, applicable_rule, convert, digest, parse, rank_seven, shared_row
from app.storage import export_history, import_history
from tests.fixtures import neighbor, regular, splash


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "hands.txt"
        self.history = self.root / "history.sqlite"

    def tearDown(self): self.temp.cleanup()

    def run_hands(self, hands, hero="TestPlayer", mode="all", **kw):
        self.source.write_text("\n\n".join(hands), "utf-8")
        return convert([self.source], self.root / "out", Options(hero, mode), self.history, **kw)

    def test_regular_splash_amounts_and_separate_files(self):
        result = self.run_hands([regular(), splash()])
        self.assertEqual((result["regular_hands"], result["splash_hands"], result["excluded_records"]), (1, 1, 0))
        self.assertEqual(result["totals"]["regular"]["net_cents"], -5)
        self.assertEqual(result["totals"]["splash"]["net_cents"], 380)
        with zipfile.ZipFile(result["archive"]) as z:
            self.assertIsNone(z.testzip())
            self.assertIn(b"Cash Drop to Pot : total $2.00", z.read("Import/Splash.txt"))
            self.assertIn(b"TestPlayer collected $5.80", z.read("Import/Splash.txt"))
            self.assertNotIn(b"Cash Drop", z.read("Import/Regular.txt"))

    def test_new_mode_and_other_profile(self):
        self.run_hands([regular()])
        result = self.run_hands([regular(), splash()], mode="new")
        self.assertEqual((result["exported_hands"], result["previously_exported_skipped"]), (1, 1))
        result = self.run_hands([regular(), splash()], mode="new")
        self.assertEqual((result["exported_hands"], result["previously_exported_skipped"]), (0, 2))
        result = self.run_hands([regular()], hero="AnotherPlayer", mode="new")
        self.assertEqual(result["exported_hands"], 1)

    def test_exact_duplicates_and_conflicting_ids(self):
        result = self.run_hands([regular(), regular(), regular().replace("As Kd", "Qs Kd")])
        self.assertEqual((result["exact_duplicates"], result["conflicting_ids"], result["exported_hands"], result["excluded_records"]), (1, 1, 0, 2))

    def test_changed_previously_exported_hand_not_silently_skipped(self):
        self.run_hands([regular()])
        result = self.run_hands([regular().replace("As Kd", "Qs Kd")], mode="new")
        self.assertEqual(result["reasons"], {"changed_export": 1})
        self.assertEqual(self.run_hands([regular().replace("As Kd", "Qs Kd")])["exported_hands"], 1)

    def test_cashout_requires_splash_next_stack_evidence(self):
        result = self.run_hands([splash(cashout=True)])
        self.assertEqual(result["reasons"], {"cashout_evidence": 1})
        result = self.run_hands([splash(cashout=True), neighbor()], mode="new")
        self.assertEqual((result["splash_hands"], result["regular_hands"]), (1, 1))
        bad = neighbor().replace("Hero (₮5.80", "Hero (₮5.81")
        result = self.run_hands([splash(cashout=True), bad])
        self.assertEqual(result["reasons"], {"cashout_evidence": 1})

    def test_hero_cashout_and_three_boards_excluded(self):
        result = self.run_hands([splash(cashout=True, hero_cashout=True)])
        self.assertEqual(result["reasons"], {"hero_cashout": 1})
        result = self.run_hands([splash().replace("*** FLOP", "*** THIRD FLOP").replace("*** TURN", "*** THIRD TURN").replace("*** RIVER", "*** THIRD RIVER").replace("*** SHOWDOWN", "*** THIRD SHOWDOWN").replace("Board [", "THIRD Board [")])
        self.assertEqual(result["reasons"], {"three_boards": 1})

    def test_conflicting_cashout_rejected(self):
        hand = splash(cashout=True).replace("*** FLOP ***", "Rival cashed out the hand for ₮2.01 | Cash Out Fee ₮0.06\n*** FLOP ***")
        result = self.run_hands([hand, neighbor()])
        self.assertIn("cashout_conflict", result["reasons"])

    def test_rename_does_not_change_table_or_other_players(self):
        hand = regular().replace("'Synthetic'", "'Hero'").replace("Rival", "Hero2")
        result = self.run_hands([hand], hero="Мой_Ник")
        with zipfile.ZipFile(result["archive"]) as z:
            text = z.read("Import/Regular.txt").decode()
            self.assertIn("Table 'CoinPoker_Hero'", text)
            self.assertIn("Hero2", text)
            self.assertIn("Dealt to Мой_Ник", text)
        result = self.run_hands([regular(hero="ActualNick")], hero="ActualNick")
        self.assertEqual(result["exported_hands"], 1)
        result = self.run_hands([regular().replace("Rival", "TestPlayer")])
        self.assertEqual(result["reasons"], {"hero": 1})

    def test_shared_percent_dates_and_fractional_cents(self):
        h = parse(splash(), "Hero")
        h["bb"], h["splash"] = 10, 150
        h["players"] = {str(i): {} for i in range(6)}
        row = shared_row(h, Options("Hero"), "exported")
        self.assertEqual((row["full_chest_cents_fraction"], row["shared_total_cents_fraction"], row["hero_shared_cents_fraction"]), ("375", "225", "75/2"))
        self.assertEqual(row["actual_wallet_cents"], "")
        rules = copy.deepcopy(DEFAULT_RULES) + [{"bb_cents": 10, "shared_percent": 80, "since": "2026-11-02"}]
        self.assertEqual(applicable_rule(h, rules)["shared_percent"], 60)
        h["date"] = "2026/11/02 01:00:00"
        self.assertEqual(applicable_rule(h, rules)["shared_percent"], 80)
        with self.assertRaises(Rejected): applicable_rule(h, [{"bb_cents": 10, "shared_percent": 100, "since": "1900-01-01"}])

    def test_malformed_hand_does_not_drop_other_hands(self):
        result = self.run_hands([regular(), splash().replace("Hero: ALLIN", "Hero: UNKNOWN")])
        self.assertEqual((result["exported_hands"], result["excluded_records"]), (1, 1))
        self.assertEqual(result["reasons"], {"format": 1})

    def test_cancel_and_write_failure_do_not_consume_history(self):
        self.source.write_text(regular(), "utf-8")
        cancelled = [False]
        def progress(text, current, total):
            if text.startswith("Создание"): cancelled[0] = True
        with self.assertRaises(Cancelled): convert([self.source], self.root/"out", Options("Hero"), self.history, progress, lambda: cancelled[0])
        self.assertFalse(list((self.root/"out").glob("*.zip")))
        with patch("app.engine.zipfile.ZipFile.writestr", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError): self.run_hands([regular()], hero="Hero")
        result = self.run_hands([regular()], hero="Hero", mode="new")
        self.assertEqual(result["exported_hands"], 1)

    def test_zip_read_does_not_extract_traversal_paths(self):
        source = self.root/"input.zip"
        with zipfile.ZipFile(source, "w") as z: z.writestr("../../escaped.txt", regular())
        result = convert([source], self.root/"out", Options("Hero"), self.history)
        self.assertEqual(result["exported_hands"], 1)
        self.assertFalse((self.root.parent/"escaped.txt").exists())

    def test_history_round_trip_and_conflict_rollback(self):
        self.run_hands([regular()])
        path = self.root/"portable.json"
        self.assertEqual(export_history(self.history, path), 1)
        other = self.root/"other.sqlite"
        self.assertEqual(import_history(other, path), 1)
        self.assertEqual(import_history(other, path), 0)
        data = json.loads(path.read_text())
        data["exports"][0][2] = "0"*64
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): import_history(other, path)
        db = sqlite3.connect(other)
        self.assertNotEqual(db.execute("SELECT source_sha FROM exports").fetchone()[0], "0"*64)
        db.close()

    def test_negative_money_and_hero_input_validation(self):
        from app.engine import money
        self.assertEqual(money(-5), "-0.05")
        for name in ["", "a\nb", "x: y", "a"*51]:
            with self.assertRaises(ValueError): Options(name).validate()

    def test_poker_rank_straight_flush_beats_quads(self):
        self.assertGreater(rank_seven("As Ks Qs Js Ts 2d 3c".split()), rank_seven("Ac Ad Ah As Kd Qc 2s".split()))
        self.assertGreater(rank_seven("6s 5d 4c 3h 2c Ad Kh".split()), rank_seven("As 2s 3d 4h 5c 9d Tc".split()))

    def test_invalid_event_board_fold_payout_and_long_id_quarantined(self):
        for hand, code in [
            (splash().replace("[Jc]\n*** SHOWDOWN", "[Kc]\n*** SHOWDOWN"), "cards"),
            (regular().replace("Rival collected ₮0.10", "Hero collected ₮0.10"), "actions"),
            (regular().replace("Board []\n", "").replace("As Kd", "ZZ XX"), "cards"),
            (regular("9"*5000), "format"),
        ]:
            with self.subTest(code=code):
                result = self.run_hands([hand, regular("999999")])
                self.assertEqual(result["exported_hands"], 1)
                self.assertEqual(result["reasons"], {code: 1})

    def test_corrupted_config_is_validation_error(self):
        from app.storage import load_settings
        path = self.root/"settings.json"
        for data in [[], {"schema": 1, "profiles": {"Hero": {"rules": [None]}}}, {"schema": 1, "profiles": {"Hero": None}}]:
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError): load_settings(path)


if __name__ == "__main__": unittest.main(verbosity=2)
