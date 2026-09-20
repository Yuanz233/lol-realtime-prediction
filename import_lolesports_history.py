"""Import a completed pro game from the public LoL Esports website frame feed.

This is an experimental website endpoint, not a documented Riot Developer API.
The per-game winner must be independently verified and supplied by the caller.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

from predictor import load_model, predict
from provider import normalize
from storage import save

FEED = "https://feed.lolesports.com/livestats/v1/window/"


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_window(game_id, start=None):
    if not str(game_id).isdigit():
        raise ValueError("game ID must be numeric")
    url = FEED + str(game_id)
    if start is not None:
        # This website feed accepts 10-second window boundaries. Arbitrary
        # seconds or fractional timestamps have returned HTTP 400 in testing.
        boundary = start.replace(second=(start.second // 10) * 10, microsecond=0)
        url += "?" + urllib.parse.urlencode({"startingTime": boundary.isoformat(timespec="milliseconds").replace("+00:00", "Z")})
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "lol-realtime-prediction/0.1"})
    payload = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionResetError,
                json.JSONDecodeError):
            if attempt == 3:
                raise
            time.sleep(0.5 * (attempt + 1))
    if str(payload.get("esportsGameId")) != str(game_id):
        raise ValueError("feed returned another game ID")
    if not isinstance(payload.get("frames"), list) or not payload["frames"]:
        raise ValueError("feed window has no frames")
    return payload


def teams(payload):
    metadata = payload.get("gameMetadata") or {}
    result = {}
    for side in ("blue", "red"):
        team = metadata.get(side + "TeamMetadata") or {}
        team_id = team.get("esportsTeamId")
        if team_id is None:
            raise ValueError("feed missing " + side + " team ID")
        result[side] = str(team_id)
    if result["blue"] == result["red"]:
        raise ValueError("feed team IDs are identical")
    return result


def validate_window(payload, game_id, match_id, team_ids):
    if str(payload.get("esportsGameId")) != game_id or str(payload.get("esportsMatchId")) != match_id:
        raise ValueError("feed changed game or match ID")
    if teams(payload) != team_ids:
        raise ValueError("feed changed blue/red team IDs")


def convert(payload, source, beginning, names, team_ids, winner_side=None, label_source=None,
            pause_intervals=(), outcome_reconciled=False, competition=None,
            mark_finished=False):
    instant = timestamp(source["rfc460Timestamp"])
    paused_seconds = sum(amount for resumed_at, amount in pause_intervals if instant >= resumed_at)
    game_time = round((instant - beginning).total_seconds() - paused_seconds)
    raw = {"game_id": payload["esportsGameId"], "match_id": payload["esportsMatchId"],
           "current_timestamp": game_time,
           "played_at": beginning.isoformat().replace("+00:00", "Z"),
           "source_timestamp": source["rfc460Timestamp"],
           "finished": winner_side is not None or mark_finished,
           "winner_id": team_ids[winner_side] if winner_side is not None else None}
    for side in ("blue", "red"):
        stats = source.get(side + "Team")
        if not isinstance(stats, dict):
            raise ValueError("feed frame missing " + side + " stats")
        raw[side] = {"id": team_ids[side], "name": names[side],
                     "gold": stats.get("totalGold"), "kills": stats.get("totalKills"),
                     "towers": stats.get("towers"),
                     "drakes": len(stats["dragons"]) if isinstance(stats.get("dragons"), list) else stats.get("dragons"),
                     "nashors": stats.get("barons"), "inhibitors": stats.get("inhibitors")}
    frame = normalize(raw)
    frame["source_name"] = "LoL Esports website feed (experimental)"
    frame["source_game_state"] = source.get("gameState")
    if competition:
        frame["competition"] = competition
    if pause_intervals:
        frame["pause_adjusted_seconds"] = round(paused_seconds)
    if outcome_reconciled:
        frame["outcome_reconciled"] = True
    if label_source is not None:
        frame["label_source"] = label_source
    return frame


def detect_pauses(game_id, beginning, duration, expected_duration_seconds,
                  match_id, team_ids, fetch, pause_seconds):
    """Measure recorded pauses from stale paused windows and first resumed frames."""
    def scan(spacing):
        intervals = []
        last_paused = None
        for seconds in range(spacing, int(duration), spacing):
            if pause_seconds:
                time.sleep(pause_seconds)
            payload = fetch(game_id, beginning + timedelta(seconds=seconds))
            validate_window(payload, str(game_id), match_id, team_ids)
            frames = payload["frames"]
            latest = frames[-1]
            if latest.get("gameState") == "paused":
                last_paused = timestamp(latest["rfc460Timestamp"])
            elif last_paused is not None:
                resumed = next((timestamp(f["rfc460Timestamp"]) for f in frames
                                if f.get("gameState") == "in_game"), None)
                if resumed is None:
                    raise ValueError("pause did not resume into a game frame")
                gap = (resumed - last_paused).total_seconds()
                if gap < 10 or gap > 900:
                    raise ValueError("invalid recorded pause gap: %.1fs" % gap)
                intervals.append((resumed, gap))
                last_paused = None
                if abs(duration - sum(amount for _, amount in intervals) - expected_duration_seconds) <= 20:
                    return intervals
        if last_paused is not None:
            raise ValueError("history ends while paused")
        return intervals

    coarse = scan(30)
    # A short pause can fall between 30-second probes. Scan 10-second windows
    # only when coarse detection cannot explain the independent game duration.
    if abs(duration - sum(amount for _, amount in coarse) - expected_duration_seconds) <= 20:
        return coarse
    return scan(10)


def collect(game_id, names, winner_side, label_source, step_seconds=30, pause_seconds=0.25,
            max_minutes=90, fetch=fetch_window, first_payload=None,
            expected_match_id=None, expected_team_ids=None, expected_duration_seconds=None,
            competition=None, archive_unlabeled=False):
    first = first_payload if first_payload is not None else fetch(game_id)
    if str(first.get("esportsGameId")) != str(game_id):
        raise ValueError("feed returned another game ID")
    match_id = str(first.get("esportsMatchId"))
    if match_id == "None":
        raise ValueError("feed missing match ID")
    if expected_match_id is not None and match_id != str(expected_match_id):
        raise ValueError("feed match ID disagrees with verified schedule")
    team_ids = teams(first)
    if expected_team_ids is not None and set(team_ids.values()) != set(map(str, expected_team_ids)):
        raise ValueError("feed team IDs disagree with verified schedule")
    beginning = timestamp(first["frames"][0]["rfc460Timestamp"])
    # The website endpoint rejects timestamps far beyond an archived game.
    # Query just past the longest expected pro game; completed windows return
    # the terminal tail instead of requiring a separate history endpoint.
    tail = None
    tail_error = None
    for minutes in range(max_minutes, 9, -10):
        try:
            candidate = fetch(game_id, beginning + timedelta(minutes=minutes))
            validate_window(candidate, str(game_id), match_id, team_ids)
            tail = candidate
            if (candidate["frames"][-1].get("gameState") == "finished"
                    or expected_duration_seconds is not None):
                break
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            tail_error = exc
    if tail is None:
        if tail_error is not None:
            raise tail_error
        raise ValueError("could not locate a terminal feed window")
    validate_window(tail, str(game_id), match_id, team_ids)
    final = tail["frames"][-1]
    ending = timestamp(final["rfc460Timestamp"])
    duration = (ending - beginning).total_seconds()
    if duration <= 0 or duration > max_minutes * 60:
        raise ValueError("invalid game duration from feed: %.0f seconds" % duration)
    pause_intervals = ()
    if expected_duration_seconds is not None and duration - expected_duration_seconds > 20:
        pause_intervals = detect_pauses(game_id, beginning, duration, expected_duration_seconds,
                                        match_id, team_ids,
                                        fetch, pause_seconds)
    corrected_duration = duration - sum(amount for _, amount in pause_intervals)
    if expected_duration_seconds is not None and abs(corrected_duration - expected_duration_seconds) > 20:
        raise ValueError("feed duration %.0fs (%.0fs after measured pauses) differs from verified per-game duration %ds" %
                         (duration, corrected_duration, expected_duration_seconds))
    if final.get("gameState") != "finished" and expected_duration_seconds is None:
        raise ValueError("feed has no terminal state; independently verified game duration is required")
    if final.get("gameState") not in ("finished", "in_game"):
        raise ValueError("feed tail is not a finished or last in-game frame")
    if final["blueTeam"].get("totalGold", 0) <= 0 or final["redTeam"].get("totalGold", 0) <= 0:
        raise ValueError("finished feed frame has no team gold")
    points = []
    for seconds in range(step_seconds, int(duration), step_seconds):
        if pause_seconds:
            time.sleep(pause_seconds)
        target = beginning + timedelta(seconds=seconds)
        payload = fetch(game_id, target)
        validate_window(payload, str(game_id), match_id, team_ids)
        candidates = [f for f in payload["frames"] if timestamp(f["rfc460Timestamp"]) >= target]
        if not candidates:
            # The target can be late in a 10-second bucket while the last
            # actual frame is a fraction of a second earlier. Read the next
            # bucket rather than pretending the previous frame is current.
            payload = fetch(game_id, target + timedelta(seconds=10))
            validate_window(payload, str(game_id), match_id, team_ids)
            candidates = [f for f in payload["frames"] if timestamp(f["rfc460Timestamp"]) >= target]
            if not candidates:
                if payload["frames"][-1].get("gameState") == "paused":
                    continue
                raise ValueError("missing frame near %s in consecutive windows" % target.isoformat())
        source = candidates[0]
        if (timestamp(source["rfc460Timestamp"]) - target).total_seconds() > 20:
            raise ValueError("feed has a gap near %s" % target.isoformat())
        if source.get("gameState") == "finished":
            break
        if source.get("gameState") == "paused":
            continue
        frame = convert(payload, source, beginning, names, team_ids,
                        pause_intervals=pause_intervals, competition=competition)
        if frame["blue"]["gold"] > 0 and frame["red"]["gold"] > 0:
            points.append(frame)
    points.append(convert(tail, final, beginning, names, team_ids, winner_side, label_source,
                          pause_intervals=pause_intervals,
                          outcome_reconciled=final.get("gameState") != "finished",
                          competition=competition,
                          mark_finished=archive_unlabeled))
    if len(points) < 2:
        raise ValueError("not enough nonzero historical frames")
    return points


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id", help="numeric LoL Esports single-game ID")
    parser.add_argument("--blue-name", required=True)
    parser.add_argument("--red-name", required=True)
    parser.add_argument("--winner-side", choices=("blue", "red"), required=True,
                        help="verified per-game winner's side, never inferred from gold")
    parser.add_argument("--label-source", required=True, help="URL proving this single game's winner")
    parser.add_argument("--step-seconds", type=int, default=30)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--model", default=str(root / "models" / "live.json"))
    parser.add_argument("--league", default="unknown")
    parser.add_argument("--tournament", default="未知赛事")
    parser.add_argument("--stage", default="未知阶段")
    args = parser.parse_args()
    if args.step_seconds < 30 or args.pause_seconds < 0:
        parser.error("step-seconds must be >=30 and pause-seconds must be >=0")
    if not args.label_source.startswith("https://"):
        parser.error("label-source must be an HTTPS URL")
    names = {"blue": args.blue_name, "red": args.red_name}
    competition = {"league_slug": args.league.lower(), "league_name": args.league.upper(),
                   "tournament_name": args.tournament, "stage_name": args.stage}
    points = collect(args.game_id, names, args.winner_side, args.label_source,
                     args.step_seconds, args.pause_seconds, competition=competition)
    model = load_model(args.model)
    for frame in points:
        save(args.db, frame, predict(frame, model), model["kind"])
    print("imported %d real frames for game %s; winner %s (%s); model %s" %
          (len(points), args.game_id, names[args.winner_side], args.winner_side, model["kind"]))


if __name__ == "__main__":
    main()
