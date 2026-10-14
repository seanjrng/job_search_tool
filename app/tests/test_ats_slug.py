"""ats_slug is the company registry: load shape, availability, and check updates."""
import importlib.util
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from app.discover_companies import append_new_companies
from app.main import load_ats_slugs
from setup import ensure_ats_db

ROOT = Path(__file__).resolve().parents[2]


def _load_expand():
    path = ROOT / "ats-expansion" / "expand_companies.py"
    spec = importlib.util.spec_from_file_location("expand_companies", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(path: Path) -> sqlite3.Connection:
    ensure_ats_db(path)
    return sqlite3.connect(path)


def test_new_database_defaults_available_to_one(tmp_path):
    db_path = tmp_path / "ats_discovery.sqlite3"
    conn = _table(db_path)
    conn.execute(
        "INSERT INTO ats_slug (slug, ats_board, discovery_time) VALUES ('acme', 'greenhouse', 10)"
    )
    conn.commit()
    row = conn.execute(
        "SELECT available, last_seen, name FROM ats_slug WHERE slug = 'acme'"
    ).fetchone()
    conn.close()
    assert row == (1, None, None)


def test_load_ats_slugs_matches_company_dict_shape(tmp_path):
    db_path = tmp_path / "ats_discovery.sqlite3"
    conn = _table(db_path)
    conn.executemany(
        """
        INSERT INTO ats_slug (slug, ats_board, available, name)
        VALUES (?, ?, ?, ?)
        """,
        [
            ("stripe", "greenhouse", 1, "Stripe"),
            ("0g", "ashby", 1, None),
            ("gone", "lever", 0, "Gone Co"),
        ],
    )
    conn.commit()
    conn.close()

    loaded = load_ats_slugs(db_path)
    assert loaded == [
        {"name": "0g", "ats": "ashby", "slug": "0g"},
        {"name": "Stripe", "ats": "greenhouse", "slug": "stripe"},
    ]
    assert all(set(company) == {"name", "ats", "slug"} for company in loaded)


def test_append_new_companies_skips_existing_primary_key(tmp_path):
    db_path = tmp_path / "ats_discovery.sqlite3"
    conn = _table(db_path)
    conn.execute(
        "INSERT INTO ats_slug (slug, ats_board, name, discovery_time) VALUES ('affirm', 'greenhouse', 'Affirm', 5)"
    )
    conn.commit()
    conn.close()

    added = append_new_companies(
        [
            {"name": "Affirm Inc.", "ats": "greenhouse", "slug": "affirm"},
            {"name": "Some New Co", "ats": "lever", "slug": "somenewco"},
            {"name": "Some New Co", "ats": "lever", "slug": "somenewco"},
        ],
        path=db_path,
    )
    assert added == [{"name": "Some New Co", "ats": "lever", "slug": "somenewco"}]

    conn = sqlite3.connect(db_path)
    affirm = conn.execute(
        "SELECT name, discovery_time FROM ats_slug WHERE slug = 'affirm'"
    ).fetchone()
    created = conn.execute(
        "SELECT available, name FROM ats_slug WHERE slug = 'somenewco'"
    ).fetchone()
    conn.close()
    assert affirm == ("Affirm", 5)
    assert created == (1, "Some New Co")


def test_failed_check_clears_available_and_keeps_last_seen(tmp_path):
    expand = _load_expand()
    db_path = tmp_path / "ats_discovery.sqlite3"
    conn = _table(db_path)
    conn.execute(
        """
        INSERT INTO ats_slug (slug, ats_board, available, last_seen, name)
        VALUES ('missing', 'greenhouse', 1, 42, 'Missing')
        """
    )
    conn.commit()
    expand.mark_unavailable(conn, "greenhouse", "missing")
    conn.commit()
    failed = conn.execute(
        "SELECT available, last_seen FROM ats_slug WHERE slug = 'missing'"
    ).fetchone()
    expand.mark_seen(conn, "greenhouse", "missing", 99)
    conn.commit()
    # A later success stamp must not turn available back on by itself.
    still = conn.execute(
        "SELECT available, last_seen FROM ats_slug WHERE slug = 'missing'"
    ).fetchone()
    conn.close()
    assert failed == (0, 42)
    assert still == (0, 99)


def test_successful_check_sets_last_seen_and_leaves_available(tmp_path):
    expand = _load_expand()
    db_path = tmp_path / "ats_discovery.sqlite3"
    conn = _table(db_path)
    conn.execute(
        "INSERT INTO ats_slug (slug, ats_board, available) VALUES ('stripe', 'greenhouse', 1)"
    )
    conn.commit()
    expand.mark_seen(conn, "greenhouse", "stripe", 123)
    conn.commit()
    row = conn.execute(
        "SELECT available, last_seen FROM ats_slug WHERE slug = 'stripe'"
    ).fetchone()
    conn.close()
    assert row == (1, 123)


def test_url_check_waits_ten_seconds_before_each_retry():
    expand = _load_expand()

    class Response:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload or {}

        def json(self):
            return self._payload

    sleeps = []
    session = MagicMock()
    session.get.side_effect = [requests.ConnectionError("down"), Response(503), Response(404)]
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", sleeps.append):
        name, reason = expand.check("greenhouse", "acme")
    assert (name, reason, sleeps) == (None, "404", [10, 10])

    sleeps.clear()
    session.get.side_effect = None
    session.get.return_value = Response(200, {"name": "Acme"})
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", sleeps.append):
        name, reason = expand.check("greenhouse", "acme")
    assert (name, reason, sleeps) == ("Acme", None, [])
