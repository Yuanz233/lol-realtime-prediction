"""Import curated, outcome-verified games from public website frame windows.

The public LoL Esports page supplies game/team IDs. A human-checked manifest
supplies per-game winners and durations; no winner is inferred from series
scores or a terminal gold lead. There is no website API key in this workflow.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from import_lolesports_history import collect, fetch_window, teams
from predictor import load_model, predict
from schedule_index import event_competition, league_matches, read_schedule
from storage import game, save


def seconds(text):
    minute, second = text.split(":")
    if not minute.isdigit() or not second.isdigit() or int(second) >= 60:
        raise ValueError("invalid verified duration: " + text)
    return int(minute) * 60 + int(second)


def candidates(manifest, events_by_league):
    seen_games = set()
    result = []
    for series in manifest["series"]:
        league = str(series.get("league", "lpl")).lower()
        match_id = str(series["match_id"])
        event = events_by_league.get(league, {}).get(match_id)
        if event is not None:
            if event["startTime"][:10] != series["date"]:
                raise ValueError("match %s date disagrees with verified result" % match_id)
            team_ids = {}
            for team in event["matchTeams"]:
                code = team["code"]
                team_id = str(team["id"]).split(":")[-1]
                if code in team_ids or not team_id.isdigit():
                    raise ValueError("match %s has invalid team mapping" % match_id)
                team_ids[code] = team_id
            official_games = sorted((g for g in event["match"]["games"]
                                     if g["state"] == "completed"), key=lambda g: g["number"])
            published = {team["code"]: (team.get("result") or {}).get("gameWins")
                         for team in event["matchTeams"]}
            competition = event_competition(event)
        else:
            source = str(series.get("official_source") or "")
            team_ids = {str(code): str(team_id)
                        for code, team_id in (series.get("team_ids") or {}).items()}
            if (not source.startswith("https://lolesports.com/")
                    or not team_ids or any(not value.isdigit() for value in team_ids.values())):
                raise ValueError("match %s missing from completed %s schedule" % (match_id, league))
            official_games = [{"id": label.get("game_id"), "number": number}
                              for number, label in enumerate(series["games"], 1)]
            published = {str(code): int(wins)
                         for code, wins in (series.get("series_wins") or {}).items()}
            competition = {"league_slug": league, "league_name": league.upper(),
                           "tournament_name": str(series.get("tournament_name") or "未知赛事"),
                           "stage_name": str(series.get("stage_name") or "未知阶段"),
                           "event_start": series.get("event_start")}
        if set(team_ids) != set(series["teams"]) or len(team_ids) != 2:
            raise ValueError("match %s teams disagree with verified result" % match_id)
        verified = series["games"]
        if len(official_games) != len(verified):
            raise ValueError("match %s game count differs from verified result" % match_id)
        wins = {code: 0 for code in team_ids}
        for official, label in zip(official_games, verified):
            number = official["number"]
            game_id = str(official["id"])
            winner = label["winner"]
            if number < 1 or number > len(verified) or game_id in seen_games or winner not in team_ids:
                raise ValueError("match %s has invalid/duplicate game label" % match_id)
            seen_games.add(game_id)
            wins[winner] += 1
            result.append({"match_id": match_id, "game_id": game_id, "number": number,
                           "date": series["date"], "league": league, "team_ids": team_ids,
                           "competition": competition,
                           "winner_code": winner, "duration_seconds": seconds(label["duration"]),
                           "label_source": label.get("label_source") or
                           "https://gol.gg/game/stats/%d/page-game/" %
                           (series["gol_first_game"] + number - 1)})
        if published != wins:
            raise ValueError("match %s manually verified game winners disagree with series total" % match_id)
    return result


def import_one(item, db_path, model, step_seconds=120, pause_seconds=0.35):
    stored = game(db_path, item["game_id"])
    if stored is not None:
        last = stored["latest"]["frame"]
        if (not last["finished"] or last["winner_id"] != item["team_ids"][item["winner_code"]]
                or last["match_id"] != item["match_id"]
                or set((last["blue"]["id"], last["red"]["id"])) != set(item["team_ids"].values())):
            raise ValueError("stored game conflicts with verified winner, match or team IDs")
        return {"status": "skipped", "points": len(stored["points"])}
    first = fetch_window(item["game_id"])
    if str(first.get("esportsMatchId")) != item["match_id"]:
        raise ValueError("feed match ID disagrees with public schedule")
    sides = teams(first)
    if set(sides.values()) != set(item["team_ids"].values()):
        raise ValueError("feed team IDs disagree with public schedule")
    by_id = {team_id: code for code, team_id in item["team_ids"].items()}
    names = {side: by_id[team_id] for side, team_id in sides.items()}
    winner_id = item["team_ids"][item["winner_code"]]
    winner_side = next(side for side, team_id in sides.items() if team_id == winner_id)
    points = collect(item["game_id"], names, winner_side, item["label_source"],
                     step_seconds=step_seconds, pause_seconds=pause_seconds,
                     first_payload=first, expected_match_id=item["match_id"],
                     expected_team_ids=item["team_ids"].values(),
                     expected_duration_seconds=item["duration_seconds"],
                     competition=item["competition"])
    if points[-1]["winner_id"] != winner_id:
        raise ValueError("collected winner differs from verified manifest")
    for frame in points:
        save(db_path, frame, predict(frame, model), model["kind"])
    return {"status": "imported", "points": len(points), "winner_side": winner_side,
            "actual_duration_seconds": points[-1]["game_time"],
            "pause_adjusted_seconds": points[-1].get("pause_adjusted_seconds", 0),
            "outcome_reconciled": points[-1].get("outcome_reconciled", False)}


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(root / "training_manifest.json"))
    parser.add_argument("--schedule-html", help="previously downloaded public page; otherwise fetch once")
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--model", default=str(root / "models" / "live.json"))
    parser.add_argument("--report", default=str(root / "data" / "history_crawl_report.json"))
    parser.add_argument("--step-seconds", type=int, default=30)
    parser.add_argument("--pause-seconds", type=float, default=0.35)
    parser.add_argument("--max-new-games", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel games; each game still samples sequentially")
    parser.add_argument("--index-only", action="store_true")
    args = parser.parse_args()
    if (args.step_seconds < 30 or args.pause_seconds < 0 or args.max_new_games < 0
            or args.workers < 1 or args.workers > 6):
        parser.error("invalid sample interval, pause or max-new-games")
    if args.workers > 1 and args.max_new_games:
        parser.error("--max-new-games cannot be combined with parallel workers")
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    leagues = sorted({str(series.get("league", "lpl")).lower()
                      for series in manifest["series"]})
    if args.schedule_html and len(leagues) != 1:
        parser.error("--schedule-html requires a manifest containing exactly one league")
    events_by_league = {}
    for league in leagues:
        html = (Path(args.schedule_html).read_text(encoding="utf-8")
                if args.schedule_html else read_schedule(league))
        events_by_league[league] = league_matches(html, league, ("completed",))
    indexed = candidates(manifest, events_by_league)
    print("verified manifest: %d single games across %d matches" %
          (len(indexed), len(manifest["series"])), flush=True)
    if args.index_only:
        return
    model = load_model(args.model)
    report = {"source_pages": ["https://lolesports.com/en-US/leagues/" + league
                               for league in leagues],
              "official_sources": sorted({series["official_source"]
                                          for series in manifest["series"]
                                          if series.get("official_source")}),
              "manifest": str(args.manifest), "games": [], "errors": []}
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    imported = 0
    def run(item):
        try:
            outcome = import_one(item, args.db, model, args.step_seconds, args.pause_seconds)
            return item, outcome, None
        except Exception as exc:
            return item, None, str(exc)

    def record(item, outcome, error):
        nonlocal imported
        if error is None:
            report["games"].append({**item, **outcome})
            imported += outcome["status"] == "imported"
            print(item["date"], item["game_id"], item["winner_code"],
                  outcome["status"], outcome["points"], "points", flush=True)
        else:
            report["errors"].append({"game_id": item["game_id"], "error": error})
            print(item["game_id"], "ERROR", error, flush=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.workers == 1:
        for item in indexed:
            if args.max_new_games and imported >= args.max_new_games:
                break
            record(*run(item))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run, item) for item in indexed]
            for future in as_completed(futures):
                record(*future.result())
    print("new games %d; errors %d; report %s" %
          (imported, len(report["errors"]), report_path), flush=True)
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
