"""Experimental free live collector for LoL Esports website frame endpoints.

This reads the same website-only endpoints used by the historical importer.  It
is intentionally opt-in because Riot does not document them as a supported
third-party live API.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from import_lolesports_history import timestamp, teams
from predictor import predict
from provider import normalize
from schedule_index import event_competition, league_matches, read_schedule
from storage import game, save
from tls_context import TLS_CONTEXT


LOG = logging.getLogger(__name__)
FEED = "https://feed.lolesports.com/livestats/v1/"


def website_json(kind, game_id, start=None, timeout=20):
    if kind not in ("window", "details") or not str(game_id).isdigit():
        raise ValueError("invalid LoL Esports feed request")
    url = FEED + kind + "/" + str(game_id)
    if start is not None:
        boundary = start.replace(second=(start.second // 10) * 10, microsecond=0)
        value = boundary.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        url += "?" + urllib.parse.urlencode({"startingTime": value})
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "lol-realtime-prediction/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS_CONTEXT) as response:
            if response.status == 204:
                return None
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (204, 404):
            return None
        raise


def schedule_team_names(event):
    result = {}
    for item in event.get("matchTeams", []):
        raw_id = str(item.get("id", ""))
        team_id = raw_id.rsplit(":", 1)[-1]
        if team_id.isdigit():
            result[team_id] = str(item.get("code") or item.get("name") or team_id)
    return result


def metadata_team_names(payload):
    """Best-effort team codes for explicitly pinned games missing from schedule."""
    result = {}
    metadata = payload.get("gameMetadata") or {}
    for side in ("blue", "red"):
        team = metadata.get(side + "TeamMetadata") or {}
        team_id = str(team.get("esportsTeamId") or "")
        prefixes = {str(item.get("summonerName") or "").split(" ", 1)[0]
                    for item in team.get("participantMetadata") or []
                    if str(item.get("summonerName") or "").strip()}
        name = next(iter(prefixes)) if len(prefixes) == 1 else team_id
        if team_id:
            result[team_id] = name or team_id
    return result


def live_frame(payload, source, beginning, names, paused_seconds=0, competition=None):
    team_ids = teams(payload)
    instant = timestamp(source["rfc460Timestamp"])
    raw = {
        "game_id": payload["esportsGameId"],
        "match_id": payload["esportsMatchId"],
        "current_timestamp": max(0, round((instant - beginning).total_seconds() - paused_seconds)),
        "finished": False,
        "winner_id": None,
        "played_at": beginning.isoformat().replace("+00:00", "Z"),
        "source_timestamp": source["rfc460Timestamp"],
    }
    for side in ("blue", "red"):
        stats = source.get(side + "Team")
        if not isinstance(stats, dict):
            raise ValueError("feed frame missing " + side + " stats")
        team_id = team_ids[side]
        raw[side] = {
            "id": team_id,
            "name": names.get(team_id, side),
            "gold": stats.get("totalGold"),
            "kills": stats.get("totalKills"),
            "towers": stats.get("towers"),
            "drakes": len(stats["dragons"]) if isinstance(stats.get("dragons"), list) else stats.get("dragons"),
            "nashors": stats.get("barons"),
            "inhibitors": stats.get("inhibitors"),
        }
    frame = normalize(raw)
    frame["source_name"] = "LoL Esports website feed (experimental live)"
    frame["source_game_state"] = source.get("gameState")
    frame["source_timestamp"] = source["rfc460Timestamp"]
    if competition:
        frame["competition"] = competition
    frame["lag_seconds"] = max(0, round((datetime.now(timezone.utc) - instant).total_seconds(), 3))
    if paused_seconds:
        frame["pause_adjusted_seconds"] = round(paused_seconds)
    return frame


class LoLEsportsCollector:
    provider_name = "lolesports"

    def __init__(self, db_path, model, league_slug="lpl", match_id=None, game_ids=(),
                 poll_seconds=30, discover_seconds=120, fetch=website_json,
                 schedule_fetch=read_schedule, window_delay_seconds=40):
        self.db_path, self.model = db_path, model
        self.league_slug = league_slug
        self.match_id = str(match_id) if match_id else None
        self.fixed_match_id = bool(match_id)
        self.configured_game_ids = [str(value) for value in game_ids if str(value)]
        self.game_ids = [str(value) for value in game_ids if str(value)]
        self.poll_seconds = max(1, int(poll_seconds))
        self.discover_seconds = max(60, int(discover_seconds))
        self.window_delay_seconds = max(30, int(window_delay_seconds))
        self.fetch, self.schedule_fetch = fetch, schedule_fetch
        self.team_names = {}
        self.beginnings = {}
        self.last_source = {}
        self.pause_started = {}
        self.paused_seconds = {}
        self.completed = set()
        self.next_discover = 0
        self.next_poll = 0
        self.phase = "preparing"
        self.active_game_id = None
        self.last_error = None
        self.last_frame_at = None
        self.source_lag_seconds = None
        self.scheduled_start = None
        self.competition = {"league_slug": league_slug, "league_name": league_slug.upper(),
                            "tournament_name": "未知赛事", "stage_name": "未知阶段",
                            "event_start": None}

    def start(self):
        threading.Thread(target=self._run, daemon=True,
                         name="lolesports-%s-collector" % self.league_slug).start()

    def _run(self):
        while True:
            self.run_once()
            time.sleep(1)

    def discover(self):
        html = self.schedule_fetch(self.league_slug)
        events = {}
        for state in ("inProgress", "unstarted", "completed"):
            try:
                events.update(league_matches(html, self.league_slug, (state,)))
            except ValueError as exc:
                if "no matching" not in str(exc):
                    raise
        selected = events.get(self.match_id) if self.fixed_match_id and self.match_id else None
        if self.fixed_match_id and selected is None:
            raise ValueError("pinned match is no longer present in current schedule page")
        if selected is None:
            def has_unfinished_game(item):
                games = (item.get("match") or {}).get("games") or []
                return not games or any(game.get("state") not in ("completed", "unneeded")
                                        for game in games)
            candidates = [item for item in events.values()
                          if item.get("state") in ("inProgress", "unstarted")
                          and has_unfinished_game(item)]
            if not candidates:
                self.match_id = None
                self.game_ids = []
                self.team_names = {}
                self.scheduled_start = None
                self.active_game_id = None
                self.phase = "idle"
                return
            selected = sorted(candidates, key=lambda item: (
                item.get("state") != "inProgress", item.get("startTime", "")))[0]
        selected_match_id = str(selected["id"])
        if self.match_id != selected_match_id:
            self.match_id = selected_match_id
            self.completed.clear()
            self.beginnings.clear()
            self.last_source.clear()
            self.pause_started.clear()
            self.paused_seconds.clear()
        if str((selected.get("match") or {}).get("id")) != self.match_id:
            raise ValueError("schedule match ID changed")
        scheduled = [str(item["id"]) for item in (selected.get("match") or {}).get("games", [])
                     if item.get("state") != "unneeded"]
        self.completed.update(str(item["id"])
                              for item in (selected.get("match") or {}).get("games", [])
                              if item.get("state") == "completed")
        if (self.configured_game_ids and scheduled
                and not set(scheduled).issubset(set(self.configured_game_ids))):
            raise ValueError("configured game IDs disagree with current schedule")
        self.game_ids = self.configured_game_ids or scheduled
        self.team_names = schedule_team_names(selected)
        self.competition = event_competition(selected)
        value = selected.get("startTime")
        self.scheduled_start = timestamp(value).timestamp() if value else None

    def run_once(self, now=None):
        now = time.time() if now is None else now
        if now >= self.next_discover:
            self.next_discover = now + self.discover_seconds
            try:
                self.discover()
                self.last_error = None
            except Exception as exc:
                self._error("schedule", exc)
        if (self.scheduled_start and now < self.scheduled_start - 3600
                and not any(value in self.beginnings for value in self.game_ids)):
            self.phase = "scheduled"
            self.active_game_id = None
            return
        if now < self.next_poll or not self.game_ids:
            return
        self.next_poll = now + self.poll_seconds
        current = next((value for value in self.game_ids if value not in self.completed), None)
        if current is None:
            self.phase = "finished"
            self.active_game_id = None
            return
        self.active_game_id = current
        try:
            self._poll_game(current)
            self.last_error = None
        except Exception as exc:
            self._error(current, exc)

    def _poll_game(self, game_id):
        first = self.fetch("window", game_id, None)
        if first is None:
            self.phase = "preparing"
            return
        if str(first.get("esportsGameId")) != game_id:
            raise ValueError("feed returned another game ID")
        if self.match_id and str(first.get("esportsMatchId")) != self.match_id:
            raise ValueError("feed match ID disagrees with schedule")
        if not self.team_names:
            self.team_names = metadata_team_names(first)
        frames = first.get("frames") or []
        if not frames:
            self.phase = "preparing"
            return
        # A far-future window is clamped by the website endpoint to its latest
        # currently published tail. This is verified by the live probe before
        # enabling the provider for deployment.
        recent = None
        # The feed rejects a startingTime newer than its delayed publication
        # head with HTTP 400. The observed lag varies during a match, so step
        # backwards instead of treating a temporarily short delay as failure.
        for extra_delay in range(0, 151, 30):
            query_at = datetime.now(timezone.utc) - timedelta(
                seconds=self.window_delay_seconds + extra_delay)
            try:
                recent = self.fetch("window", game_id, query_at)
                if recent is not None:
                    break
            except urllib.error.HTTPError as exc:
                if exc.code != 400:
                    raise
        recent = recent or first
        if str(recent.get("esportsGameId")) != game_id:
            raise ValueError("latest window returned another game ID")
        recent_frames = recent.get("frames") or frames
        recovered_sources = []
        if game_id not in self.beginnings:
            stored = game(self.db_path, game_id)
            stored_start = ((stored or {}).get("latest") or {}).get("frame", {}).get("played_at")
            possible_starts = list(frames + recent_frames)
            if stored_start:
                self.beginnings[game_id] = timestamp(stored_start)
            else:
                # When the collector joins a game late, the no-start request can
                # return only the latest window. Walk backwards once to recover
                # the actual opening frames and a correct game clock.
                saw_frames = bool(possible_starts)
                probe_now = datetime.now(timezone.utc)
                latest_nonempty_minutes = None
                first_empty_minutes = None
                for minutes in range(10, 91, 10):
                    try:
                        earlier = self.fetch("window", game_id,
                                             probe_now - timedelta(minutes=minutes))
                    except urllib.error.HTTPError as exc:
                        if exc.code != 400:
                            raise
                        earlier = None
                    earlier_frames = (earlier or {}).get("frames") or []
                    if earlier_frames:
                        if str(earlier.get("esportsMatchId")) != self.match_id:
                            raise ValueError("backfill window returned another match ID")
                        possible_starts.extend(earlier_frames)
                        saw_frames = True
                        latest_nonempty_minutes = minutes
                    elif saw_frames:
                        first_empty_minutes = minutes
                        break
                # Refine the final ten-minute boundary to keep a late-started
                # collector's game clock within roughly one minute of kickoff.
                if latest_nonempty_minutes is not None and first_empty_minutes is not None:
                    for minutes in range(latest_nonempty_minutes + 1, first_empty_minutes):
                        try:
                            earlier = self.fetch("window", game_id,
                                                 probe_now - timedelta(minutes=minutes))
                        except urllib.error.HTTPError as exc:
                            if exc.code != 400:
                                raise
                            earlier = None
                        earlier_frames = (earlier or {}).get("frames") or []
                        if not earlier_frames:
                            break
                        if str(earlier.get("esportsMatchId")) != self.match_id:
                            raise ValueError("refined window returned another match ID")
                        possible_starts.extend(earlier_frames)
                possible_starts.sort(key=lambda item: item["rfc460Timestamp"])
                recovered_sources = possible_starts
            first_playable = next((item for item in possible_starts
                                   if item.get("gameState") == "in_game"
                                   and (item.get("blueTeam") or {}).get("totalGold", 0) > 0
                                   and (item.get("redTeam") or {}).get("totalGold", 0) > 0), None)
            if game_id not in self.beginnings:
                if first_playable is None:
                    self.phase = "preparing"
                    return
                self.beginnings[game_id] = timestamp(first_playable["rfc460Timestamp"])
        beginning = self.beginnings[game_id]
        candidates = recovered_sources or recent_frames
        candidates = sorted(candidates, key=lambda item: item["rfc460Timestamp"])
        for source in candidates:
            source_time = timestamp(source["rfc460Timestamp"])
            if self.last_source.get(game_id) and source_time <= self.last_source[game_id]:
                continue
            state = source.get("gameState")
            if state == "paused":
                self.phase = "paused"
                self.pause_started.setdefault(game_id, source_time)
                self.last_source[game_id] = source_time
                continue
            if game_id in self.pause_started:
                self.paused_seconds[game_id] = self.paused_seconds.get(game_id, 0) + max(
                    0, (source_time - self.pause_started.pop(game_id)).total_seconds())
            if state not in ("in_game", "finished"):
                self.phase = "preparing"
                continue
            frame = live_frame(recent, source, beginning, self.team_names,
                               self.paused_seconds.get(game_id, 0), self.competition)
            if state == "finished":
                frame["finished"] = True
                frame["winner_id"] = None
            if frame["blue"]["gold"] <= 0 or frame["red"]["gold"] <= 0:
                continue
            previous = game(self.db_path, game_id)
            if previous is None or frame["game_time"] > previous["latest"]["game_time"]:
                save(self.db_path, frame, predict(frame, self.model), self.model["kind"])
                self.last_frame_at = time.time()
                self.source_lag_seconds = frame["lag_seconds"]
            self.last_source[game_id] = source_time
            if state == "finished":
                self.completed.add(game_id)
                self.phase = "waiting_next_game"
                return
            self.phase = "in_game"

    def status(self):
        return {"league": self.league_slug, "phase": self.phase,
                "match_id": self.match_id, "active_game_id": self.active_game_id,
                "scheduled_start": self.competition.get("event_start"),
                "competition": self.competition, "last_frame_at": self.last_frame_at,
                "source_lag_seconds": self.source_lag_seconds, "error": self.last_error}

    def _error(self, stage, exc):
        self.last_error = "%s: %s: %s" % (stage, type(exc).__name__, str(exc))
        LOG.warning("LoL Esports %s failed: %s", stage, self.last_error)


class MultiLeagueCollector:
    """Run independent low-frequency collectors and expose one combined status."""
    provider_name = "lolesports"

    def __init__(self, collectors, history_sync=None):
        if not collectors:
            raise ValueError("at least one league collector is required")
        self.collectors = collectors
        self.history_sync = history_sync

    def start(self):
        for collector in self.collectors:
            collector.start()
        if self.history_sync is not None:
            self.history_sync.start()

    def _primary(self):
        priority = {"in_game": 0, "paused": 1, "preparing": 2,
                    "waiting_next_game": 3, "scheduled": 4, "finished": 5,
                    "idle": 6}
        return min(self.collectors, key=lambda item: (
            priority.get(item.phase, 9), item.scheduled_start or float("inf")))

    @property
    def phase(self):
        return self._primary().phase

    @property
    def active_game_id(self):
        return self._primary().active_game_id

    @property
    def source_lag_seconds(self):
        return self._primary().source_lag_seconds

    @property
    def last_frame_at(self):
        values = [item.last_frame_at for item in self.collectors if item.last_frame_at]
        return max(values) if values else None

    @property
    def last_error(self):
        errors = ["%s: %s" % (item.league_slug, item.last_error)
                  for item in self.collectors if item.last_error]
        if self.history_sync is not None and self.history_sync.last_error:
            errors.append("history: %s" % self.history_sync.last_error)
        return "; ".join(errors) if errors else None

    def status(self):
        primary = self._primary()
        return {"provider_enabled": True, "provider_name": self.provider_name,
                "provider_phase": primary.phase, "active_game_id": primary.active_game_id,
                "source_lag_seconds": primary.source_lag_seconds,
                "last_frame_at": self.last_frame_at, "provider_error": self.last_error,
                "leagues": [item.status() for item in self.collectors],
                "history_sync": self.history_sync.status() if self.history_sync else None}
