"""Check imported training frames against the independently labeled crawl report."""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def audit(db_path, report_path):
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    if report["errors"]:
        raise ValueError("crawl report contains failed games")
    listed = report["games"]
    if len({item["game_id"] for item in listed}) != len(listed):
        raise ValueError("crawl report repeats a game")
    with sqlite3.connect(db_path) as db:
        records = db.execute("SELECT game_id, match_id, game_time, finished, winner_id, "
                             "frame_json FROM frames ORDER BY game_id, game_time").fetchall()
    by_game = {}
    for game_id, match_id, game_time, finished, winner_id, raw in records:
        frame = json.loads(raw)
        if (str(frame["game_id"]) != game_id or str(frame["match_id"]) != match_id
                or frame["game_time"] != game_time or frame["finished"] != bool(finished)
                or (None if frame["winner_id"] is None else str(frame["winner_id"])) != winner_id):
            raise ValueError("stored frame columns disagree with JSON for game " + game_id)
        by_game.setdefault(game_id, []).append(frame)
    listed_ids = {item["game_id"] for item in listed}
    missing = listed_ids - set(by_game)
    if missing:
        raise ValueError("database is missing labeled crawl games: " + ", ".join(sorted(missing)))
    sides = Counter()
    reconciled = []
    paused = []
    max_duration_error = 0
    for item in listed:
        frames = by_game[item["game_id"]]
        # A skipped crawl-report row can describe an older sampling interval;
        # the database may later be re-imported at 30 seconds. Audit the actual
        # timeline and labels below instead of treating that cached count as an
        # immutable property of the game.
        if len(frames) < 2:
            raise ValueError("not enough stored points for game " + item["game_id"])
        final = frames[-1]
        winning_id = item["team_ids"][item["winner_code"]]
        if (str(final["match_id"]) != item["match_id"] or not final["finished"]
                or str(final["winner_id"]) != winning_id
                or final.get("label_source") != item["label_source"]):
            raise ValueError("terminal label differs for game " + item["game_id"])
        side_ids = {side: str(final[side]["id"]) for side in ("blue", "red")}
        if set(side_ids.values()) != set(item["team_ids"].values()):
            raise ValueError("team IDs differ for game " + item["game_id"])
        sides[next(side for side, id_ in side_ids.items() if id_ == winning_id)] += 1
        duration_error = abs(final["game_time"] - item["duration_seconds"])
        max_duration_error = max(duration_error, max_duration_error)
        if duration_error > 20:
            raise ValueError("terminal game clock differs for game " + item["game_id"])
        if any(frame["finished"] or frame["winner_id"] is not None for frame in frames[:-1]):
            raise ValueError("winner leaked into observed frames for game " + item["game_id"])
        times = [frame["game_time"] for frame in frames]
        if times != sorted(set(times)) or times[0] <= 0:
            raise ValueError("invalid sampled game clock for game " + item["game_id"])
        if any({side: str(frame[side]["id"]) for side in ("blue", "red")} != side_ids
               for frame in frames):
            raise ValueError("team switched side during game " + item["game_id"])
        if final.get("outcome_reconciled"):
            reconciled.append(item["game_id"])
        if final.get("pause_adjusted_seconds"):
            paused.append((item["game_id"], final["pause_adjusted_seconds"]))
    audited_frames = sum(len(by_game[item["game_id"]]) for item in listed)
    return {"games": len(listed), "matches": len({x["match_id"] for x in listed}),
            "frames": audited_frames, "database_games": len(by_game),
            "database_frames": len(records), "winner_sides": dict(sides),
            "max_duration_error_seconds": max_duration_error,
            "paused_games": paused, "outcome_reconciled_games": reconciled}


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--report", default=str(root / "data" / "history_crawl_report.json"))
    args = parser.parse_args()
    print(json.dumps(audit(args.db, args.report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
