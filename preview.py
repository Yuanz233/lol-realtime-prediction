"""Run an isolated local UI preview with one synthetic live match and real history."""
import argparse
import shutil
import tempfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import make_handler
from predictor import BASELINE, predict
from provider import normalize
from storage import save


def demo_frame(seconds):
    progress = seconds // 120
    return normalize({
        "game_id": "preview-live-001", "match_id": "preview-match-001",
        "current_timestamp": seconds, "finished": False, "winner_id": None,
        "played_at": "2026-09-16T11:00:00+08:00",
        "source_timestamp": "2026-09-16T11:%02d:00+08:00" % (seconds // 60),
        "blue": {"id": "preview-blue", "name": "BLG", "gold": 14500 + progress * 1180,
                 "kills": 2 + progress // 2, "towers": min(5, progress // 2),
                 "drakes": min(2, progress // 4), "nashors": 0, "inhibitors": 0},
        "red": {"id": "preview-red", "name": "AL", "gold": 14000 + progress * 1040,
                "kills": 1 + progress // 3, "towers": min(3, progress // 3),
                "drakes": min(1, progress // 5), "nashors": 0, "inhibitors": 0}
    })


def build_preview_db(source, destination, include_live=True):
    if Path(source).exists():
        shutil.copy2(source, destination)
    if include_live:
        for seconds in range(120, 1201, 120):
            frame = demo_frame(seconds)
            save(destination, frame, predict(frame, BASELINE), BASELINE["kind"])


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--source-db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--no-live", action="store_true", help="preview the empty live-match state")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="lol-preview-") as folder:
        db = str(Path(folder) / "preview.sqlite3")
        build_preview_db(args.source_db, db, not args.no_live)
        collector = None
        if not args.no_live:
            collector = type("PreviewCollector", (), {"last_frame_at": None, "last_error": None})()
        server = ThreadingHTTPServer((args.host, args.port), make_handler(db, collector, BASELINE.copy()))
        print("local render preview: http://%s:%d" % (args.host, args.port), flush=True)
        print("temporary database:", db, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
