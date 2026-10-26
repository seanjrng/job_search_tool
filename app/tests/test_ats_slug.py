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


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_expand():
    return _load_module(ROOT / "ats-expansion" / "expand_companies.py", "expand_companies")


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
            ("nvidia.wd5/NVIDIAExternalCareerSite", "workday", 1, "NVIDIA"),
        ],
    )
    conn.commit()
    conn.close()

    loaded = load_ats_slugs(db_path)
    assert loaded == [
        {"name": "0g", "ats": "ashby", "slug": "0g"},
        {"name": "Stripe", "ats": "greenhouse", "slug": "stripe"},
        {
            "name": "NVIDIA",
            "ats": "workday",
            "slug": "nvidia.wd5/NVIDIAExternalCareerSite",
        },
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


def test_crawl_query_extracts_lever_and_workable_slugs(tmp_path):
    import duckdb

    discovery = _load_module(ROOT / "ats-expansion" / "ats_discovery.py", "ats_discovery")
    parquet = tmp_path / "index.parquet"
    con = duckdb.connect()
    con.execute(
        """
        COPY (
            SELECT * FROM (VALUES
                ('jobs.lever.co', '/Netflix/11111111-1111-1111-1111-111111111111', '', 200),
                ('jobs.eu.lever.co', '/EuCo/', '', 200),
                ('api.lever.co', '/v0/postings/spotify', '', 200),
                ('api.eu.lever.co', '/v0/postings/eucorp/abc', '', 200),
                ('jobs.lever.co', '/favicon.ico', '', 200),
                ('jobs.lever.co', '/logo.png', '', 200),
                ('apply.workable.com', '/Treewalk/j/ABC123', '', 200),
                ('apply.workable.com', '/api/v1/widget/accounts/seequent', '', 200),
                ('apply.workable.com', '/j/SHORTCODE', '', 200),
                ('treewalk.workable.com', '/backend', '', 200),
                ('www.workable.com', '/pricing', '', 200),
                ('jobs.workable.com', '/foo', '', 200),
                ('example.com', '/Netflix', '', 200),
                ('jobs.lever.co', '/hidden', '', 404)
            ) t(url_host_name, url_path, url_query, fetch_status)
        ) TO ?
        """,
        [str(parquet)],
    )
    rows = con.execute(discovery.QUERY, [str(parquet)]).fetchall()
    con.close()
    assert sorted(rows) == sorted(
        [
            ("lever", "netflix"),
            ("lever", "euco"),
            ("lever", "spotify"),
            ("lever", "eucorp"),
            ("workable", "treewalk"),
            ("workable", "seequent"),
        ]
    )


def test_crawl_query_extracts_workday_sites(tmp_path):
    import duckdb

    discovery = _load_module(ROOT / "ats-expansion" / "ats_discovery.py", "ats_discovery_workday")
    parquet = tmp_path / "index.parquet"
    con = duckdb.connect()
    con.execute(
        """
        COPY (
            SELECT * FROM (VALUES
                ('nvidia.wd5.myworkdayjobs.com', '/en-US/NVIDIAExternalCareerSite/job/Santa-Clara/Title_JR1', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs', '', 200),
                ('adobe.wd5.myworkdayjobs.com', '/external_experienced', '', 200),
                ('general-motors.wd12.myworkdayjobs.com', '/GM_Careers', '', 200),
                ('wd1.myworkdaysite.com', '/recruiting/salesforce/External_Career_Site', '', 200),
                ('wd3.myworkdaysite.com', '/en-US/recruiting/clio/Clio_Careers/job/x', '', 200),
                ('wd5.myworkdaysite.com', '/wday/cxs/nike/NikeCareers/jobs', '', 200),
                ('custom.example', '/wday/cxs/acme/External/jobs', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/wday/cxs/otherco/External/jobs', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/en-US', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/en-US/job/Title_JR1', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/robots.txt', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/logo.png', '', 200),
                ('www.wd5.myworkdayjobs.com', '/External', '', 200),
                ('jobs.lever.co', '/Netflix/11111111-1111-1111-1111-111111111111', '', 200),
                ('nvidia.wd5.myworkdayjobs.com', '/Hidden', '', 404)
            ) t(url_host_name, url_path, url_query, fetch_status)
        ) TO ?
        """,
        [str(parquet)],
    )
    rows = con.execute(discovery.QUERY, [str(parquet)]).fetchall()
    con.close()
    assert sorted(rows) == sorted(
        [
            ("lever", "netflix"),
            ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
            ("workday", "adobe.wd5/external_experienced"),
            ("workday", "general-motors.wd12/GM_Careers"),
            ("workday", "salesforce.wd1/External_Career_Site"),
            ("workday", "clio.wd3/Clio_Careers"),
            ("workday", "nike.wd5/NikeCareers"),
        ]
    )


def test_workday_site_spelling_is_kept_once(tmp_path):
    discovery = _load_module(ROOT / "ats-expansion" / "ats_discovery.py", "ats_discovery_case")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "a.txt").write_text(
        "workday\tnvidia.wd5/NVIDIAExternalCareerSite\nworkday\tadobe.wd5/external_experienced\n",
        encoding="utf-8",
    )
    (cache / "b.txt").write_text(
        "workday\tnvidia.wd5/nvidiaexternalcareersite\n",
        encoding="utf-8",
    )
    missing = tmp_path / "missing"
    rows = discovery.collect_slugs(cache=cache, legacy={"greenhouse": missing, "ashby": missing})
    assert sorted(rows) == sorted(
        [
            ("nvidia.wd5/NVIDIAExternalCareerSite", "workday"),
            ("adobe.wd5/external_experienced", "workday"),
        ]
    )


