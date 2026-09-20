"""Read match and single-game IDs embedded in a public LoL Esports league page.

This website rendering format is experimental and may change without notice.
No website API key is used or stored.
"""
import json
import re
import urllib.request

SCHEDULE_URL = "https://lolesports.com/en-US/leagues/"
MARKER = '<script>(window[Symbol.for("ApolloSSRDataTransport")]'


def read_schedule(league_slug="lpl"):
    if not re.fullmatch(r"[a-z0-9_-]+", league_slug):
        raise ValueError("invalid LoL Esports league slug")
    request = urllib.request.Request(SCHEDULE_URL + league_slug, headers={
        "Accept": "text/html", "User-Agent": "lol-realtime-prediction/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def league_matches(html, league_slug="lpl", states=("completed",)):
    start = html.find(MARKER)
    if start < 0:
        raise ValueError("LoL Esports page has no embedded Apollo schedule")
    end = html.find("</script>", start)
    script = html[start:end]
    opening = script.find(").push(")
    if end < 0 or opening < 0 or not script.endswith(")"):
        raise ValueError("LoL Esports Apollo script changed format")
    source = script[opening + len(").push("):-1]
    # Apollo's inline JavaScript may contain an undefined value for a query
    # that has no data. All actual events still come from parsed JSON objects.
    payload = json.loads(re.sub(r"(?<=:)undefined\b", "null", source))
    matches = {}
    for response in payload.get("rehydrate", {}).values():
        data = response.get("data") or {}
        esports = data.get("esports") if isinstance(data, dict) else None
        if not isinstance(esports, dict):
            continue
        for event in esports.get("events", []):
            if (event.get("type") == "match" and event.get("state") in states
                    and (event.get("league") or {}).get("slug") == league_slug):
                if str(event.get("id")) != str((event.get("match") or {}).get("id")):
                    raise ValueError("schedule event and match IDs disagree")
                matches[str(event["id"])] = event
    if not matches:
        raise ValueError("LoL Esports page has no matching %s matches" % league_slug)
    return matches


def completed_matches(html):
    """Backward-compatible LPL helper used by the curated training importer."""
    return league_matches(html, "lpl", ("completed",))


def event_competition(event):
    """Return stable display metadata from a public schedule event."""
    league = event.get("league") or {}
    tournament = event.get("tournament") or {}
    slug = str(league.get("slug") or "unknown").lower()
    return {
        "league_slug": slug,
        "league_name": str(league.get("name") or slug.upper()),
        "tournament_name": str(tournament.get("name") or "未知赛事"),
        "stage_name": str(event.get("blockName") or "未知阶段"),
        "event_start": event.get("startTime"),
    }
