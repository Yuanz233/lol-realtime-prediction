"""Measure the real publication behavior of LoL Esports window/details feeds."""
import argparse
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE = "https://feed.lolesports.com/livestats/v1/"


def request(kind, game_id, delay_seconds, timeout=10):
    now = datetime.now(timezone.utc) - timedelta(seconds=delay_seconds)
    boundary = now.replace(second=(now.second // 10) * 10, microsecond=0)
    value = boundary.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    url = BASE + kind + "/" + game_id + "?" + urllib.parse.urlencode({"startingTime": value})
    started = time.time()
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "lol-realtime-prediction/0.1-live-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    received = time.time()
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            pass
    frames = payload.get("frames", []) if isinstance(payload, dict) else []
    timestamps = [item.get("rfc460Timestamp") for item in frames if item.get("rfc460Timestamp")]
    latest = frames[-1] if frames else {}
    required = None
    if kind == "window" and frames:
        required = all((latest.get(side + "Team") or {}).get(field) is not None
                       for side in ("blue", "red")
                       for field in ("totalGold", "totalKills", "towers", "dragons",
                                     "barons", "inhibitors"))
    return {
        "kind": kind, "requested_at": started, "received_at": received,
        "elapsed_seconds": round(received - started, 6), "status": status,
        "query_starting_time": value, "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest() if body else None,
        "frames": len(frames), "first_source_timestamp": timestamps[0] if timestamps else None,
        "latest_source_timestamp": timestamps[-1] if timestamps else None,
        "game_states": sorted({str(item.get("gameState")) for item in frames}),
        "required_team_fields": required,
    }, body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--window-delay", type=float, default=40.0,
                        help="query this many seconds behind wall time")
    parser.add_argument("--details-delay", type=float, default=510.0,
                        help="query this many seconds behind wall time")
    parser.add_argument("--output", default="data/live_probe")
    args = parser.parse_args()
    if not args.game_id.isdigit() or args.seconds < 10 or args.interval < 0.5:
        raise SystemExit("invalid probe arguments")
    output = Path(args.output)
    raw_dir = output / "changed_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    samples_path = output / "samples.jsonl"
    previous = {}
    started_monotonic = time.monotonic()
    deadline = started_monotonic + args.seconds
    index = 0
    delays = {"window": args.window_delay, "details": args.details_delay}
    pending = []
    with samples_path.open("a", encoding="utf-8") as stream, ThreadPoolExecutor(max_workers=6) as pool:
        def write_completed(entries, wait=False):
            remaining_entries = []
            for kind, sample_index, future in entries:
                if not wait and not future.done():
                    remaining_entries.append((kind, sample_index, future))
                    continue
                try:
                    summary, body = future.result()
                    summary["sample"] = sample_index
                    if body and previous.get(kind) != summary["sha256"]:
                        path = raw_dir / ("%s_%04d.json" % (kind, sample_index))
                        path.write_bytes(body)
                        summary["changed_response_file"] = str(path)
                        previous[kind] = summary["sha256"]
                except Exception as exc:
                    summary = {"kind": kind, "sample": sample_index,
                               "requested_at": time.time(), "error": "%s: %s" %
                               (type(exc).__name__, str(exc))}
                stream.write(json.dumps(summary, ensure_ascii=False) + "\n")
                stream.flush()
            return remaining_entries

        while time.monotonic() < deadline:
            tick_at = started_monotonic + index * args.interval
            remaining = tick_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            for kind in ("window", "details"):
                pending.append((kind, index,
                                pool.submit(request, kind, args.game_id, delays[kind])))
            pending = write_completed(pending)
            index += 1
        write_completed(pending, wait=True)
    (output / "completed.json").write_text(json.dumps({
        "game_id": args.game_id, "samples": index, "requested_feeds": index * 2,
        "interval_seconds": args.interval, "duration_target_seconds": args.seconds,
        "completed_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