def test_load_slugs_includes_lever_and_workable(tmp_path):
    expand = _load_expand()
    conn = _table(tmp_path / "ats_discovery.sqlite3")
    conn.executemany(
        "INSERT INTO ats_slug (slug, ats_board, available) VALUES (?, ?, 1)",
        [
            ("acme", "lever"),
            ("treewalk", "workable"),
            ("nope", "greenhouse"),
            ("nvidia.wd5/NVIDIAExternalCareerSite", "workday"),
        ],
    )
    conn.execute(
        "UPDATE ats_slug SET available = 0 WHERE slug = 'nope'"
    )
    conn.commit()
    assert expand.load_slugs(conn) == [
        ("lever", "acme"),
        ("workable", "treewalk"),
        ("workday", "nvidia.wd5/NVIDIAExternalCareerSite"),
    ]
    conn.close()


def test_lever_us_404_checks_eu_without_waiting():
    expand = _load_expand()

    class Response:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    sleeps = []
    session = MagicMock()
    session.get.side_effect = [Response(404), Response(200, [])]
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", sleeps.append):
        name, reason = expand.check("lever", "euco")
    assert (name, reason, sleeps) == ("euco", None, [])
    assert session.get.call_args_list[0].args[0].startswith("https://api.lever.co/")
    assert session.get.call_args_list[1].args[0].startswith("https://api.eu.lever.co/")


def test_workday_check_posts_the_jobs_endpoint():
    expand = _load_expand()

    class Response:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    slug = "nvidia.wd5/NVIDIAExternalCareerSite"
    session = MagicMock()
    session.post.return_value = Response(200, {"total": 2, "jobPostings": [{"title": "Engineer"}]})
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", lambda _s: None):
        assert expand.check("workday", slug) == ("nvidia", None)
    posted = session.post.call_args
    assert posted.args[0] == (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert posted.kwargs["json"] == expand.WORKDAY_SEARCH
    session.post.return_value = Response(200, {"ok": True})
    with patch.object(expand, "session", return_value=session):
        assert expand.check("workday", slug) == (None, "unexpected-body")
    assert expand.check("workday", "not-a-workday-slug") == (None, "bad-slug")


def test_workday_404_tries_myworkdaysite_without_waiting():
    expand = _load_expand()

    class Response:
        def __init__(self, code, payload=None):
            self.status_code = code
            self.headers = {}
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    sleeps = []
    session = MagicMock()
    session.post.side_effect = [
        Response(404, {"message": "not found"}),
        Response(200, {"total": 0, "jobPostings": []}),
    ]
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", sleeps.append):
        assert expand.check("workday", "clio.wd3/Clio_Careers") == ("clio", None)
    assert sleeps == []
    assert session.post.call_args_list[0].args[0].startswith("https://clio.wd3.myworkdayjobs.com/")
    assert session.post.call_args_list[1].args[0] == (
        "https://wd3.myworkdaysite.com/wday/cxs/clio/Clio_Careers/jobs"
    )


def test_workable_check_requires_a_jobs_list():
    expand = _load_expand()

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self.headers = {}
            self._payload = payload

        def json(self):
            return self._payload

    session = MagicMock()
    session.get.return_value = Response({"name": "Treewalk", "jobs": []})
    with patch.object(expand, "session", return_value=session), patch.object(expand.time, "sleep", lambda _s: None):
        assert expand.check("workable", "treewalk") == ("Treewalk", None)
    session.get.return_value = Response({"ok": True})
    with patch.object(expand, "session", return_value=session):
        assert expand.check("workable", "treewalk") == (None, "unexpected-body")


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
