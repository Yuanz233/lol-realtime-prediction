"""SQLite persistence with in-place schema migration for match metadata."""
import json
import sqlite3
import time
from pathlib import Path

COMPETITION_DEFAULTS = {
    "league_slug": "unknown", "league_name": "未知赛区",
    "tournament_name": "未知赛事", "stage_name": "未知阶段", "event_start": None,
}


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS frames (
        game_id TEXT NOT NULL, match_id TEXT NOT NULL, game_time INTEGER NOT NULL,
        received_at REAL NOT NULL, finished INTEGER NOT NULL, winner_id TEXT,
        blue_name TEXT NOT NULL, red_name TEXT NOT NULL, blue_probability REAL NOT NULL,
        model_kind TEXT NOT NULL, frame_json TEXT NOT NULL,
        league_slug TEXT NOT NULL DEFAULT 'unknown',
        league_name TEXT NOT NULL DEFAULT '未知赛区',
        tournament_name TEXT NOT NULL DEFAULT '未知赛事',
        stage_name TEXT NOT NULL DEFAULT '未知阶段', event_start TEXT,
        PRIMARY KEY (game_id, game_time)
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(frames)")}
    migrations = {
        "league_slug": "TEXT NOT NULL DEFAULT 'unknown'",
        "league_name": "TEXT NOT NULL DEFAULT '未知赛区'",
        "tournament_name": "TEXT NOT NULL DEFAULT '未知赛事'",
        "stage_name": "TEXT NOT NULL DEFAULT '未知阶段'",
        "event_start": "TEXT",
    }
    for name, declaration in migrations.items():
        if name not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN %s %s" % (name, declaration))
    db.execute("CREATE INDEX IF NOT EXISTS frames_match ON frames(match_id, game_time)")
    db.execute("CREATE INDEX IF NOT EXISTS frames_competition ON frames(league_slug, tournament_name, stage_name)")
    db.commit()
    return db


def competition(frame):
    supplied = frame.get("competition") or {}
    return {key: supplied.get(key) if supplied.get(key) not in (None, "") else default
            for key, default in COMPETITION_DEFAULTS.items()}


def save(db_path, frame, probability, model_kind):
    contest = competition(frame)
    stored = dict(frame)
    stored["competition"] = contest
    with connect(db_path) as db:
        db.execute("""INSERT INTO frames (
            game_id, match_id, game_time, received_at, finished, winner_id,
            blue_name, red_name, blue_probability, model_kind, frame_json,
            league_slug, league_name, tournament_name, stage_name, event_start
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(game_id, game_time) DO UPDATE SET
            received_at=excluded.received_at, finished=excluded.finished,
            winner_id=excluded.winner_id, blue_probability=excluded.blue_probability,
            model_kind=excluded.model_kind, frame_json=excluded.frame_json,
            league_slug=excluded.league_slug, league_name=excluded.league_name,
            tournament_name=excluded.tournament_name, stage_name=excluded.stage_name,
            event_start=excluded.event_start""",
            (str(frame["game_id"]), str(frame["match_id"]), frame["game_time"], time.time(),
             int(frame["finished"]), str(frame["winner_id"]) if frame["winner_id"] is not None else None,
             frame["blue"]["name"], frame["red"]["name"], probability, model_kind,
             json.dumps(stored, ensure_ascii=False), contest["league_slug"],
             contest["league_name"], contest["tournament_name"], contest["stage_name"],
             contest["event_start"]))


def update_match_competition(db_path, match_id, contest):
    """Backfill metadata without changing stored frames or predictions."""
    values = {**COMPETITION_DEFAULTS, **{k: v for k, v in contest.items() if v not in (None, "")}}
    with connect(db_path) as db:
        rows = db.execute("SELECT game_id, game_time, frame_json FROM frames WHERE match_id=?",
                          (str(match_id),)).fetchall()
        for row in rows:
            frame = json.loads(row["frame_json"])
            frame["competition"] = values
            db.execute("""UPDATE frames SET frame_json=?, league_slug=?, league_name=?,
                tournament_name=?, stage_name=?, event_start=?
                WHERE game_id=? AND game_time=?""",
                (json.dumps(frame, ensure_ascii=False), values["league_slug"], values["league_name"],
                 values["tournament_name"], values["stage_name"], values["event_start"],
                 row["game_id"], row["game_time"]))
        db.commit()
    return len(rows)


def _public(row):
    item = dict(row)
    item["frame"] = json.loads(item.pop("frame_json"))
    item["finished"] = bool(item["finished"])
    item["frame"]["competition"] = {key: item.get(key) for key in COMPETITION_DEFAULTS}
    return item


def catalog_from_items(items):
    leagues = {}
    for item in items:
        contest = item["frame"].get("competition") or COMPETITION_DEFAULTS
        slug = contest.get("league_slug") or "unknown"
        league = leagues.setdefault(slug, {"slug": slug,
            "name": contest.get("league_name") or slug.upper(), "count": 0, "tournaments": {}})
        league["count"] += 1
        tournament_name = contest.get("tournament_name") or "未知赛事"
        tournament = league["tournaments"].setdefault(tournament_name,
            {"name": tournament_name, "count": 0, "stages": {}})
        tournament["count"] += 1
        stage_name = contest.get("stage_name") or "未知阶段"
        tournament["stages"][stage_name] = tournament["stages"].get(stage_name, 0) + 1
    result = []
    for league in sorted(leagues.values(), key=lambda x: (x["slug"] == "unknown", x["name"])):
        league["tournaments"] = [{**value, "stages": [
            {"name": name, "count": count} for name, count in sorted(value["stages"].items())
        ]} for value in sorted(league["tournaments"].values(), key=lambda x: x["name"])]
        result.append(league)
    return result


def lists(db_path, limit=1000):
    limit = max(1, min(int(limit), 5000))
    with connect(db_path) as db:
        rows = db.execute("""SELECT f.* FROM frames f JOIN
            (SELECT game_id, MAX(game_time) last_time FROM frames GROUP BY game_id) latest
            ON f.game_id=latest.game_id AND f.game_time=latest.last_time
            ORDER BY f.received_at DESC LIMIT ?""", (limit,)).fetchall()
    items = [_public(r) for r in rows]
    now = time.time()
    live = [i for i in items if not i["finished"] and now - i["received_at"] < 120]
    pending = [i for i in items if not i["finished"] and now - i["received_at"] >= 120]
    history = [i for i in items if i["finished"]] + pending
    # Keep the old key empty so a browser tab still running the pre-fix
    # JavaScript cannot merge expired games back into the live selector.
    return {"live": live, "history": history, "pending": pending, "stale": [],
            "catalog": catalog_from_items(history)}


def game(db_path, game_id):
    with connect(db_path) as db:
        rows = db.execute("SELECT * FROM frames WHERE game_id=? ORDER BY game_time",
                          (str(game_id),)).fetchall()
    if not rows:
        return None
    points = [{"time": r["game_time"], "blue_probability": r["blue_probability"]} for r in rows]
    return {"latest": _public(rows[-1]), "points": points}
