"""Prepare a checkout for the current pipeline.

Copies example config files when the real file is missing, installs Python
dependencies, and creates data/ats_discovery.sqlite3 with the ats_slug
table. Existing config files and an existing database are left in place.
Company boards live in that database, not in companies.yaml.
"""
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ATS_DB = ROOT / "data" / "ats_discovery.sqlite3"

CONFIG_COPIES = (
    (".env.example", ".env"),
    ("profile.example.yaml", "profile.yaml"),
    ("filters.yaml.example", "filters.yaml"),
    ("aggregators.yaml.example", "aggregators.yaml"),
)

ATS_SLUG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ats_slug(
    slug TEXT,
    ats_board TEXT,
    available INTEGER DEFAULT 1,
    last_seen INTEGER,
    discovery_time INTEGER,
    name TEXT,
    PRIMARY KEY(slug, ats_board)
)
"""


def copy_missing_configs() -> None:
    for source_name, dest_name in CONFIG_COPIES:
        source = ROOT / source_name
        dest = ROOT / dest_name
        if dest.exists():
            print(f"keep {dest_name}")
            continue
        if not source.exists():
            raise SystemExit(f"Missing template {source_name}")
        shutil.copy(source, dest)
        print(f"created {dest_name} from {source_name}")


def ensure_ats_db(path: Path = ATS_DB) -> None:
    """Create the discovery database and add columns the current code expects."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(ATS_SLUG_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ats_slug)")}
        if "name" not in columns:
            conn.execute("ALTER TABLE ats_slug ADD COLUMN name TEXT")
        conn.commit()
    finally:
        conn.close()


def install_requirements() -> None:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]
    )


def main() -> None:
    install_requirements()
    copy_missing_configs()
    ensure_ats_db()
    print(f"ats database ready at {ATS_DB}")
    print("Edit .env, profile.yaml, filters.yaml, and aggregators.yaml before the first run.")
    print("Company boards are read from data/ats_discovery.sqlite3 (table ats_slug).")
    print("The review board still needs: cd web && npm install")


if __name__ == "__main__":
    main()
