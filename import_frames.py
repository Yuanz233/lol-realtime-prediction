"""Import canonical project frames from JSONL for offline QA and training."""
import argparse
import json
from pathlib import Path

from predictor import load_model, predict
from provider import normalize
from storage import save


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", help="one raw frame object per line")
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--model", default=str(root / "models" / "live.json"))
    args = parser.parse_args()
    model = load_model(args.model)
    count = 0
    with open(args.jsonl, encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                frame = normalize(json.loads(line))
                save(args.db, frame, predict(frame, model), model["kind"])
                count += 1
            except Exception as exc:
                raise ValueError("line %d: %s" % (number, exc)) from exc
    print("imported %d frames" % count)


if __name__ == "__main__":
    main()
