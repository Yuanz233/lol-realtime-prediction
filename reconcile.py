"""Mark a locally captured Cito game complete when its explicit winner is available."""
import argparse
import os
import urllib.parse
from pathlib import Path

from provider import cito_get, reconcile_winner
from storage import game, save


def reconcile_game(game_id, token, db_path):
    stored = game(db_path, game_id)
    if not stored:
        raise ValueError("game was not captured locally; Cito postgame alone has no full prediction curve")
    frame = stored["latest"]["frame"]
    if frame["finished"] and frame["winner_id"] is not None:
        return frame
    metadata = cito_get("/lol/games/" + urllib.parse.quote(str(game_id), safe=""), token)
    winner = reconcile_winner(metadata, frame)
    if winner is None:
        raise ValueError("postgame winner is missing or cannot be mapped to the two sides")
    frame["finished"] = True
    frame["winner_id"] = winner
    frame["outcome_reconciled"] = True
    save(db_path, frame, stored["latest"]["blue_probability"],
         stored["latest"]["model_kind"])
    return frame


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-id", required=True)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    args = parser.parse_args()
    token = os.getenv("CITO_API_KEY", "").strip()
    if not token:
        raise SystemExit("set CITO_API_KEY on the server first")
    frame = reconcile_game(args.game_id, token, args.db)
    print("reconciled", frame["game_id"], "winner", frame["winner_id"])


if __name__ == "__main__":
    main()
