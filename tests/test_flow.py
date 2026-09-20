import json
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from reconcile import reconcile_game
from finalize_game import finalize
from import_lolesports_history import collect, detect_pauses
from datetime import datetime, timezone, timedelta
from crawl_training_history import candidates
from schedule_index import completed_matches, league_matches
from predictor import BASELINE, predict
from provider import Collector, normalize, normalize_cito
from lolesports_provider import LoLEsportsCollector, MultiLeagueCollector
from storage import game, lists, save
from sync_completed_history import recent_completed
from train_model import load_games, split_by_match


def frame(seconds, finished=False):
    return {"game_id": 101, "match_id": 9, "current_timestamp": seconds,
            "finished": finished, "winner_id": 1 if finished else None,
            "blue": {"id": 1, "name": "蓝队", "gold": 18000 + seconds, "kills": 3,
                     "towers": 1, "drakes": 1, "nashors": 0, "inhibitors": 0},
            "red": {"id": 2, "name": "红队", "gold": 17000, "kills": 2,
                    "towers": 0, "drakes": 0, "nashors": 0, "inhibitors": 0}}


class FlowTest(unittest.TestCase):
    def test_multileague_partial_failure_is_warning_not_global_error(self):
        def fake(league, error):
            return type("FakeCollector", (), {"league_slug": league, "last_error": error,
                "phase": "idle", "scheduled_start": None, "active_game_id": None,
                "source_lag_seconds": None, "last_frame_at": None,
                "status": lambda self: {"league": league, "error": error}})()
        healthy = fake("lpl", None)
        broken = fake("lec", "temporary timeout")
        collector = MultiLeagueCollector([healthy, broken])
        self.assertIsNone(collector.last_error)
        self.assertIn("lec", collector.last_warning)
        all_broken = MultiLeagueCollector([broken, fake("lck", "temporary timeout")])
        self.assertIn("lec", all_broken.last_error)

    def test_lolesports_collector_reports_idle_when_schedule_has_no_match(self):
        empty = {"rehydrate": {"query": {"data": {"esports": {"events": []}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(empty) + ')</script>'
        collector = LoLEsportsCollector("unused.sqlite3", BASELINE,
            schedule_fetch=lambda league: html)
        collector.run_once(now=1000)
        self.assertEqual(collector.phase, "idle")
        self.assertIsNone(collector.active_game_id)
        self.assertIsNone(collector.last_error)

    def test_lolesports_collector_ignores_stale_unstarted_completed_series(self):
        event = {"id": "9", "type": "match", "state": "unstarted",
                 "startTime": "2026-09-20T09:00:00Z", "league": {"slug": "vcs"},
                 "matchTeams": [{"id": "9:1", "code": "A"}, {"id": "9:2", "code": "B"}],
                 "match": {"id": "9", "games": [
                     {"id": "101", "number": 1, "state": "completed"},
                     {"id": "102", "number": 2, "state": "completed"}]}}
        embedded = {"rehydrate": {"query": {"data": {"esports": {"events": [event]}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(embedded) + ')</script>'
        collector = LoLEsportsCollector("unused.sqlite3", BASELINE, league_slug="vcs",
            schedule_fetch=lambda league: html)
        collector.run_once(now=1000)
        self.assertEqual(collector.phase, "idle")
        self.assertIsNone(collector.match_id)
        self.assertEqual(collector.game_ids, [])

    def test_late_join_saves_recovered_early_frames(self):
        now = datetime.now(timezone.utc)
        beginning = now - timedelta(minutes=21)
        current = now - timedelta(minutes=1)
        def source(at):
            return {"rfc460Timestamp": at.isoformat().replace("+00:00", "Z"),
                    "gameState": "in_game",
                    "blueTeam": {"totalGold": 2500, "totalKills": 0, "towers": 0,
                                 "dragons": [], "barons": 0, "inhibitors": 0},
                    "redTeam": {"totalGold": 2500, "totalKills": 0, "towers": 0,
                                "dragons": [], "barons": 0, "inhibitors": 0}}
        def payload(frames):
            return {"esportsGameId": "101", "esportsMatchId": "9",
                    "gameMetadata": {"blueTeamMetadata": {"esportsTeamId": "1"},
                                     "redTeamMetadata": {"esportsTeamId": "2"}},
                    "frames": frames}
        def fetch(kind, game_id, start=None):
            if start is None or start > now - timedelta(minutes=15):
                return payload([source(current)])
            if start > now - timedelta(minutes=30):
                return payload([source(beginning)])
            return None
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "late.sqlite3")
            collector = LoLEsportsCollector(db, BASELINE, match_id="9", game_ids=["101"],
                fetch=fetch, schedule_fetch=lambda league: "")
            collector.team_names = {"1": "蓝队", "2": "红队"}
            collector._poll_game("101")
            curve = game(db, "101")
            self.assertEqual(curve["points"][0]["time"], 0)
            self.assertGreater(curve["points"][-1]["time"], 1100)

    def test_expired_unfinished_game_is_pending_history_not_live_or_stale(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "expired.sqlite3")
            unfinished = normalize(frame(900))
            save(db_path, unfinished, .63, BASELINE["kind"])
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE frames SET received_at=?", (time.time() - 3600,))
            result = lists(db_path)
            self.assertEqual(result["live"], [])
            self.assertEqual(result["stale"], [])
            self.assertEqual(result["pending"][0]["game_id"], "101")
            self.assertEqual(result["history"][0]["game_id"], "101")

    def test_storage_migrates_legacy_database_and_builds_competition_catalog(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "legacy.sqlite3")
            with sqlite3.connect(db_path) as db:
                db.execute("""CREATE TABLE frames (
                    game_id TEXT NOT NULL, match_id TEXT NOT NULL, game_time INTEGER NOT NULL,
                    received_at REAL NOT NULL, finished INTEGER NOT NULL, winner_id TEXT,
                    blue_name TEXT NOT NULL, red_name TEXT NOT NULL, blue_probability REAL NOT NULL,
                    model_kind TEXT NOT NULL, frame_json TEXT NOT NULL,
                    PRIMARY KEY (game_id, game_time))""")
            completed = normalize(frame(1200, finished=True))
            completed["competition"] = {"league_slug": "lck", "league_name": "LCK",
                "tournament_name": "2026 Season", "stage_name": "Finals"}
            save(db_path, completed, .75, BASELINE["kind"])
            result = lists(db_path)
            self.assertEqual(result["history"][0]["frame"]["competition"]["league_slug"], "lck")
            self.assertEqual(result["catalog"][0]["tournaments"][0]["stages"][0],
                             {"name": "Finals", "count": 1})

    def test_free_lolesports_collector_waits_then_saves_live_frame(self):
        event = {"id": "9", "type": "match", "state": "unstarted",
                 "startTime": "2026-09-19T09:00:00Z", "league": {"slug": "lpl"},
                 "matchTeams": [{"id": "9:1", "code": "蓝队"},
                                {"id": "9:2", "code": "红队"}],
                 "match": {"id": "9", "games": [{"id": "101", "number": 1,
                                                        "state": "unstarted"}]}}
        embedded = {"rehydrate": {"query": {"data": {"esports": {"events": [event]}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(embedded) + ')</script>'
        source = {"rfc460Timestamp": "2026-09-19T09:00:05.000Z", "gameState": "in_game",
                  "blueTeam": {"totalGold": 2500, "totalKills": 0, "towers": 0,
                               "dragons": [], "barons": 0, "inhibitors": 0},
                  "redTeam": {"totalGold": 2500, "totalKills": 0, "towers": 0,
                              "dragons": [], "barons": 0, "inhibitors": 0}}
        payload = {"esportsGameId": "101", "esportsMatchId": "9",
                   "gameMetadata": {"blueTeamMetadata": {"esportsTeamId": "1"},
                                    "redTeamMetadata": {"esportsTeamId": "2"}},
                   "frames": [source]}
        ready = {"value": False}
        recent_failures = {"remaining": 1}
        def fetch(kind, game_id, start=None):
            if ready["value"] and start is not None and recent_failures["remaining"]:
                recent_failures["remaining"] -= 1
                raise urllib.error.HTTPError("https://example.test", 400, "ahead", {}, None)
            return payload if ready["value"] else None
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "live.sqlite3")
            collector = LoLEsportsCollector(db, BASELINE, match_id="9", game_ids=["101"],
                poll_seconds=1, fetch=fetch, schedule_fetch=lambda league: html)
            collector.run_once(now=1000)
            self.assertEqual(collector.phase, "scheduled")
            start = datetime(2026, 9, 19, 9, tzinfo=timezone.utc).timestamp()
            collector.run_once(now=start)
            self.assertEqual(collector.phase, "preparing")
            self.assertIsNone(game(db, "101"))
            ready["value"] = True
            collector.run_once(now=start + 1)
            self.assertEqual(collector.phase, "in_game")
            self.assertEqual(recent_failures["remaining"], 0)
            self.assertEqual(collector.active_game_id, "101")
            stored = game(db, "101")["latest"]
            self.assertEqual(stored["model_kind"], BASELINE["kind"])
            self.assertEqual(stored["frame"]["competition"]["league_slug"], "lpl")

    def test_public_schedule_ids_are_checked_against_manual_per_game_labels(self):
        event = {"id": "9", "type": "match", "state": "completed", "startTime": "2026-09-12T09:00:00Z",
                 "league": {"slug": "lpl"}, "matchTeams": [
                     {"code": "AL", "id": "9:1", "result": {"gameWins": 1}},
                     {"code": "IG", "id": "9:2", "result": {"gameWins": 1}}],
                 "match": {"id": "9", "games": [
                     {"id": "101", "number": 1, "state": "completed"},
                     {"id": "102", "number": 2, "state": "completed"}]}}
        embedded = {"rehydrate": {"query": {"data": {"esports": {"events": [event]}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(embedded) + ')</script>'
        matches = completed_matches(html)
        manifest = {"series": [{"match_id": "9", "date": "2026-09-12", "teams": ["AL", "IG"],
                                "gol_first_game": 1000, "games": [
                                    {"winner": "AL", "duration": "30:05"},
                                    {"winner": "IG", "duration": "32:07"}]}]}
        indexed = candidates(manifest, matches)
        self.assertEqual([(x["game_id"], x["winner_code"], x["label_source"]) for x in indexed],
                         [("101", "AL", "https://gol.gg/game/stats/1000/page-game/"),
                          ("102", "IG", "https://gol.gg/game/stats/1001/page-game/")])
        manifest["series"][0]["games"][1]["winner"] = "AL"
        with self.assertRaisesRegex(ValueError, "disagree with series total"):
            candidates(manifest, matches)

    def test_schedule_index_can_select_another_region(self):
        event = {"id": "10", "type": "match", "state": "completed",
                 "startTime": "2026-09-13T05:00:00Z", "league": {"slug": "lck"},
                 "matchTeams": [], "match": {"id": "10", "games": []}}
        embedded = {"rehydrate": {"query": {"data": {"esports": {"events": [event]}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(embedded) + ')</script>'
        self.assertEqual(set(league_matches(html, "lck")), {"10"})
        with self.assertRaisesRegex(ValueError, "no matching lec matches"):
            league_matches(html, "lec")

    def test_recent_completed_history_discovery_keeps_unlabeled_games(self):
        event = {"id": "9", "type": "match", "state": "completed",
                 "startTime": "2026-09-18T09:00:00Z", "blockName": "Regional Qualifier",
                 "league": {"slug": "lpl", "name": "LPL"},
                 "tournament": {"name": "Split 3 2026"},
                 "matchTeams": [{"id": "9:1", "code": "WE"}, {"id": "9:2", "code": "JDG"}],
                 "match": {"id": "9", "games": [{"id": "101", "number": 1,
                                                       "state": "completed"}]}}
        embedded = {"rehydrate": {"query": {"data": {"esports": {"events": [event]}}}}}
        html = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(' + json.dumps(embedded) + ')</script>'
        items = recent_completed(["lpl"], 14, datetime(2026, 9, 19, tzinfo=timezone.utc),
                                 schedule_fetch=lambda league: html)
        self.assertEqual(items[0]["game_id"], "101")
        self.assertEqual(items[0]["competition"]["stage_name"], "Regional Qualifier")

    def test_training_holdout_keeps_entire_series_together(self):
        games = [(day * 10 + number, "%d-%d" % (day, number), [([0.0] * 7, number % 2)], str(day))
                 for day in range(6) for number in range(2)]
        train, test, train_matches, test_matches = split_by_match(games)
        self.assertEqual((train_matches, test_matches), (4, 2))
        self.assertEqual({game[3] for game in train}, {"0", "1", "2", "3"})
        self.assertEqual({game[3] for game in test}, {"4", "5"})

    def test_completed_lolesports_history_needs_verified_winner_and_stable_teams(self):
        def feed_frame(second, state="in_game"):
            return {"rfc460Timestamp": "2026-09-13T09:%02d:00.000Z" % second,
                    "gameState": state,
                    "blueTeam": {"totalGold": 10000 + 100 * second, "totalKills": 2,
                                 "towers": 1, "dragons": ["ocean"], "barons": 0, "inhibitors": 0},
                    "redTeam": {"totalGold": 11000 + 100 * second, "totalKills": 3,
                                "towers": 2, "dragons": [], "barons": 0, "inhibitors": 0}}

        def payload(frames, blue_id="1"):
            return {"esportsGameId": "101", "esportsMatchId": "9",
                    "gameMetadata": {"blueTeamMetadata": {"esportsTeamId": blue_id},
                                     "redTeamMetadata": {"esportsTeamId": "2"}},
                    "frames": frames}

        first = payload([feed_frame(0)])
        mid = payload([feed_frame(1)])
        tail = payload([feed_frame(2, "finished")])
        calls = iter([first, tail, mid])
        points = collect("101", {"blue": "BLG", "red": "AL"}, "red",
                         "https://gol.gg/game/stats/82967/page-game/",
                         step_seconds=60, pause_seconds=0, fetch=lambda game_id, start=None: next(calls))
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]["game_time"], 60)
        self.assertEqual(points[0]["blue"]["drakes"], 1)
        self.assertEqual(points[-1]["winner_id"], "2")
        self.assertTrue(points[-1]["finished"])
        self.assertEqual(points[-1]["label_source"], "https://gol.gg/game/stats/82967/page-game/")
        just_before = feed_frame(0)
        just_before["rfc460Timestamp"] = "2026-09-13T09:00:59.900Z"
        just_after = feed_frame(1)
        just_after["rfc460Timestamp"] = "2026-09-13T09:01:00.100Z"
        calls = iter([first, tail, payload([just_before]), payload([just_after])])
        recovered = collect("101", {"blue": "BLG", "red": "AL"}, "red",
                            "https://example.com/label", step_seconds=60, pause_seconds=0,
                            fetch=lambda game_id, start=None: next(calls))
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered[0]["source_timestamp"], "2026-09-13T09:01:00.100Z")
        calls = iter([first, payload([feed_frame(2, "finished")], blue_id="3")])
        with self.assertRaisesRegex(ValueError, "changed blue/red team IDs"):
            collect("101", {"blue": "BLG", "red": "AL"}, "red", "https://example.com/label",
                    step_seconds=60, pause_seconds=0, fetch=lambda game_id, start=None: next(calls))
        calls = iter([first, tail])
        with self.assertRaisesRegex(ValueError, "verified per-game duration"):
            collect("101", {"blue": "BLG", "red": "AL"}, "red", "https://example.com/label",
                    step_seconds=60, expected_duration_seconds=180, pause_seconds=0,
                    fetch=lambda game_id, start=None: next(calls))
        last_live = feed_frame(2)
        calls = iter([first, payload([last_live]), mid])
        reconciled = collect("101", {"blue": "BLG", "red": "AL"}, "red",
                             "https://example.com/label", expected_duration_seconds=120,
                             step_seconds=60, pause_seconds=0, fetch=lambda game_id, start=None: next(calls))
        self.assertTrue(reconciled[-1]["outcome_reconciled"])
        self.assertNotEqual(predict(reconciled[-1], BASELINE), 0.0)
        calls = iter([first, tail, mid])
        pending = collect("101", {"blue": "BLG", "red": "AL"}, None, None,
                          step_seconds=60, pause_seconds=0, archive_unlabeled=True,
                          fetch=lambda game_id, start=None: next(calls))
        self.assertTrue(pending[-1]["finished"])
        self.assertIsNone(pending[-1]["winner_id"])

    def test_recorded_pause_is_subtracted_from_elapsed_game_clock(self):
        beginning = datetime(2026, 9, 6, 7, tzinfo=timezone.utc)
        def response(at):
            stamp = (beginning + timedelta(seconds=at)).isoformat().replace("+00:00", "Z")
            state = "paused" if at == 20 else "in_game"
            return {"esportsGameId": "101", "esportsMatchId": "9",
                    "gameMetadata": {"blueTeamMetadata": {"esportsTeamId": "1"},
                                     "redTeamMetadata": {"esportsTeamId": "2"}},
                    "frames": [{"rfc460Timestamp": stamp, "gameState": state}]}
        def fetch(game_id, target):
            elapsed = round((target - beginning).total_seconds())
            return response(20 if elapsed in (30, 60) else 80)
        intervals = detect_pauses("101", beginning, 120, 60, "9", {"blue": "1", "red": "2"},
                                  fetch, 0)
        self.assertEqual(len(intervals), 1)
        self.assertEqual(round(intervals[0][1]), 60)

    def test_live_frame_becomes_historical_curve(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "test.sqlite3")
            first = normalize({"type": "frame", "payload": frame(600)})
            save(db, first, predict(first, BASELINE), BASELINE["kind"])
            self.assertEqual(len(lists(db)["live"]), 1)
            final = normalize(frame(1200, finished=True))
            save(db, final, predict(final, BASELINE), BASELINE["kind"])
            result = lists(db)
            self.assertEqual(len(result["live"]), 0)
            self.assertEqual(len(result["history"]), 1)
            curve = game(db, "101")
            self.assertEqual(len(curve["points"]), 2)
            self.assertNotIn(curve["points"][-1]["blue_probability"], (0.0, 1.0))

    def test_free_live_curve_can_be_archived_without_changing_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "free.sqlite3")
            live = normalize(frame(900))
            live["finished"] = True
            save(db, live, .63, BASELINE["kind"])
            archived = finalize(db, "101", "red", "https://example.com/game/101")
            self.assertTrue(archived["finished"])
            self.assertEqual(archived["frame"]["winner_id"], "2")
            self.assertEqual(archived["blue_probability"], .63)
            self.assertEqual(len(lists(db)["history"]), 1)

    def test_missing_model_field_rejected(self):
        raw = frame(600)
        del raw["blue"]["gold"]
        with self.assertRaises(ValueError):
            normalize(raw)

    def test_cito_board_requires_game_time(self):
        board = {"success": True, "frameTimestamp": "2026-07-14T19:23:50Z",
                 "lagSeconds": 193, "data": {
                     "gameId": "101", "state": "in_game",
                     "blueTeam": {"totalGold": 18000, "totalKills": 3, "towers": 1,
                                  "inhibitors": 0, "barons": 0, "dragons": ["cloud"]},
                     "redTeam": {"totalGold": 17000, "totalKills": 2, "towers": 0,
                                 "inhibitors": 0, "barons": 0, "dragons": []}}}
        live = {"matchId": "9", "currentGameId": "101",
                "blueTeam": {"id": 1, "name": "蓝队"},
                "redTeam": {"id": 2, "name": "红队"}}
        with self.assertRaisesRegex(ValueError, "game time"):
            normalize_cito(board, live)
        converted = normalize_cito(board, live, {"data": {"gameTime": 600}})
        self.assertEqual(converted["game_time"], 600)
        self.assertEqual(converted["blue"]["drakes"], 1)
        self.assertEqual(converted["lag_seconds"], 193)

    def test_cito_collector_creates_curve_and_reconciles_winner(self):
        with tempfile.TemporaryDirectory() as folder:
            db = str(Path(folder) / "history.sqlite3")
            live = {"matchId": "9", "currentGameId": "101",
                    "blueTeam": {"id": 1, "name": "蓝队"},
                    "redTeam": {"id": 2, "name": "红队"}}
            board = {"success": True, "data": {
                "gameId": "101", "gameTime": 600, "state": "in_game",
                "blueTeam": {"totalGold": 18000, "totalKills": 3, "towers": 1,
                             "inhibitors": 0, "barons": 0, "dragons": []},
                "redTeam": {"totalGold": 17000, "totalKills": 2, "towers": 0,
                            "inhibitors": 0, "barons": 0, "dragons": []}}}
            collector = Collector("fake-token", db, BASELINE, game_id=101, match_id=9)
            collector.games["101"] = live
            with patch("provider.cito_get", return_value=board):
                collector.run_once(now=1000)
            self.assertEqual(len(game(db, 101)["points"]), 1)
            first_received_at = game(db, 101)["latest"]["received_at"]
            with patch("provider.cito_get", return_value=board):
                collector.run_once(now=1030)
            self.assertEqual(game(db, 101)["latest"]["received_at"], first_received_at)
            prior_probability = game(db, 101)["points"][-1]["blue_probability"]
            with patch("reconcile.cito_get", return_value={"data": {"winnerTeamId": 1}}):
                reconciled = reconcile_game(101, "fake-token", db)
            self.assertTrue(reconciled["finished"])
            self.assertTrue(reconciled["outcome_reconciled"])
            self.assertEqual(len(lists(db)["history"]), 1)
            self.assertEqual(game(db, 101)["points"][-1]["blue_probability"], prior_probability)
            self.assertEqual(len(load_games(db)[0][2]), 1)


if __name__ == "__main__":
    unittest.main()
