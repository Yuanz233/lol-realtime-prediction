"""Backfill league/tournament/stage metadata for games already stored in SQLite."""
import argparse
from pathlib import Path

from schedule_index import event_competition, league_matches, read_schedule
from storage import lists, update_match_competition


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--leagues", default="lpl", help="comma-separated LoL Esports slugs")
    parser.add_argument("--schedule-html", help="one cached page; valid only with one league")
    args = parser.parse_args()
    leagues = [value.strip().lower() for value in args.leagues.split(",") if value.strip()]
    if not leagues or (args.schedule_html and len(leagues) != 1):
        parser.error("choose at least one league; cached HTML supports exactly one")
    stored = lists(args.db, limit=5000)
    match_ids = {str(item["match_id"]) for kind in ("live", "history", "stale")
                 for item in stored[kind]}
    updated_matches = updated_frames = 0
    for league in leagues:
        html = Path(args.schedule_html).read_text(encoding="utf-8") if args.schedule_html else read_schedule(league)
        events = {}
        for state in ("completed", "inProgress", "unstarted"):
            try:
                events.update(league_matches(html, league, (state,)))
            except ValueError as exc:
                if "no matching" not in str(exc):
                    raise
        for match_id in sorted(match_ids & set(events)):
            changed = update_match_competition(args.db, match_id, event_competition(events[match_id]))
            if changed:
                updated_matches += 1
                updated_frames += changed
    print("updated %d matches and %d frames" % (updated_matches, updated_frames))


if __name__ == "__main__":
    main()
