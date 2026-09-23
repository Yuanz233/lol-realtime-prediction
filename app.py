"""HTTP service; paid Cito collection is a disabled legacy fallback."""
import argparse
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from predictor import load_model
from provider import Collector
from lolesports_provider import LoLEsportsCollector, MultiLeagueCollector
from sync_completed_history import CompletedHistorySync
from storage import connect, game, lists
from finalize_game import finalize

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MIME = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
STARTED_AT = time.time()


def provider_status(collector, model, manual_result_enabled=False):
    payload = {"provider_enabled": collector is not None,
               "provider_name": getattr(collector, "provider_name", None),
               "provider_phase": getattr(collector, "phase", None),
               "active_game_id": getattr(collector, "active_game_id", None),
               "source_lag_seconds": getattr(collector, "source_lag_seconds", None),
               "last_frame_at": getattr(collector, "last_frame_at", None),
               "provider_error": getattr(collector, "last_error", None),
               "provider_warning": getattr(collector, "last_warning", None),
               "leagues": []}
    if collector is not None and hasattr(collector, "status"):
        detail = collector.status()
        if "provider_enabled" in detail:
            payload.update(detail)
        else:
            payload["leagues"] = [detail]
    payload["model_kind"] = model["kind"]
    payload["model_training"] = model.get("training")
    payload["model_holdout"] = model.get("holdout")
    payload["manual_result_enabled"] = manual_result_enabled
    return payload


def make_handler(db_path, collector, model, manual_result_enabled=False):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, content_type, status=200, cache_control=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            policy = cache_control or "no-store"
            self.send_header("Cache-Control", policy)
            if policy == "no-store":
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status=200):
            self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json", status)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/api/matches":
                self._json(lists(db_path))
                return
            if path.startswith("/api/games/"):
                game_id = path[len("/api/games/"):]
                if not game_id or "/" in game_id or len(game_id) > 100:
                    self._json({"error": "invalid game id"}, 400)
                    return
                result = game(db_path, game_id)
                self._json(result if result else {"error": "game not found"}, 200 if result else 404)
                return
            if path == "/api/status":
                self._json(provider_status(collector, model, manual_result_enabled))
                return
            if path == "/api/health":
                try:
                    with connect(db_path) as db:
                        db.execute("SELECT 1").fetchone()
                    self._json({"status": "ok", "uptime_seconds": round(time.time() - STARTED_AT),
                                "model_kind": model["kind"]})
                except Exception as exc:
                    self._json({"status": "error", "error": str(exc)}, 503)
                return
            file = STATIC / ("index.html" if path == "/" else path.lstrip("/"))
            if file.parent != STATIC or not file.is_file() or file.suffix not in MIME:
                self._send(b"Not found", "text/plain", 404)
                return
            self._send(file.read_bytes(), MIME[file.suffix])

        def do_POST(self):
            path = unquote(urlparse(self.path).path)
            prefix, suffix = "/api/games/", "/finalize"
            if not manual_result_enabled:
                self._json({"error": "manual result confirmation is disabled"}, 403)
                return
            if not path.startswith(prefix) or not path.endswith(suffix):
                self._json({"error": "not found"}, 404)
                return
            game_id = path[len(prefix):-len(suffix)]
            if not game_id or "/" in game_id or len(game_id) > 100:
                self._json({"error": "invalid game id"}, 400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError("invalid request size")
                body = json.loads(self.rfile.read(length))
                latest = finalize(db_path, game_id, str(body.get("winner_side", "")),
                                  str(body.get("label_source", "")))
                self._json({"ok": True, "game": latest})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(ROOT / "data" / "matches.sqlite3")))
    parser.add_argument("--model", default=os.getenv("MODEL_PATH", str(ROOT / "models" / "live.json")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    model = load_model(args.model)
    manual_result_enabled = os.getenv("ALLOW_MANUAL_RESULT", "false").strip().lower() in (
        "1", "true", "yes")
    live_provider = os.getenv("LIVE_PROVIDER", "lolesports").strip().lower()
    if live_provider not in ("none", "cito", "lolesports"):
        raise SystemExit("LIVE_PROVIDER must be none, lolesports or cito")
    token = os.getenv("CITO_API_KEY", "").strip()
    game_id = os.getenv("CITO_GAME_ID", "").strip()
    match_id = os.getenv("CITO_MATCH_ID", "").strip()
    if live_provider == "cito" and not token:
        raise SystemExit("LIVE_PROVIDER=cito requires CITO_API_KEY")
    if live_provider == "cito" and bool(game_id) != bool(match_id):
        raise SystemExit("set both CITO_GAME_ID and CITO_MATCH_ID, or neither")
    collector = None
    if live_provider == "cito":
        collector = Collector(token, args.db, model,
            poll_seconds=int(os.getenv("CITO_POLL_SECONDS", "30")),
            discover_seconds=int(os.getenv("CITO_DISCOVER_SECONDS", "600")),
            game_id=game_id or None, match_id=match_id or None)
    elif live_provider == "lolesports":
        leagues_text = os.getenv("LOLESPORTS_LEAGUES",
                                  os.getenv("LOLESPORTS_LEAGUE", "lpl,lck,lec,vcs,worlds"))
        leagues = list(dict.fromkeys(value.strip().lower() for value in leagues_text.split(",")
                                     if value.strip()))
        if not leagues:
            raise SystemExit("LOLESPORTS_LEAGUES must contain at least one league slug")
        free_games = [value.strip() for value in os.getenv("LOLESPORTS_GAME_IDS", "").split(",")
                      if value.strip()]
        free_match = os.getenv("LOLESPORTS_MATCH_ID", "").strip() or None
        if (free_match or free_games) and len(leagues) != 1:
            raise SystemExit("explicit LoL Esports match/game IDs require exactly one configured league")
        collectors = [LoLEsportsCollector(args.db, model,
            league_slug=league, match_id=free_match, game_ids=free_games,
            poll_seconds=int(os.getenv("LOLESPORTS_POLL_SECONDS", "30")),
            discover_seconds=int(os.getenv("LOLESPORTS_DISCOVER_SECONDS", "120")),
            window_delay_seconds=int(os.getenv("LOLESPORTS_WINDOW_DELAY_SECONDS", "40")))
            for league in leagues]
        history_sync = CompletedHistorySync(args.db, model, leagues,
            interval_seconds=int(os.getenv("LOLESPORTS_HISTORY_SYNC_SECONDS", "21600")),
            days=int(os.getenv("LOLESPORTS_HISTORY_DAYS", "14")),
            workers=int(os.getenv("LOLESPORTS_HISTORY_WORKERS", "2")),
            max_new_games=int(os.getenv("LOLESPORTS_HISTORY_MAX_NEW_GAMES", "20")))
        collector = MultiLeagueCollector(collectors, history_sync)
    if collector:
        collector.start()
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(args.db, collector, model,
                                              manual_result_enabled))
    logging.info("open http://%s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
