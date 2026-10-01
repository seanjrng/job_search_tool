"""Digest content matches the review-board detail panel, highest score first."""
from pathlib import Path

from app import dedup
from app.digest import description_to_text, render_digest, write_digest


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
