"""Small, dependency-free CPU probability model for normalized LoL frames."""
import json
import math
from pathlib import Path

FEATURES = ("time_min", "gold_diff_k", "kills_diff", "towers_diff",
            "drakes_diff", "nashors_diff", "inhibitors_diff")

# A transparent engineering baseline, NOT fitted or calibrated on match data.
BASELINE = {"kind": "experimental_baseline", "intercept": 0.0,
            "weights": [0.0, 0.42, 0.13, 0.35, 0.18, 0.55, 0.75]}


def features(frame):
    blue, red = frame["blue"], frame["red"]
    return [frame["game_time"] / 60.0,
            (blue["gold"] - red["gold"]) / 1000.0,
            blue["kills"] - red["kills"],
            blue["towers"] - red["towers"],
            blue["drakes"] - red["drakes"],
            blue["nashors"] - red["nashors"],
            blue["inhibitors"] - red["inhibitors"]]


def load_model(path):
    if not Path(path).exists():
        return BASELINE.copy()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("features") != list(FEATURES) or len(data.get("weights", [])) != len(FEATURES):
        raise ValueError("model features do not match serving schema")
    return data


def predict(frame, model):
    values = features(frame)
    if "means" in model and "scales" in model:
        values = [(x - m) / s for x, m, s in zip(values, model["means"], model["scales"])]
    score = model["intercept"] + sum(w * x for w, x in zip(model["weights"], values))
    score = max(-30.0, min(30.0, score))
    return 1.0 / (1.0 + math.exp(-score))
