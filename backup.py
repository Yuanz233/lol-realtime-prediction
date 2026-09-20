"""Create a consistent SQLite backup while the service is running."""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(root / "data" / "matches.sqlite3"))
    parser.add_argument("--output-dir", default=str(root / "backups"))
    args = parser.parse_args()
    source = Path(args.db)
    if not source.is_file():
        raise SystemExit("database does not exist: %s" % source)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ("matches-%s.sqlite3" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(destination)) as dst:
        src.backup(dst)
    print(destination)


if __name__ == "__main__":
    main()
