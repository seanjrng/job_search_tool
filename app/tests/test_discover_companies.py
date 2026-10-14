"""Sanity test for discover_companies.py — runs against a throwaway database,
never the real ats_discovery.sqlite3. Verifies known companies are never
re-inserted, duplicates within one run are collapsed, and a second run with
the same discoveries adds nothing.

Run with: python -m app.tests.test_discover_companies
"""
import os
import sqlite3

from app import discover_companies

TEST_DB = "data/test_ats_discovery.sqlite3"


def main():
    os.makedirs("data", exist_ok=True)
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    conn = sqlite3.connect(TEST_DB)
    conn.execute(
        """
        CREATE TABLE ats_slug(
            slug TEXT,
            ats_board TEXT,
            available INTEGER DEFAULT 1,
            last_seen INTEGER,
            discovery_time INTEGER,
            name TEXT,
            PRIMARY KEY(slug, ats_board)
        )
        """
    )
    conn.execute(
        "INSERT INTO ats_slug (slug, ats_board, name) VALUES ('affirm', 'greenhouse', 'Affirm')"
    )
    conn.execute(
        "INSERT INTO ats_slug (slug, ats_board, name) VALUES ('treewalk', 'workable', 'Treewalk')"
    )
    conn.commit()
    conn.close()

    known = discover_companies.load_known_keys(TEST_DB)
    assert ("greenhouse", "affirm") in known
    assert ("workable", "treewalk") in known
    print(f"load_known_keys OK: {len(known)} pairs loaded")

    discovered = [
        {"name": "Affirm Inc.", "ats": "greenhouse", "slug": "affirm"},
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},
        {"name": "Some New Co", "ats": "lever", "slug": "somenewco"},
    ]
    added = discover_companies.append_new_companies(discovered, path=TEST_DB)

    assert len(added) == 2, f"expected 2 new companies inserted, got {len(added)}: {added}"
    assert {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"} in added
    assert {"name": "Some New Co", "ats": "lever", "slug": "somenewco"} in added
    print("append_new_companies OK: correctly deduped known + within-call dupes, added 2")

    conn = sqlite3.connect(TEST_DB)
    rows = conn.execute(
        """
        SELECT name, ats_board, slug, available, last_seen, discovery_time
        FROM ats_slug
        WHERE slug IN ('warnermusicgroup', 'somenewco', 'affirm')
        ORDER BY slug
        """
    ).fetchall()
    conn.close()
    by_slug = {slug: (name, ats, available, last_seen, discovery_time) for name, ats, slug, available, last_seen, discovery_time in rows}
    assert by_slug["affirm"][0] == "Affirm"
    assert by_slug["warnermusicgroup"][0] == "Warner Music Group"
    assert by_slug["warnermusicgroup"][2] == 1
    assert by_slug["warnermusicgroup"][3] == by_slug["warnermusicgroup"][4]
    assert by_slug["somenewco"][1] == "lever"
    print("Inserted rows OK: available = 1, timestamps set, existing Affirm name kept")

    added_again = discover_companies.append_new_companies(discovered, path=TEST_DB)
    assert added_again == [], f"expected no new companies on second run, got {added_again}"
    print("Idempotency OK: re-running with the same discoveries adds nothing")

    os.remove(TEST_DB)
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
