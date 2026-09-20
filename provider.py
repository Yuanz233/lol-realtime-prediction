"""Cito REST polling and conversion into the local single-game schema."""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from predictor import predict
from storage import game, save

LOG = logging.getLogger(__name__)
API = "https://api.citoapi.com/api/v1"


def normalize(raw):
    """Import the project's documented canonical frame format."""
    if not isinstance(raw, dict):
        raise ValueError("frame must be an object")
    if "blue" not in raw and isinstance(raw.get("payload"), dict):
        raw = raw["payload"]
    game_id = raw.get("game_id") or raw.get("id")
    match_id = raw.get("match_id")
    game_time = raw.get("current_timestamp", raw.get("game_time"))
    if game_id is None or match_id is None or game_time is None:
        raise ValueError("missing game_id, match_id or current_timestamp")
    sides = {}
    for side in ("blue", "red"):
        source = raw.get(side)
        if not isinstance(source, dict) or source.get("id") is None:
            raise ValueError("missing team " + side)
        fields = {"id": str(source["id"]), "name": source.get("name") or source.get("acronym") or side}
        for key in ("gold", "kills", "towers", "drakes", "nashors", "inhibitors"):
            if source.get(key) is None:
                raise ValueError("missing " + side + "." + key)
            fields[key] = int(source[key])
            if fields[key] < 0:
                raise ValueError("negative " + side + "." + key)
        sides[side] = fields
    winner = raw.get("winner_id")
    if winner is not None and str(winner) not in (sides["blue"]["id"], sides["red"]["id"]):
        raise ValueError("winner_id does not match either team")
    if int(game_time) < 0:
        raise ValueError("negative current_timestamp")
    result = {"game_id": str(game_id), "match_id": str(match_id), "game_time": int(game_time),
              "finished": bool(raw.get("finished", False)), "winner_id": str(winner) if winner is not None else None,
              **sides}
    for key in ("played_at", "source_timestamp", "lag_seconds"):
        if raw.get(key) is not None:
            result[key] = raw[key]
    return result


def first(mapping, *keys):
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


def seconds(value):
    if value is None:
        return None
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            return int(parts[0]) * 60 + int(parts[1])
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def count(value):
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return first(value, "count", "total")
    return value


def normalize_cito(board, live, timing=None):
    """Reject a live board missing required model inputs or actual game time."""
    if not isinstance(board, dict) or board.get("success") is False:
        raise ValueError("Cito board request was not successful")
    data = board.get("data")
    if not isinstance(data, dict):
        raise ValueError("Cito board has no data object")
    live = live if isinstance(live, dict) else {}
    timing = timing if isinstance(timing, dict) else {}
    timing_data = timing.get("data") if isinstance(timing.get("data"), dict) else timing
    game_id = first(data, "gameId", "game_id") or first(live, "currentGameId", "gameId")
    match_id = first(data, "matchId", "match_id") or first(live, "matchId", "match_id")
    if game_id is None or match_id is None:
        raise ValueError("Cito gameId or matchId missing")
    expected_game = first(live, "currentGameId", "gameId")
    if expected_game is not None and str(game_id) != str(expected_game):
        raise ValueError("Cito board gameId differs from discovery")
    game_time = seconds(first(data, "gameTime", "gameTimeSeconds", "game_time", "currentTimestamp"))
    if game_time is None:
        game_time = seconds(first(timing_data, "gameTime", "gameTimeSeconds", "game_time", "currentTimestamp"))
    if game_time is None:
        raise ValueError("Cito board and visual-state have no game time")
    teams = {}
    for side in ("blue", "red"):
        team = data.get(side + "Team")
        meta = live.get(side + "Team")
        if not isinstance(team, dict):
            raise ValueError("Cito board missing " + side + "Team")
        meta = meta if isinstance(meta, dict) else {}
        team_id = first(team, "id", "teamId") or first(meta, "id", "teamId")
        # Stable internal side IDs when Cito's documented board omits team IDs.
        team_id = str(team_id) if team_id is not None else str(game_id) + ":" + side
        name = first(team, "name", "acronym") or first(meta, "name", "acronym") or side
        teams[side] = {"id": team_id, "name": str(name),
                       "gold": first(team, "totalGold", "gold"),
                       "kills": first(team, "totalKills", "kills"),
                       "towers": count(first(team, "towers", "towerKills")),
                       "drakes": count(first(team, "dragons", "drakes")),
                       "nashors": count(first(team, "barons", "nashors")),
                       "inhibitors": count(first(team, "inhibitors", "inhibitorKills"))}
    state = str(first(data, "state", "status") or "").lower()
    terminal = state in ("completed", "finished", "ended", "post_game")
    raw = {"game_id": game_id, "match_id": match_id, "current_timestamp": game_time,
           "finished": terminal and first(data, "winnerId", "winnerTeamId") is not None,
           "blue": teams["blue"], "red": teams["red"],
           "source_timestamp": board.get("frameTimestamp"), "lag_seconds": board.get("lagSeconds")}
    winner = first(data, "winnerId", "winnerTeamId")
    if winner is not None:
        side = str(winner).lower()
        raw["winner_id"] = teams[side]["id"] if side in teams else winner
    return normalize(raw)


