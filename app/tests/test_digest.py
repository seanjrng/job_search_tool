"""Digest content matches the review-board detail panel, highest score first."""
import sys
from pathlib import Path

import pytest

from app import dedup
from app.digest import (
    apply_digest_marks,
    description_to_text,
    main,
    refresh_digest,
    render_digest,
    write_digest,
)


def _job(url, company, title, location="Remote Canada", description="Build things.", passed=True):
    return {
        "url": url,
        "company": company,
        "title": title,
        "location": location,
        "description": description,
        "posted_at": None,
        "source": "test",
    }


def test_description_shapes():
    escaped = (
        "&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;li&gt;Two&lt;/li&gt;&lt;/ul&gt;"
    )
    text = description_to_text(escaped)
    assert "Hello & welcome" in text
    assert "- One" in text
    assert "- Two" in text
    assert "<" not in text
    assert "&lt;" not in text
    assert "&amp;" not in text

    assert description_to_text("<p>We use <b>Python</b>.</p>") == "We use Python."
    assert description_to_text("Line one\n\nLine two") == "Line one\n\nLine two"
    assert description_to_text("Q&amp;A team") == "Q&A team"
    assert description_to_text("") == ""
    assert description_to_text("   ") == ""


def test_digest_orders_by_score_and_matches_panel_fields(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    low = _job("https://example.com/low", "LowCo", "Backend Engineer", description="<p>Low JD</p>")
    high = _job(
        "https://example.com/high",
        "HighCo",
        "Staff Engineer",
        description="&lt;p&gt;High &amp;amp; JD&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Ship it&lt;/li&gt;&lt;/ul&gt;",
    )
    unscored = _job("https://example.com/new", "NewCo", "Software Engineer", description="")
    rejected = _job("https://example.com/nope", "NoCo", "Software Engineer", passed=False)

    with dedup.connect(db) as conn:
        for job, passed in ((low, True), (high, True), (unscored, True), (rejected, False)):
            dedup.save_details(conn, job, passed_filters=passed)
        dedup.save_evaluation(conn, low["url"], {
            "match_score": 40,
            "recommendation": "skip",
            "genuine_gaps": "Gap low",
            "transferable_strengths": "Strength low",
            "risk_factors": "Risk low",
        }, model="test")
        dedup.save_evaluation(conn, high["url"], {
            "match_score": 90,
            "recommendation": "apply",
            "genuine_gaps": "",
            "transferable_strengths": "Strength high",
            "risk_factors": "Risk high",
        }, model="test")
        dedup.save_user_status(conn, high["url"], "interview", "referred by Sam")
        # Same score as low: company name breaks the tie (Alpha before LowCo).
        tied = _job("https://example.com/tied", "AlphaCo", "Platform Engineer", description="Tied JD")
        dedup.save_details(conn, tied, passed_filters=True)
        dedup.save_evaluation(conn, tied["url"], {
            "match_score": 40,
            "recommendation": "consider",
            "genuine_gaps": "Gap tie",
            "transferable_strengths": "Strength tie",
            "risk_factors": "Risk tie",
        }, model="test")

    out = tmp_path / "digest.md"
    assert write_digest(db, out) == 3
    text = out.read_text(encoding="utf-8")

    assert text.index("## 90 · HighCo — Staff Engineer") < text.index("## 40 · AlphaCo — Platform Engineer")
    assert text.index("## 40 · AlphaCo — Platform Engineer") < text.index("## 40 · LowCo — Backend Engineer")
    assert "NewCo" not in text
    assert "NoCo" not in text

    def section(heading: str) -> str:
        start = text.index(heading)
        rest = text[start + len(heading):]
        nxt = rest.find("\n## ")
        return text[start:] if nxt < 0 else text[start:start + len(heading) + nxt]

    high_section = section("## 90 ·")

    assert "**Company:** HighCo" in high_section
    assert "**Location:** Remote Canada" in high_section
    assert "**AI status:** Apply" in high_section
    assert "**Score:** 90" in high_section
    assert "**My status:** Interview" in high_section
    assert "**Link:** https://example.com/high" in high_section
    assert "referred by Sam" in high_section
    assert "Strength high" in high_section
    assert "### Genuine gaps" in high_section
    assert "High & JD" in high_section
    assert "- Ship it" in high_section
    # Empty scored field still shows the section, with the panel's em dash.
    assert high_section.split("### Genuine gaps", 1)[1].split("###", 1)[0].strip() == "—"

    assert "highest score first" in render_digest([])
    assert "0 scored jobs" in render_digest([])
    assert "Set **My status:** to `Applied` or `Skipped`" in render_digest([])


def _eval(conn, url, score, recommendation="consider"):
    dedup.save_evaluation(conn, url, {
        "match_score": score,
        "recommendation": recommendation,
        "genuine_gaps": "Gap",
        "transferable_strengths": "Strength",
        "risk_factors": "Risk",
    }, model="test")


def _mark_status(text: str, url: str, status: str) -> str:
    """Replace the My status line in the section whose link is `url`."""
    idx = text.find(f"**Link:** {url}")
    assert idx >= 0, url
    # The job's own status line is the closest one above its link.
    previous = text.rfind("**My status:**", 0, idx)
    assert previous >= 0
    status_end = text.find("\n", previous)
    return text[:previous] + f"**My status:** {status}" + text[status_end:]


def test_applied_and_skipped_are_left_out(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    open_job = _job("https://example.com/open", "OpenCo", "Backend Engineer")
    applied = _job("https://example.com/applied", "AppliedCo", "Backend Engineer")
    skipped = _job("https://example.com/skipped", "SkipCo", "Backend Engineer")
    rejected = _job("https://example.com/rejected", "RejectCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        for job in (open_job, applied, skipped, rejected):
            dedup.save_details(conn, job, passed_filters=True)
            _eval(conn, job["url"], 70)
        dedup.save_user_status(conn, applied["url"], "applied", "sent Tuesday")
        dedup.save_user_status(conn, skipped["url"], "skipped", "comp too low")
        dedup.save_user_status(conn, rejected["url"], "rejected", "they passed")

    out = tmp_path / "digest.md"
    assert write_digest(db, out) == 2
    text = out.read_text(encoding="utf-8")
    assert "OpenCo" in text
    assert "RejectCo" in text
    assert "AppliedCo" not in text
    assert "SkipCo" not in text
    assert "2 excluded jobs (applied or not a fit)." in text
    assert "sent Tuesday" not in text


def test_digest_edits_exclude_and_keep_notes(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    applied = _job("https://example.com/applied", "AppliedCo", "Backend Engineer")
    skipped = _job("https://example.com/skipped", "SkipCo", "Backend Engineer")
    interview = _job("https://example.com/interview", "TalkCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        for job, status, notes in (
            (applied, None, "portal"),
            (skipped, None, "looks junior"),
            (interview, "interview", "onsite Thursday"),
        ):
            dedup.save_details(conn, job, passed_filters=True)
            _eval(conn, job["url"], 60)
            if status or notes:
                dedup.save_user_status(conn, job["url"], status, notes)

    out = tmp_path / "digest.md"
    write_digest(db, out)
    text = _mark_status(out.read_text(encoding="utf-8"), applied["url"], "Applied")
    text = _mark_status(text, skipped["url"], "not a good fit")
    # A description can repeat these lines. Only the metadata line counts.
    text = text.replace(
        "Build things.",
        "Build things.\n\n**My status:** Applied\n**Link:** https://example.com/decoy",
        1,
    )
    out.write_text(text, encoding="utf-8")

    missing = refresh_digest(db, out)
    assert missing == []
    rewritten = out.read_text(encoding="utf-8")
    assert "AppliedCo" not in rewritten
    assert "SkipCo" not in rewritten
    assert "TalkCo" in rewritten
    assert "1 scored job, highest score first." in rewritten
    assert "2 excluded jobs (applied or not a fit)." in rewritten

    with dedup.connect(db) as conn:
        assert dedup.get_user_status(conn, applied["url"])["my_status"] == "applied"
        assert dedup.get_user_status(conn, applied["url"])["notes"] == "portal"
        assert dedup.get_user_status(conn, skipped["url"])["my_status"] == "skipped"
        assert dedup.get_user_status(conn, skipped["url"])["notes"] == "looks junior"
        talk = dedup.get_user_status(conn, interview["url"])
        assert talk["my_status"] == "interview"
        assert talk["notes"] == "onsite Thursday"


def test_stale_digest_does_not_clear_an_exclusion(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    job = _job("https://example.com/applied", "AppliedCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        dedup.save_details(conn, job, passed_filters=True)
        _eval(conn, job["url"], 60)
    out = tmp_path / "digest.md"
    write_digest(db, out)
    with dedup.connect(db) as conn:
        dedup.set_my_status(conn, job["url"], "applied")

    result = apply_digest_marks(db, out.read_text(encoding="utf-8"))
    assert result == {"applied": 0, "skipped": 0, "warnings": []}
    with dedup.connect(db) as conn:
        assert dedup.get_user_status(conn, job["url"])["my_status"] == "applied"


def test_unrecognized_status_is_not_saved(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    job = _job("https://example.com/open", "OpenCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        dedup.save_details(conn, job, passed_filters=True)
        _eval(conn, job["url"], 60)
        dedup.save_user_status(conn, job["url"], "maybe", "hmm")
    out = tmp_path / "digest.md"
    write_digest(db, out)
    # Regenerating a status we wrote ourselves is quiet.
    assert apply_digest_marks(db, out.read_text(encoding="utf-8"))["warnings"] == []

    text = _mark_status(out.read_text(encoding="utf-8"), job["url"], "nope")
    result = apply_digest_marks(db, text)
    assert result["applied"] == 0
    assert result["skipped"] == 0
    assert any("Unrecognized" in warning for warning in result["warnings"])
    with dedup.connect(db) as conn:
        saved = dedup.get_user_status(conn, job["url"])
        assert saved["my_status"] == "maybe"
        assert saved["notes"] == "hmm"


def test_restore_puts_a_job_back_and_keeps_notes(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    job = _job("https://example.com/applied", "AppliedCo", "Backend Engineer")
    kept = _job("https://example.com/talk", "TalkCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        for row, status, notes in (
            (job, "applied", "portal"),
            (kept, "interview", "Thursday"),
        ):
            dedup.save_details(conn, row, passed_filters=True)
            _eval(conn, row["url"], 60)
            dedup.save_user_status(conn, row["url"], status, notes)
    out = tmp_path / "digest.md"
    assert write_digest(db, out) == 1

    missing = refresh_digest(db, out, restore=[job["url"], kept["url"]])
    assert missing == []
    text = out.read_text(encoding="utf-8")
    assert "AppliedCo" in text
    assert "portal" in text
    assert "TalkCo" in text
    with dedup.connect(db) as conn:
        assert dedup.get_user_status(conn, job["url"])["my_status"] is None
        assert dedup.get_user_status(conn, job["url"])["notes"] == "portal"
        assert dedup.get_user_status(conn, kept["url"])["my_status"] == "interview"


def test_cli_mark_and_unknown_url(tmp_path: Path, monkeypatch):
    db = str(tmp_path / "jobs.sqlite3")
    job = _job("https://example.com/open", "OpenCo", "Backend Engineer")
    with dedup.connect(db) as conn:
        dedup.save_details(conn, job, passed_filters=True)
        _eval(conn, job["url"], 60)
        dedup.save_user_status(conn, job["url"], None, "keep me")
    out = tmp_path / "digest.md"
    write_digest(db, out)

    missing = refresh_digest(db, out, skipped=[job["url"]])
    assert missing == []
    assert "OpenCo" not in out.read_text(encoding="utf-8")
    with dedup.connect(db) as conn:
        assert dedup.get_user_status(conn, job["url"])["my_status"] == "skipped"
        assert dedup.get_user_status(conn, job["url"])["notes"] == "keep me"

    monkeypatch.setattr(sys, "argv", [
        "digest", "--db", db, "--output", str(out), "--applied", "https://example.com/missing",
    ])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 1
    with dedup.connect(db) as conn:
        assert dedup.get_user_status(conn, job["url"])["my_status"] == "skipped"
