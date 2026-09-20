"""Archive a collected free-feed game after independently verifying its winner."""
import argparse
from pathlib import Path

from storage import game, save


def finalize(db_path, game_id, winner_side, label_source):
    if winner_side not in ("blue", "red") or not label_source.startswith("https://"):
        raise ValueError("winner side and HTTPS label source are required")
    stored = game(db_path, game_id)
    if stored is None:
        raise ValueError("game not found: %s" % game_id)
    latest = stored["latest"]
    frame = latest["frame"]
    if not frame.get("finished"):
        raise ValueError("game is still live; only a finished game can be labeled")
    winner = str(frame[winner_side]["id"])
    if frame.get("finished") and frame.get("winner_id") is not None and str(frame.get("winner_id")) != winner:
        raise ValueError("stored winner conflicts with requested winner")
    frame["finished"] = True
    frame["winner_id"] = winner
    frame["outcome_reconciled"] = True
    frame["label_source"] = label_source
    save(db_path, frame, latest["blue_probability"], latest["model_kind"])
    return game(db_path, game_id)["latest"]


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id")
    parser.add_argument("--winner-side", choices=("blue", "red"), required=True)
    parser.add_argument("--label-source", required=True, help="HTTPS page proving the single-game winner")
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    args = parser.parse_args()
    if not args.label_source.startswith("https://"):
        parser.error("label-source must be an HTTPS URL")
    try:
        latest = finalize(args.db, args.game_id, args.winner_side, args.label_source)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    frame = latest["frame"]
    print("archived game %s; winner %s; preserved probability %.6f" %
          (args.game_id, frame[args.winner_side]["name"], latest["blue_probability"]))


if __name__ == "__main__":
    main()
