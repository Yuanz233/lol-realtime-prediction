"""Recompute stored curve probabilities with a validated local model."""
import argparse
import json
import sqlite3
from pathlib import Path

from predictor import load_model, predict


def apply_model(db_path, model_path):
    model = load_model(model_path)
    if model["kind"] == "experimental_baseline":
        raise ValueError("refusing to activate the experimental baseline")
    with sqlite3.connect(db_path, timeout=10) as db:
        rows = db.execute("SELECT game_id, game_time, frame_json FROM frames").fetchall()
        updates = []
        for game_id, game_time, raw in rows:
            frame = json.loads(raw)
            updates.append((predict(frame, model), model["kind"], game_id, game_time))
        db.executemany("UPDATE frames SET blue_probability=?, model_kind=? "
                       "WHERE game_id=? AND game_time=?", updates)
    return len(rows), model["kind"]


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    count, kind = apply_model(args.db, args.model)
    print("updated %d stored frames with %s" % (count, kind))


if __name__ == "__main__":
    main()