def cito_get(path, token):
    if not (path == "/lol/live" or path.startswith("/lol/live/") or path.startswith("/lol/games/")):
        raise ValueError("invalid Cito path")
    request = urllib.request.Request(API + path,
        headers={"x-api-key": token, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("success") is False:
        raise ValueError("Cito returned unsuccessful response for " + path)
    return payload


def live_games(token):
    payload = cito_get("/lol/live", token)
    rows = payload.get("data") or payload.get("matches") or []
    if not isinstance(rows, list):
        raise ValueError("Cito live response is not a list")
    games = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        game_id = first(row, "currentGameId", "gameId")
        match_id = first(row, "matchId", "match_id")
        if game_id is not None and match_id is not None and row.get("statsAvailable", True):
            games[str(game_id)] = row
    return games


def reconcile_winner(metadata, frame):
    """Resolve only explicit winners; ambiguous postgame results remain stale."""
    data = metadata.get("data") if isinstance(metadata.get("data"), dict) else metadata
    value = first(data, "winnerTeamId", "winnerId", "winningTeamId", "winner_id")
    if isinstance(value, dict):
        value = first(value, "id", "teamId", "side")
    side = str(first(data, "winnerSide", "winningSide") or value or "").lower()
    if side in ("blue", "red"):
        return frame[side]["id"]
    if value is not None and str(value) in (frame["blue"]["id"], frame["red"]["id"]):
        return str(value)
    return None


class Collector:
    def __init__(self, token, db_path, model, poll_seconds=30, discover_seconds=600,
                 game_id=None, match_id=None):
        self.token, self.db_path, self.model = token, db_path, model
        self.poll_seconds = max(5, int(poll_seconds))
        self.discover_seconds = max(60, int(discover_seconds))
        self.games = {}
        if game_id and match_id:
            self.games[str(game_id)] = {"currentGameId": str(game_id), "matchId": str(match_id)}
        self.manual = bool(game_id and match_id)
        self.next_discover = 0
        self.next_poll = {}
        self.last_error = None
        self.last_frame_at = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="cito-rest-collector").start()

    def _run(self):
        while True:
            self.run_once()
            time.sleep(1)

    def run_once(self, now=None):
        now = time.time() if now is None else now
        if not self.manual and now >= self.next_discover:
            self.next_discover = now + self.discover_seconds
            try:
                self.games.update(live_games(self.token))
            except Exception as exc:
                self._error("discovery", exc)
        for game_id, live in list(self.games.items()):
            if now < self.next_poll.get(game_id, 0):
                continue
            self.next_poll[game_id] = now + self.poll_seconds
            try:
                encoded = urllib.parse.quote(game_id, safe="")
                board = cito_get("/lol/live/" + encoded + "/stats", self.token)
                data = board.get("data") or {}
                timing = None
                if first(data, "gameTime", "gameTimeSeconds", "game_time", "currentTimestamp") is None:
                    timing = cito_get("/lol/live/" + encoded + "/visual-state", self.token)
                frame = normalize_cito(board, live, timing)
                prior = game(self.db_path, game_id)
                previous = prior["latest"]["frame"] if prior else None
                if previous is not None and frame["game_time"] < previous["game_time"]:
                    raise ValueError("Cito game time moved backwards")
                changed = (previous is None or frame["game_time"] > previous["game_time"]
                           or frame["blue"] != previous["blue"] or frame["red"] != previous["red"]
                           or frame["finished"] != previous["finished"])
                if changed:
                    save(self.db_path, frame, predict(frame, self.model), self.model["kind"])
                    self.last_frame_at = time.time()
                self.last_error = None
                if frame["finished"] and frame["winner_id"] is not None:
                    self.games.pop(game_id, None)
                elif str(first(data, "state", "status") or "").lower() in (
                        "completed", "finished", "ended", "post_game"):
                    self._finish(game_id)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    self._finish(game_id)
                else:
                    self._error(game_id, exc)
                    if exc.code == 429:
                        self.next_poll[game_id] = now + max(60, self.poll_seconds)
            except Exception as exc:
                self._error(game_id, exc)

    def _finish(self, game_id):
        stored = game(self.db_path, game_id)
        if not stored:
            self.games.pop(game_id, None)
            return
        frame = stored["latest"]["frame"]
        try:
            metadata = cito_get("/lol/games/" + urllib.parse.quote(game_id, safe=""), self.token)
            winner = reconcile_winner(metadata, frame)
            if winner is None:
                raise ValueError("Cito postgame has no winner matching stored sides")
            frame["winner_id"] = winner
            frame["finished"] = True
            frame["outcome_reconciled"] = True
            # No terminal live frame exists: preserve the last *predicted* probability.
            save(self.db_path, frame, stored["latest"]["blue_probability"],
                 stored["latest"]["model_kind"])
            self.games.pop(game_id, None)
            self.last_error = None
        except Exception as exc:
            self._error("postgame " + game_id, exc)
            self.next_poll[game_id] = time.time() + max(600, self.discover_seconds)

    def _error(self, stage, exc):
        message = "%s: %s" % (type(exc).__name__, str(exc).replace(self.token, "[redacted]"))
        self.last_error = stage + ": " + message
        LOG.warning("Cito %s failed: %s", stage, message)
