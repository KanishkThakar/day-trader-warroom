import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _load import load, FIX, ROOT

wr = load("trader-warroom", "warroom")

SCRIPT = os.path.join(ROOT, "trader-warroom", "warroom.py")
TM = os.path.join(ROOT, "trader-timemachine", "timemachine.py")
TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")  # 60 days of 5-min bars to 2026-10-19 10:49 ET


def run(args, cwd):
    p = subprocess.run([sys.executable, SCRIPT] + args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def board_of(cwd):
    with open(os.path.join(cwd, "trader", "warroom", "latest.json")) as fh:
        latest = json.load(fh)
    with open(os.path.join(latest["run"], "warroom.json")) as fh:
        board = json.load(fh)
    with open(latest["html"], encoding="utf-8") as fh:
        html = fh.read()
    return board, html


def bar(slot, o, h, l, c, known=True):
    m = wr.OPEN_MIN + 5 * slot
    return {"slot": slot, "start": wr.clock24(m), "o": o, "h": h, "l": l, "c": c, "v": 1000,
            "close_utc": "2026-10-19T%02d:%02d:00Z" % divmod(m + 5 + 240, 60), "known": known}


PLAN = {"entry": 100.0, "stop": 99.0, "target": 102.0, "entry_bar": 3, "flat_by": "15:55"}


class TheReel(unittest.TestCase):
    """TSLA, Monday 2026-10-19 at 10:15, the reel's trade, end to end through the whole pack."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.code, cls.out = run(["run", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:15", "--target", "1R"],
                                cls.tmp)
        cls.board, cls.html = board_of(cls.tmp)
        cls.card = cls.board["cards"][0]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_runs_and_says_so(self):
        self.assertEqual(self.code, 0, self.out)
        self.assertIn("WAR ROOM  TSLA  Mon 2026-10-19 10:15 AM ET  (replay)", self.out)
        self.assertIn("TRIGGERED 10:15 AM at $364.18", self.out)
        self.assertIn("Nothing was placed", self.out)

    def test_the_plan_matches_the_reel(self):
        p = self.card["plan"]
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk_per_share"]), (364.18, 358.32, 370.04, 5.86))
        self.assertEqual(p["entry_time"], "10:15 AM")
        self.assertEqual(self.card["entry"]["opening_range"]["high"], 362.18)

    def test_the_order_matches_the_reel(self):
        od = self.card["orders"]["orders"][0]
        self.assertEqual((od["qty"], od["stop_loss_stop_price"], od["take_profit_limit_price"]),
                         ("8", "358.32", "370.04"))
        self.assertEqual(od["plan"]["max_loss"], 46.88)
        self.assertEqual(self.card["status"], "ready")
        self.assertEqual(self.card["risk"]["results"][0]["verdict"], "PASS")

    def test_the_time_machine_numbers_are_the_time_machines(self):
        tmp = tempfile.mkdtemp()
        try:
            p = subprocess.run([sys.executable, TM, "run", "--symbol", "TSLA", "--date", "2026-10-19",
                                "--entry", "364.18", "--stop", "358.32", "--target", "370.04",
                                "--entry-time", "10:15 AM", "--until", "15:55", "--offline", TSLA,
                                "--now", "2026-10-19 10:15", "--json"], cwd=tmp, stdout=subprocess.PIPE,
                               universal_newlines=True, timeout=120)
            direct = json.loads(p.stdout)["mc"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        mine = self.card["tm"]["mc"]
        for k in ("p_target", "p_stop", "p_neither", "p_green", "avg_R", "median_close", "seed", "replays"):
            self.assertEqual(mine[k], direct[k], k)
        self.assertEqual(len(mine["sample_paths"]), wr.SAMPLE_KEEP)

    def test_the_fresh_price_is_the_last_closed_bar(self):
        px = self.card["price_now"]
        self.assertEqual(px["price"], 364.18)
        self.assertIn("10:10 bar", px["from"])

    def test_the_bars_after_now_are_ghosts_and_say_what_happened(self):
        bars = self.card["bars"]
        self.assertTrue(any(not b["known"] for b in bars))
        self.assertTrue(all(b["known"] for b in bars if b["slot"] <= 8))
        self.assertIn(self.card["actual"]["outcome"], ("open", "stop", "target"))
        self.assertEqual(self.card["so_far"]["outcome"], "open")

    def test_a_replay_is_never_promoted(self):
        self.assertTrue(self.board["replay"])
        self.assertNotIn("promoted", self.board)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "trader", "orders.json")))
        code, out = run(["pick", "TSLA"], self.tmp)
        self.assertEqual(code, 1)
        self.assertIn("replay", out)

    def test_the_page_carries_the_board(self):
        start = self.html.index("const BOARD = ") + len("const BOARD = ")
        end = self.html.index(";</script>", start)
        data = json.loads(self.html[start:end].replace("<\\/", "</"))
        self.assertEqual(data["cards"][0]["symbol"], "TSLA")
        self.assertNotIn("/*__BOARD__*/", self.html)
        self.assertNotIn("http://", self.html.split("<script>")[0])  # no external stylesheet or font


class AtTheTrigger(unittest.TestCase):
    def test_a_later_clock_is_moved_back_to_the_trigger(self):
        tmp = tempfile.mkdtemp()
        try:
            code, out = run(["run", "TSLA", "--offline", TSLA, "--now", "2026-10-19 10:40", "--target", "1R",
                             "--at-trigger", "--no-news"], tmp)
            board, _ = board_of(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        card = board["cards"][0]
        self.assertEqual(code, 0, out)
        self.assertTrue(card["judged_at_trigger"])
        self.assertEqual(card["now_ny"], "Mon 2026-10-19 10:15 AM ET")
        self.assertEqual(card["orders"]["orders"][0]["qty"], "8")

    def test_at_trigger_needs_a_replay(self):
        code, out = run(["run", "TSLA", "--at-trigger"], tempfile.gettempdir())
        self.assertEqual(code, 1)
        self.assertIn("for replays", out)


class OfflineScan(unittest.TestCase):
    def test_a_folder_of_charts_becomes_a_ranked_board(self):
        tmp = tempfile.mkdtemp()
        try:
            charts = os.path.join(tmp, "charts")
            os.makedirs(charts)
            shutil.copyfile(TSLA, os.path.join(charts, "TSLA.json.gz"))
            code, out = run(["scan", "--offline", charts, "--symbols", "TSLA,ZZZZ", "--now", "2026-10-19 10:15",
                             "--target", "1R"], tmp)
            board, html = board_of(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(board["mode"], "scan")
        self.assertEqual([c["symbol"] for c in board["cards"]], ["TSLA", "ZZZZ"])
        self.assertEqual(board["cards"][0]["rank"], 1)
        self.assertEqual(board["cards"][1]["status"], "no data")
        self.assertEqual(board["replays_total"], 10000)


class Reality(unittest.TestCase):
    def test_stop_first_when_one_bar_touches_both(self):
        bars = [bar(4, 100, 101, 99.5, 100.5), bar(5, 100.5, 102.5, 98.9, 101)]
        r = wr.reality(bars, PLAN)
        self.assertEqual((r["outcome"], r["R"], r["at"]), ("stop", -1.0, "10:00 AM"))

    def test_target_first(self):
        bars = [bar(4, 100, 101, 99.5, 100.5), bar(5, 100.5, 102.1, 99.6, 102)]
        r = wr.reality(bars, PLAN)
        self.assertEqual((r["outcome"], r["R"]), ("target", 2.0))

    def test_neither_by_the_flat_by_time(self):
        bars = [bar(i, 100, 100.5, 99.5, 100.25) for i in range(4, 78)]
        r = wr.reality(bars, PLAN)
        self.assertEqual((r["outcome"], r["R"], r["at"]), ("close", 0.25, "3:55 PM"))

    def test_open_when_the_data_stops(self):
        bars = [bar(4, 100, 100.5, 99.5, 100.4), bar(5, 100.4, 100.6, 99.8, 100.5)]
        r = wr.reality(bars, PLAN)
        self.assertEqual((r["outcome"], r["R"]), ("open", 0.5))

    def test_a_cut_ignores_bars_after_it(self):
        bars = [bar(4, 100, 100.5, 99.5, 100.4), bar(5, 100.4, 100.6, 98.0, 98.5)]
        cut = wr.parse_iso(bars[0]["close_utc"])
        self.assertEqual(wr.reality(bars, PLAN, cut)["outcome"], "open")
        self.assertEqual(wr.reality(bars, PLAN)["outcome"], "stop")

    def test_a_missing_bar_in_the_middle_is_skipped(self):
        bars = [bar(4, 100, 100.5, 99.5, 100.4), bar(6, 100.4, 102.5, 100.1, 102.2)]
        self.assertEqual(wr.reality(bars, PLAN)["outcome"], "target")

    def test_cone_position(self):
        bands = [[0, 0, 0, 0, 0], [90, 95, 100, 105, 110]]
        self.assertEqual(wr.cone_position(89, bands), "below the 5th percentile")
        self.assertEqual(wr.cone_position(101, bands), "between the 50th and 75th percentiles")
        self.assertEqual(wr.cone_position(111, bands), "above the 95th percentile")


class Pieces(unittest.TestCase):
    def test_rank_puts_replayed_cards_first_by_average_R(self):
        cards = [{"symbol": "A", "status": "waiting"},
                 {"symbol": "B", "status": "ready", "tm": {"mc": {"avg_R": 0.05}}},
                 {"symbol": "C", "status": "blocked", "tm": {"mc": {"avg_R": 0.20}}}]
        self.assertEqual([c["symbol"] for c in wr.rank(cards)], ["C", "B", "A"])
        self.assertEqual([c["rank"] for c in cards if "rank" in c], [3, 2, 1])

    def test_price_now_prefers_a_fresh_tick(self):
        now = wr.parse_now("2026-10-19 10:15")
        bars = [bar(8, 1, 1, 1, 364.18)]
        live = wr.price_now(bars, {"regularMarketPrice": 365.0}, now, now)
        self.assertEqual((live["price"], live["from"]), (365.0, "Yahoo's last trade"))
        later = wr.parse_now("2026-10-19 10:49")
        old = wr.price_now(bars, {"regularMarketPrice": 366.07}, now, later)
        self.assertEqual(old["price"], 364.18)

    def test_the_page_cannot_be_broken_out_of(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "x.html")
            wr.render_html({"cards": [{"headline": "</script><script>alert(1)</script>"}]}, path)
            with open(path, encoding="utf-8") as fh:
                page = fh.read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertNotIn("</script><script>alert", page)

    def test_parse_now_reads_new_york_time(self):
        self.assertEqual(wr.iso(wr.parse_now("2026-10-19 10:15")), "2026-10-19T14:15:00Z")
        self.assertEqual(wr.iso(wr.parse_now("2026-12-01 10:15")), "2026-12-01T15:15:00Z")
        self.assertEqual(wr.iso(wr.parse_now("2026-10-19T14:15:00Z")), "2026-10-19T14:15:00Z")


if __name__ == "__main__":
    unittest.main()
