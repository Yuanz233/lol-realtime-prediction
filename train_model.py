"""Fit regularized logistic regression from labeled, completed games in SQLite."""
import argparse
import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from predictor import FEATURES, features


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def metrics(rows, model):
    losses, briers = [], []
    for x, y in rows:
        z = model["intercept"] + sum(w * (v - m) / s for w, v, m, s in
            zip(model["weights"], x, model["means"], model["scales"]))
        p = max(1e-8, min(1 - 1e-8, sigmoid(z)))
        losses.append(-y * math.log(p) - (1 - y) * math.log(1 - p))
        briers.append((p - y) ** 2)
    return {"frames": len(rows), "log_loss": round(sum(losses) / len(rows), 4),
            "brier": round(sum(briers) / len(rows), 4)}


def fit(rows):
    columns = list(zip(*(x for x, _ in rows)))
    means = [sum(c) / len(c) for c in columns]
    scales = [max(1.0, math.sqrt(sum((v - m) ** 2 for v in c) / len(c))) for c, m in zip(columns, means)]
    data = [([(v - m) / s for v, m, s in zip(x, means, scales)], y) for x, y in rows]
    weights = [0.0] * len(FEATURES)
    intercept = 0.0
    regularization = 0.01
    for _ in range(1200):
        grad = [regularization * w for w in weights]
        grad_intercept = 0.0
        for x, y in data:
            delta = sigmoid(intercept + sum(w * v for w, v in zip(weights, x))) - y
            grad_intercept += delta
            for j, value in enumerate(x):
                grad[j] += delta * value / len(data)
        intercept -= 0.12 * grad_intercept / len(data)
        weights = [w - 0.12 * g for w, g in zip(weights, grad)]
    return {"kind": "trained_logistic_uncalibrated", "features": list(FEATURES),
            "intercept": intercept, "weights": weights, "means": means, "scales": scales}


def load_games(db_path):
    db = sqlite3.connect(db_path)
    records = db.execute("SELECT game_id, game_time, received_at, finished, winner_id, frame_json FROM frames ORDER BY game_id, game_time").fetchall()
    by_game = defaultdict(list)
    for record in records:
        by_game[record[0]].append(record)
    games = []
    for game_id, points in by_game.items():
        last = points[-1]
        if not last[3] or last[4] is None:
            continue
        final_frame = json.loads(last[5])
        y = int(str(last[4]) == str(final_frame["blue"]["id"]))
        # One snapshot per 30-second bucket. Keep the last observed snapshot
        # when only its outcome was reconciled later; exclude true terminal frames.
        buckets = {}
        observed = points if final_frame.get("outcome_reconciled") else points[:-1]
        for item in observed:
            buckets.setdefault(item[1] // 30, json.loads(item[5]))
        if buckets:
            played_at = final_frame.get("played_at")
            try:
                order = datetime.fromisoformat(played_at.replace("Z", "+00:00")).timestamp() if played_at else points[0][2]
            except (ValueError, AttributeError):
                order = points[0][2]
            games.append((order, game_id, [(features(frame), y) for frame in buckets.values()],
                          str(final_frame["match_id"])))
    return sorted(games)


def split_by_match(games):
    """Keep every game in one BO series on the same side of the time split."""
    grouped = defaultdict(list)
    for item in games:
        grouped[item[3]].append(item)
    chronological = sorted((min(x[0] for x in items), match_id, items)
                           for match_id, items in grouped.items())
    if len(chronological) < 5:
        raise ValueError("need >=5 distinct completed matches for a series-level holdout")
    test_count = max(1, math.ceil(len(chronological) * .2))
    train = [game for _, _, items in chronological[:-test_count] for game in items]
    test = [game for _, _, items in chronological[-test_count:] for game in items]
    return train, test, len(chronological) - test_count, test_count


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--output", default=str(root / "models" / "live.json"))
    args = parser.parse_args()
    games = load_games(args.db)
    if len(games) < 20:
        raise SystemExit("need >=20 completed labeled games; found %d" % len(games))
    try:
        train_games, test_games, train_matches, test_matches = split_by_match(games)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    train = [row for _, _, rows, _ in train_games for row in rows]
    test = [row for _, _, rows, _ in test_games for row in rows]
    evaluation_model = fit(train)
    holdout = {"train_games": len(train_games), "test_games": len(test_games),
                        "train_matches": train_matches, "test_matches": test_matches,
                        "train_frames": len(train), "test_frames": len(test),
                        "test_metrics": metrics(test, evaluation_model)}
    # Keep the chronological holdout result as an honest evaluation, then fit
    # the deployed coefficients on every labeled game after evaluation.
    all_rows = [row for _, _, rows, _ in games for row in rows]
    model = fit(all_rows)
    model["holdout"] = holdout
    model["training"] = {"games": len(games), "matches": train_matches + test_matches,
                         "frames": len(all_rows), "sample_interval_seconds": 30,
                         "refit_on_all_labeled_data": True}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(model, indent=2), encoding="utf-8")
    print(json.dumps(model["holdout"], indent=2))
    print("saved", destination)


if __name__ == "__main__":
    main()
