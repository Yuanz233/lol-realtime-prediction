"""Summarize samples written by probe_live_feed.py."""
import argparse
import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path


def seconds_between(left, right):
    return (datetime.fromisoformat(right.replace("Z", "+00:00")) -
            datetime.fromisoformat(left.replace("Z", "+00:00"))).total_seconds()


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def summarize(rows, kind):
    items = sorted((item for item in rows if item.get("kind") == kind),
                   key=lambda item: item.get("requested_at", 0))
    valid = [item for item in items if item.get("status") == 200 and item.get("frames", 0)]
    changes = []
    prior = None
    for item in valid:
        if item.get("sha256") != prior:
            changes.append(item)
            prior = item.get("sha256")
    change_intervals = [changes[i]["received_at"] - changes[i - 1]["received_at"]
                        for i in range(1, len(changes))]
    request_intervals = [items[i]["requested_at"] - items[i - 1]["requested_at"]
                         for i in range(1, len(items))
                         if items[i].get("requested_at") and items[i - 1].get("requested_at")]
    lags = []
    for item in valid:
        stamp = item.get("latest_source_timestamp")
        if stamp:
            source = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            lags.append(item["received_at"] - source)
    source_stamps = set()
    for item in changes:
        filename = item.get("changed_response_file")
        if not filename or not Path(filename).is_file():
            continue
        payload = json.loads(Path(filename).read_text())
        for frame in payload.get("frames", []):
            if frame.get("rfc460Timestamp"):
                source_stamps.add(frame["rfc460Timestamp"])
    ordered_stamps = sorted(
        source_stamps,
        key=lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")),
    )
    source_intervals = [seconds_between(ordered_stamps[i - 1], ordered_stamps[i])
                        for i in range(1, len(ordered_stamps))]
    return {
        "requests": len(items),
        "http_statuses": dict(Counter(str(item.get("status", "error")) for item in items)),
        "errors": [item["error"] for item in items if item.get("error")],
        "successful_nonempty": len(valid),
        "unique_response_bodies": len({item.get("sha256") for item in valid}),
        "observed_seconds": round(items[-1]["received_at"] - items[0]["requested_at"], 3)
                            if len(items) > 1 else 0,
        "request_interval_seconds": {
            "median": round(statistics.median(request_intervals), 6)
                      if request_intervals else None,
            "p90": percentile(request_intervals, 0.9),
            "min": round(min(request_intervals), 6) if request_intervals else None,
            "max": round(max(request_intervals), 6) if request_intervals else None,
        },
        "response_change_interval_seconds": {
            "median": round(statistics.median(change_intervals), 3) if change_intervals else None,
            "p90": percentile(change_intervals, 0.9),
            "min": round(min(change_intervals), 3) if change_intervals else None,
            "max": round(max(change_intervals), 3) if change_intervals else None,
        },
        "source_frame_interval_seconds": {
            "median": round(statistics.median(source_intervals), 3) if source_intervals else None,
            "p90": percentile(source_intervals, 0.9),
            "min": round(min(source_intervals), 3) if source_intervals else None,
            "max": round(max(source_intervals), 3) if source_intervals else None,
            "unique_timestamps": len(ordered_stamps),
        },
        "source_to_receive_lag_seconds": {
            "median": round(statistics.median(lags), 3) if lags else None,
            "p90": percentile(lags, 0.9),
            "min": round(min(lags), 3) if lags else None,
            "max": round(max(lags), 3) if lags else None,
        },
        "missing_required_team_fields": sum(item.get("required_team_fields") is False
                                            for item in valid),
        "game_states": dict(Counter(state for item in valid
                                    for state in item.get("game_states", []))),
        "first_source_timestamp": valid[0].get("first_source_timestamp") if valid else None,
        "last_source_timestamp": valid[-1].get("latest_source_timestamp") if valid else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder")
    args = parser.parse_args()
    folder = Path(args.folder)
    rows = [json.loads(line) for line in (folder / "samples.jsonl").read_text().splitlines()]
    report = {kind: summarize(rows, kind) for kind in ("window", "details")}
    report["sample_rows"] = len(rows)
    report["generated_at"] = datetime.now().astimezone().isoformat()
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
