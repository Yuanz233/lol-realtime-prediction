"""Continuously backfill recent completed games as unlabeled historical curves."""
import argparse
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from import_lolesports_history import collect, fetch_window, teams
from predictor import load_model, predict
from schedule_index import event_competition, league_matches, read_schedule
from storage import game, save

LOG = logging.getLogger(__name__)


def recent_completed(leagues, days=14, now=None, schedule_fetch=read_schedule):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(1, int(days)))
    result = []
    for league_slug in leagues:
        html = schedule_fetch(league_slug)
        try:
            # A series can remain unstarted/inProgress in the embedded schedule
            # while one or more of its individual games are already completed.
            events = league_matches(html, league_slug,
                                    ("completed", "inProgress", "unstarted"))
        except ValueError as exc:
            if "no matching" in str(exc):
                continue
            raise
        for event in events.values():
            start_text = event.get("startTime")
            if not start_text:
                continue
            start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            if start < cutoff:
                continue
            names = {}
            for team in event.get("matchTeams", []):
                team_id = str(team.get("id", "")).rsplit(":", 1)[-1]
                if team_id.isdigit():
                    names[team_id] = str(team.get("code") or team.get("name") or team_id)
            contest = event_competition(event)
            for source in sorted((event.get("match") or {}).get("games", []),
                                 key=lambda value: value.get("number", 0)):
                if source.get("state") == "completed":
                    result.append({"league": league_slug, "match_id": str(event["id"]),
                        "game_id": str(source["id"]), "number": source.get("number"),
                        "names": names, "competition": contest})
    return sorted(result, key=lambda item: (
        item["competition"].get("event_start") or "", item["match_id"], item["number"] or 0))


def import_unlabeled(item, db_path, model, step_seconds=30, pause_seconds=.2,
                     fetch=fetch_window):
    stored = game(db_path, item["game_id"])
    if stored is not None and stored["latest"]["finished"]:
        return {"status": "skipped", "points": len(stored["points"])}
    first = fetch(item["game_id"])
    if str(first.get("esportsMatchId")) != item["match_id"]:
        raise ValueError("feed match ID disagrees with schedule")
    side_ids = teams(first)
    if set(side_ids.values()) != set(item["names"]):
        raise ValueError("feed team IDs disagree with schedule")
    names = {side: item["names"][team_id] for side, team_id in side_ids.items()}
    points = collect(item["game_id"], names, None, None,
                     step_seconds=step_seconds, pause_seconds=pause_seconds,
                     first_payload=first, expected_match_id=item["match_id"],
                     expected_team_ids=item["names"], competition=item["competition"],
                     archive_unlabeled=True)
    if not points[-1]["finished"] or points[-1]["winner_id"] is not None:
        raise ValueError("unlabeled history did not end in a pending-result frame")
    for frame in points:
        save(db_path, frame, predict(frame, model), model["kind"])
    return {"status": "imported", "points": len(points)}


class CompletedHistorySync:
    def __init__(self, db_path, model, leagues, interval_seconds=21600, days=14,
                 workers=2, max_new_games=20):
        self.db_path, self.model = db_path, model
        self.leagues = list(leagues)
        self.interval_seconds = max(900, int(interval_seconds))
        self.days = max(1, int(days))
        self.workers = max(1, min(4, int(workers)))
        self.max_new_games = max(1, int(max_new_games))
        self.running = False
        self.last_started_at = None
        self.last_finished_at = None
        self.last_error = None
        self.last_result = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="completed-history-sync").start()

    def _run(self):
        while True:
            self.run_once()
            time.sleep(self.interval_seconds)

    def run_once(self):
        self.running = True
        self.last_started_at = time.time()
        imported = skipped = 0
        errors = []
        try:
            candidates = recent_completed(self.leagues, self.days)
            pending = []
            for item in reversed(candidates):
                stored = game(self.db_path, item["game_id"])
                if stored is not None and stored["latest"]["finished"]:
                    skipped += 1
                elif len(pending) < self.max_new_games:
                    pending.append(item)
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(import_unlabeled, item, self.db_path, self.model): item
                           for item in pending}
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        outcome = future.result()
                        imported += outcome["status"] == "imported"
                        skipped += outcome["status"] == "skipped"
                    except Exception as exc:
                        errors.append("%s: %s" % (item["game_id"], str(exc)))
            self.last_result = {"candidates": len(candidates), "imported": imported,
                                "skipped": skipped, "errors": errors}
            self.last_error = "; ".join(errors[:3]) if errors else None
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, str(exc))
            self.last_result = {"candidates": 0, "imported": imported,
                                "skipped": skipped, "errors": [self.last_error]}
            LOG.warning("completed history sync failed: %s", self.last_error)
        finally:
            self.running = False
            self.last_finished_at = time.time()
        return self.last_result

    def status(self):
        return {"running": self.running, "last_started_at": self.last_started_at,
                "last_finished_at": self.last_finished_at, "last_error": self.last_error,
                "last_result": self.last_result, "days": self.days,
                "interval_seconds": self.interval_seconds}


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--model", default=str(root / "models" / "live.json"))
    parser.add_argument("--leagues", default="lpl,lck,lec")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-new-games", type=int, default=20)
    args = parser.parse_args()
    model = load_model(args.model)
    leagues = [value.strip().lower() for value in args.leagues.split(",") if value.strip()]
    sync = CompletedHistorySync(args.db, model, leagues, days=args.days,
                                workers=args.workers, max_new_games=args.max_new_games)
    result = sync.run_once()
    print(result)
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
