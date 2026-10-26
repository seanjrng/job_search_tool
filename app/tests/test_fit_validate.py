"""Fit-score validation: sampling, labels, noise, agreement, and gates."""
from pathlib import Path

import pytest

from app import dedup
from app.ai_evaluate import failing_constraint
from app.fit_validate import (
    HELD_OUT_BANNER,
    NOISE_GUIDANCE,
    NOISE_MISSING,
    NUMERIC_LABEL,
    OVERALL_CAVEAT,
    Settings,
    assign_splits,
    build_report,
    classify_score,
    compile_gates,
    confusion,
    create_sample,
    disagreement_card,
    draw_sample,
    ensure_schema,
    evaluate_gates,
    filter_split,
    find_disagreements,
    fingerprint_warning,
    format_history,
    format_noise,
    format_sample_summary,
    gate_version,
    get_label,
    human_accepts,
    label_card,
    label_history,
    load_scored_jobs,
    low_kind_quotas,
    measure_noise,
    model_accepts,
    parse_label_response,
    parse_tag_response,
    pattern_guidance,
    pin_scores,
    prior_reports,
    profile_version,
    record_gates,
    rescore_jobs,
    run_label_session,
    run_tag_session,
    sanity_failures,
    save_label,
    save_report,
    save_tag,
    select_noise_jobs,
    spearman,
    spread_stats,
    stratum_quotas,
    summarize_noise,
    weighted_error,
)


class Sink:
    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def scripted(replies):
    items = iter(replies)

    def read(_prompt=""):
        try:
            return next(items)
        except StopIteration:
            raise EOFError

    return read


def add_job(conn, url, score, title, company="SecretCo", description="Build things.", location="Remote"):
    dedup.save_details(conn, {
        "url": url,
        "company": company,
        "title": title,
        "location": location,
        "description": description,
        "posted_at": None,
        "source": "test",
    }, True)
    dedup.save_evaluation(conn, url, {
        "match_score": score,
        "recommendation": "consider",
        "genuine_gaps": "gap text",
        "transferable_strengths": "strength text",
        "risk_factors": "risk text",
    }, "test-model")


def seed_bands(conn):
    number = 0
    for score in range(40, 55):
        add_job(conn, f"https://example.test/near-{number}", score, f"Near {number}")
        number += 1
    for number in range(8):
        add_job(conn, f"https://example.test/high-{number}", 80, f"High {number}")
        add_job(conn, f"https://example.test/floor-{number}", 5, f"Floor {number}")
        add_job(conn, f"https://example.test/midlow-{number}", 30, f"Midlow {number}")


def rich_settings(db):
    return Settings(
        cutoff=50, margin=10, target_size=20, random_size=6,
        seed=0, split_seed=0, db_path=db,
    )


def test_quotas_and_band_edges():
    assert stratum_quotas(80) == {"near": 48, "high": 16, "low": 16}
    assert stratum_quotas(61) == {"near": 37, "high": 12, "low": 12}
    assert low_kind_quotas(15, 50) == {"not_floor": 8, "floor": 7}
    settings = Settings(cutoff=50, margin=10)
    assert settings.floor_cut == 20
    assert classify_score(60, settings) == ("near", None)
    assert classify_score(61, settings) == ("high", None)
    assert classify_score(40, settings) == ("near", None)
    assert classify_score(39, settings) == ("low", "not_floor")
    assert classify_score(21, settings) == ("low", "not_floor")
    assert classify_score(20, settings) == ("low", "floor")


def test_short_stratum_is_not_backfilled():
    settings = Settings(cutoff=50, margin=10, target_size=10, random_size=4, seed=1)
    jobs = [{"url": f"near-{i}", "match_score": 40 + i, "title": f"Near {i}"} for i in range(10)]
    jobs += [{"url": f"floor-{i}", "match_score": 10, "title": f"Floor {i}"} for i in range(4)]
    drawn = draw_sample(jobs, settings)
    assert drawn["drawn"]["near"] == 6
    assert drawn["drawn"]["high"] == 0
    assert drawn["shortfalls"]["high"] == 2
    assert drawn["drawn"]["low_not_floor"] == 0
    assert drawn["shortfalls"]["low_not_floor"] == 1
    assert drawn["drawn"]["low_floor"] == 1
    urls = [row["url"] for row in drawn["rows"]]
    assert len(urls) == len(set(urls))
    text = format_sample_summary(drawn, settings)
    assert "shortfall 2" in text
    assert "Nothing is borrowed" in text


def test_sample_is_seeded_stratified_and_stable(tmp_path: Path):
    db = str(tmp_path / "jobs.sqlite3")
    settings = rich_settings(db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        seed_bands(conn)
        jobs = [
            {"url": row["url"], "match_score": row["match_score"], "title": row["title"]}
            for row in load_scored_jobs(conn)
        ]
        first = draw_sample(jobs, settings)
        second = draw_sample(jobs, settings)
        assert [(row["url"], row["stratum"], row["split"]) for row in first["rows"]] == [
            (row["url"], row["stratum"], row["split"]) for row in second["rows"]
        ]
        urls = [row["url"] for row in first["rows"]]
        assert len(urls) == len(set(urls))
        assert all(count == 0 for count in first["shortfalls"].values())
        groups = {}
        for row in first["rows"]:
            key = "low_" + row["low_kind"] if row["stratum"] == "low" else row["stratum"]
            groups.setdefault(key, {"tuning": 0, "held_out": 0})
            groups[key][row["split"]] += 1
        for counts in groups.values():
            assert counts["tuning"] >= 1
            assert counts["held_out"] >= 1
        by_score = sorted(first["rows"], key=lambda row: (row["score_at_sample"], row["url"]))
        assert [row["url"] for row in first["rows"]] != [row["url"] for row in by_score]

        created = create_sample(conn, settings)
        again = create_sample(conn, settings)
        assert created["created"] is True
        assert again["created"] is False
        assert {row["url"] for row in created["rows"]} == {row["url"] for row in again["rows"]}
        add_job(conn, "https://example.test/extra", 50, "Extra role")
        held = create_sample(conn, settings)
        assert "https://example.test/extra" not in {row["url"] for row in held["rows"]}


def test_split_seed_and_singleton_warning():
    rows = [{"url": f"u{i}", "stratum": "near", "low_kind": None} for i in range(8)]
    first, warnings = assign_splits(rows, Settings(split_seed=0))
    again, _warnings = assign_splits(rows, Settings(split_seed=0))
    assert first == again
    assert warnings == []
    assert "tuning" in first.values() and "held_out" in first.values()
    single, single_warnings = assign_splits(
        [{"url": "only", "stratum": "high", "low_kind": None}], Settings()
    )
    assert single_warnings == ["high"]
    assert single["only"] == "tuning"
    assert filter_split(
        [{"url": "a", "split": "tuning"}, {"url": "b", "split": "held_out"}], "tuning"
    ) == [{"url": "a", "split": "tuning"}]


def test_rank_confusion_sanity_and_disagreements():
    assert spearman([1, 2, 3], [1, 2, 3]) == pytest.approx(1)
    assert spearman([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert spearman([1, 1, 1], [0, 1, 2]) is None
    rows = [
        {"match_score": 80, "label": "no"},
        {"match_score": 40, "label": "strong_yes"},
        {"match_score": 90, "label": "strong_yes"},
        {"match_score": 30, "label": "no"},
    ]
    counts = confusion(rows, 50, "reject")
    assert counts == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    weighted = weighted_error(counts, 3)
    assert weighted["weighted_errors"] == 4
    assert weighted["weighted_error_rate"] == pytest.approx(0.5)
    maybe = {"match_score": 90, "label": "maybe"}
    assert confusion([maybe], 50, "reject")["fp"] == 1
    assert confusion([maybe], 50, "accept")["tp"] == 1
    gated = {"match_score": 90, "label": "strong_yes", "gate_passed": False}
    assert model_accepts(gated, 50, False) is True
    assert model_accepts(gated, 50, True) is False
    assert human_accepts("maybe", "reject") is False
    assert human_accepts("yes", "reject") is True
    assert human_accepts("strong_no", "accept") is False

    high_no, low_yes = sanity_failures([
        {"match_score": 95, "label": "no", "title": "Too high"},
        {"match_score": 95, "label": "strong_no", "title": "Way too high"},
        {"match_score": 10, "label": "strong_yes", "title": "Too low"},
        {"match_score": 10, "label": "yes", "title": "Also low"},
        {"match_score": 95, "label": "yes", "title": "Fine"},
        {"match_score": 10, "label": "maybe", "title": "Unsure"},
        {"match_score": 60, "label": "no", "title": "Edge"},
        {"match_score": 40, "label": "strong_yes", "title": "Edge low"},
    ], 50, 10)
    assert [row["title"] for row in high_no] == ["Too high", "Way too high"]
    assert [row["title"] for row in low_yes] == ["Too low", "Also low"]

    found = find_disagreements([
        {"url": "a", "match_score": 10, "label": "strong_yes", "title": "Low yes"},
        {"url": "b", "match_score": 50, "label": "maybe", "title": "Mid"},
        {"url": "c", "match_score": 90, "label": "no", "title": "High no"},
    ], 55, "reject", 0.40)
    reasons = {item["url"]: item["reasons"] for item in found}
    assert "decision" in reasons["a"] and "rank" in reasons["a"]
    assert "decision" in reasons["c"]
    assert "b" not in reasons
    assert "edit the profile" in pattern_guidance({"profile_gap": 4, "rubric_weighting": 1})
    assert "scoring prompt" in pattern_guidance({"rubric_weighting": 2, "hallucination": 2})
    assert "pass/fail gates" in pattern_guidance({"hard_constraint": 3})
    assert pattern_guidance({}) == "No cause tags yet."


def test_noise_summary_and_selection():
    stats = spread_stats([10, 20, 30])
    assert stats["mean"] == 20
    assert stats["stdev"] == 10
    assert stats["range"] == 20
    jobs = [{"url": str(i), "match_score": i} for i in range(10)]
    picked, shortfall = select_noise_jobs(jobs, 4)
    assert [job["match_score"] for job in picked] == [0, 3, 6, 9]
    assert shortfall == 0
    _picked, shortfall = select_noise_jobs(jobs, 12)
    assert shortfall == 2
    summary = summarize_noise([
        {"title": "Wide", "scores": [40, 70]},
        {"title": "Narrow", "scores": [10, 20, 30]},
    ], 55, 10)
    assert summary["per_jd"][0]["crosses"] is True
    assert summary["per_jd"][1]["crosses"] is False
    assert summary["guidance"] is True
    text = format_noise(summary)
    assert "CROSSES THE DECISION CUTOFF" in text
    assert NOISE_GUIDANCE in text
    assert "wider than the band" in text
    assert format_noise(None, missing=True) == NOISE_MISSING


def test_label_parser_card_and_session(tmp_path: Path):
    parsed = parse_label_response("y loves quilts")
    assert parsed["label"] == "yes" and parsed["note"] == "loves quilts"
    assert parse_label_response("strong yes the stack")["label"] == "strong_yes"
    assert parse_label_response("strong no location")["label"] == "strong_no"
    assert parse_label_response("1")["label"] == "strong_no"
    assert parse_label_response("2")["label"] == "no"
    assert parse_label_response("3")["label"] == "maybe"
    assert parse_label_response("4 remote ok")["label"] == "yes"
    assert parse_label_response("5")["label"] == "strong_yes"
    assert parse_label_response("sy")["label"] == "strong_yes"
    assert parse_label_response("sn: location")["note"] == "location"
    assert parse_label_response("n")["label"] == "no" and parse_label_response("n")["note"] is None
    assert parse_label_response("m: maybe later")["note"] == "maybe later"
    assert parse_label_response("80")["message"] == NUMERIC_LABEL
    assert parse_label_response("6")["action"] == "invalid"
    assert parse_label_response("s")["action"] == "skip"
    card = label_card("Quilt Engineer", "<p>Sew <b>quilts</b> daily.</p>")
    assert "Quilt Engineer" in card and "quilts" in card and "<b>" not in card
    assert "SecretCo" not in card

    db = str(tmp_path / "label.sqlite3")
    settings = Settings(cutoff=50, margin=10, target_size=10, random_size=0, seed=0, db_path=db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        add_job(conn, "https://example.test/a", 77, "Quilt Engineer", description="Sew the pipeline.")
        add_job(conn, "https://example.test/b", 77, "Loom Engineer", description="Keep the looms running.")
        create_sample(conn, settings)
        conn.execute("UPDATE fit_samples SET position = 0 WHERE url = ?", ("https://example.test/a",))
        conn.execute("UPDATE fit_samples SET position = 1 WHERE url = ?", ("https://example.test/b",))
        sink = Sink()
        run_label_session(conn, settings.sample_set_id, scripted(["80", "n", "b", "5 quilt", "m"]), sink)
        assert "77" not in sink.text
        assert "SecretCo" not in sink.text
        assert "Quilt Engineer" in sink.text and "Sew the pipeline." in sink.text
        assert NUMERIC_LABEL in sink.text
        assert get_label(conn, "https://example.test/a")["label"] == "strong_yes"
        assert get_label(conn, "https://example.test/a")["note"] == "quilt"
        assert get_label(conn, "https://example.test/a")["description"] == "Sew the pipeline."
        assert get_label(conn, "https://example.test/b")["label"] == "maybe"
        history = label_history(conn, "https://example.test/a")
        assert [item["label"] for item in history] == ["no", "strong_yes"]
        assert history[0]["recorded_at"] <= history[-1]["recorded_at"]
        assert get_label(conn, "https://example.test/a")["created_at"] == history[0]["recorded_at"]

        conn.execute(
            "UPDATE job_details SET description = ? WHERE url = ?",
            ("Replaced description the user never judged.", "https://example.test/a"),
        )
        sink = Sink()
        run_label_session(conn, settings.sample_set_id, scripted(["n domain"]), sink, revise=True)
        assert "Sew the pipeline." in sink.text
        assert "Replaced description" not in sink.text
        assert "77" not in sink.text and "SecretCo" not in sink.text
        assert get_label(conn, "https://example.test/a")["description"] == "Sew the pipeline."
        assert get_label(conn, "https://example.test/a")["label"] == "no"
        conn.execute("DELETE FROM ai_evaluations")
        assert get_label(conn, "https://example.test/a")["label"] == "no"


def test_skip_resumes_on_the_next_pass(tmp_path: Path):
    db = str(tmp_path / "skip.sqlite3")
    settings = Settings(cutoff=50, margin=10, target_size=4, random_size=0, seed=0, db_path=db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        add_job(conn, "https://example.test/a", 40, "First role", description="First jd.")
        add_job(conn, "https://example.test/b", 80, "Second role", description="Second jd.")
        create_sample(conn, settings)
        conn.execute("UPDATE fit_samples SET position = 0 WHERE url = ?", ("https://example.test/a",))
        conn.execute("UPDATE fit_samples SET position = 1 WHERE url = ?", ("https://example.test/b",))
        run_label_session(conn, settings.sample_set_id, scripted(["s", "y"]), Sink())
        assert get_label(conn, "https://example.test/a") is None
        assert get_label(conn, "https://example.test/b")["label"] == "yes"
        run_label_session(conn, settings.sample_set_id, scripted(["n"]), Sink())
        assert get_label(conn, "https://example.test/a")["label"] == "no"


def test_empty_sample_is_replaced_after_jobs_arrive(tmp_path: Path):
    db = str(tmp_path / "empty-sample.sqlite3")
    settings = Settings(cutoff=50, margin=10, target_size=4, random_size=0, seed=0, db_path=db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        first = create_sample(conn, settings)
        assert first["rows"] == []
        add_job(conn, "https://example.test/later", 80, "Later role")
        second = create_sample(conn, settings)
        assert second["created"] is True
        assert second["reset_held_out"] is False
        assert {row["url"] for row in second["rows"]} == {"https://example.test/later"}


def test_redraw_resets_split_and_keeps_labels(tmp_path: Path):
    db = str(tmp_path / "redraw.sqlite3")
    settings = rich_settings(db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        seed_bands(conn)
        create_sample(conn, settings)
        save_label(conn, "https://example.test/high-0", "High 0", "Remote", "Build things.", "no", None)
        redrawn = create_sample(conn, settings, redraw=True)
        assert redrawn["reset_held_out"] is True
        assert "resets the held-out" in format_sample_summary(redrawn, settings, reset_held_out=True)
        assert get_label(conn, "https://example.test/high-0")["label"] == "no"


def test_report_hides_held_out_company_and_compares_versions(tmp_path: Path):
    db = str(tmp_path / "report.sqlite3")
    settings = rich_settings(db)
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        seed_bands(conn)
        created = create_sample(conn, settings)
        for row in created["rows"]:
            label = "strong_yes" if row["score_at_sample"] >= 80 else "no" if row["score_at_sample"] <= 30 else "maybe"
            save_label(conn, row["url"], row["title"], "Remote", "Build things.", label, None)
        tuning_high = next(row for row in created["rows"] if row["split"] == "tuning" and row["stratum"] == "high")
        held_high = next(row for row in created["rows"] if row["split"] == "held_out" and row["stratum"] == "high")
        save_label(conn, tuning_high["url"], tuning_high["title"], "Remote", "Build things.", "no", "too senior")
        save_label(conn, held_high["url"], held_high["title"], "Remote", "Build things.", "no", "also too senior")
        report = build_report(conn, settings, "tuning", "prompt-a", "profile-a", None)
        assert OVERALL_CAVEAT in report["body"]
        assert NOISE_MISSING in report["body"]
        assert report["body"].index("Noise") < report["body"].index("Near-threshold")
        assert "Random control (only this stratum supports an overall accuracy claim)" in report["body"]
        assert "not an accuracy claim" in report["body"]
        assert "False negatives:" in report["body"]
        assert "SANITY: FAIL" in report["body"]
        assert tuning_high["title"] in report["body"]
        assert held_high["title"] not in report["body"]
        assert "SecretCo" not in report["body"]
        assert HELD_OUT_BANNER not in report["body"]
        assert "not pinned" in report["body"]
        save_report(conn, settings, report, "prompt-a", "profile-a")
        shifted = build_report(conn, settings, "tuning", "prompt-b", "profile-a", None)
        assert "not a new scoring run" in shifted["body"]
        save_report(conn, settings, shifted, "prompt-b", "profile-a")

        pin_scores(conn, [row["url"] for row in created["rows"]], "prompt-a", "profile-a")
        conn.execute("UPDATE ai_evaluations SET match_score = 99")
        pinned = build_report(conn, settings, "tuning", "prompt-a", "profile-a", None)
        assert "Scores are pinned" in pinned["body"]
        assert "99" not in pinned["body"]
        save_report(conn, settings, pinned, "prompt-a", "profile-a")
        history = format_history(prior_reports(conn, settings.sample_set_id))
        assert "prompt-a" in history and "prompt-b" in history
        held = build_report(conn, settings, "held_out", "prompt-a", "profile-a", None)
        assert HELD_OUT_BANNER in held["body"]
        assert held_high["title"] in held["body"]
        assert tuning_high["title"] not in held["body"]
        warning = fingerprint_warning(
            [{"scores_fingerprint": "abc", "prompt_version": "old"}], "abc", "new"
        )
        assert "not a new scoring run" in warning


def test_gates_change_the_reported_decision(tmp_path: Path):
    db = str(tmp_path / "gates.sqlite3")
    settings = Settings(cutoff=50, margin=10, target_size=10, random_size=0, seed=0, db_path=db)
    gates = compile_gates([
        {"name": "location", "location_allow": ["remote"]},
        {"name": "seniority", "title_deny": [r"\bintern\b"]},
        {"name": "work_authorization", "description_deny": [r"must be a u\.?s\.? citizen"]},
    ])
    austin = {"title": "Engineer", "location": "Austin, TX", "description": "Build"}
    intern = {"title": "Intern", "location": "Remote", "description": "Build"}
    citizen = {"title": "Engineer", "location": "Remote", "description": "<p>Must be a US citizen.</p>"}
    fine = {"title": "Engineer", "location": "Remote US", "description": "<p>Build <b>APIs</b>.</p>"}
    assert evaluate_gates(austin, gates)["failing_constraint"] == "location"
    assert evaluate_gates(intern, gates)["failing_constraint"] == "seniority"
    assert evaluate_gates(citizen, gates)["failing_constraint"] == "work_authorization"
    assert evaluate_gates(fine, gates)["passed"] is True
    assert failing_constraint(intern, gates) == "seniority"
    assert failing_constraint(fine, None) is None
    with pytest.raises(ValueError):
        compile_gates([{"name": "location", "locations_allow": ["remote"]}])
    assert gate_version(gates) != gate_version(compile_gates([{"name": "location", "location_allow": ["austin"]}]))

    with dedup.connect(db) as conn:
        ensure_schema(conn)
        add_job(conn, "https://example.test/gated", 80, "Gated role", description="Build things.", location="Austin, TX")
        add_job(conn, "https://example.test/open", 80, "Open role", description="Build things.")
        created = create_sample(conn, settings)
        for row in created["rows"]:
            if row["split"] != "tuning":
                conn.execute("UPDATE fit_samples SET split = 'held_out' WHERE url = ?", (row["url"],))
        conn.execute("UPDATE fit_samples SET split = 'tuning' WHERE url = ?", ("https://example.test/gated",))
        save_label(conn, "https://example.test/gated", "Gated role", "Austin, TX", "Build things.", "strong_yes", None)
        blocking = compile_gates([{"name": "location", "location_allow": ["nowhere-at-all"]}])
        record_gates(conn, [{
            "url": "https://example.test/gated",
            "title": "Gated role",
            "location": "Austin, TX",
            "description": "Build things.",
        }], blocking)
        report = build_report(conn, settings, "tuning", "prompt-a", "profile-a", blocking)
        before, after = report["body"].split("With hard-constraint gating", 1)
        assert "Human accept / model accept: 1" in before
        assert "Human accept / model reject: 1" in after
        assert "Gated: Gated role | location | score 80" in report["body"]
        assert "SecretCo" not in report["body"]
        unrecorded = build_report(conn, settings, "tuning", "prompt-a", "profile-a", compile_gates([
            {"name": "seniority", "title_deny": [r"\bdirector\b"]},
        ]))
        assert "not recorded" in unrecorded["body"]
        missing = build_report(conn, settings, "tuning", "prompt-a", "profile-a", None)
        assert "not configured" in missing["body"]


def test_noise_and_rescore_do_not_replace_production_scores(tmp_path: Path):
    db = str(tmp_path / "noise.sqlite3")
    settings = Settings(
        cutoff=55, margin=10, target_size=4, random_size=0, seed=0,
        low_not_floor_percent=0, db_path=db,
    )
    calls = {"n": 0}

    def score_fn(_job):
        calls["n"] += 1
        return {
            "match_score": 40 if calls["n"] % 2 else 70,
            "recommendation": "consider",
            "genuine_gaps": "gap text",
            "transferable_strengths": "strength text",
            "risk_factors": "risk text",
            "model": "test-model",
        }

    with dedup.connect(db) as conn:
        ensure_schema(conn)
        add_job(conn, "https://example.test/n", 15, "Noise role")
        add_job(conn, "https://example.test/s", 15, "Score role")
        create_sample(conn, settings)
        jobs = [{"url": "https://example.test/n", "title": "Noise role", "match_score": 15}]
        assert measure_noise(conn, jobs, 5, "prompt-a", "profile-a", score_fn) == 5
        assert measure_noise(conn, jobs, 5, "prompt-a", "profile-a", score_fn) == 0
        assert measure_noise(conn, jobs, 5, "prompt-a", "profile-a", score_fn, force=True) == 5
        stored = conn.execute(
            "SELECT match_score FROM ai_evaluations WHERE url = ?", ("https://example.test/n",)
        ).fetchone()[0]
        assert stored == 15
        from app import fit_validate
        summary = summarize_noise(fit_validate._noise_rows(conn, "prompt-a", "profile-a"), 55, 10)
        assert summary["per_jd"][0]["crosses"] is True
        report = build_report(conn, settings, "tuning", "prompt-a", "profile-a", None)
        assert NOISE_GUIDANCE in report["body"]
        assert "Noise role" in report["body"]

        rescore_jobs(conn, [{
            "url": "https://example.test/s",
            "company": "SecretCo",
            "title": "Score role",
            "location": "Remote",
            "description": "Build things.",
        }], "prompt-a", "profile-a", lambda _job: {
            "match_score": 42,
            "recommendation": "skip",
            "genuine_gaps": "gap text",
            "transferable_strengths": "strength text",
            "risk_factors": "risk text",
            "model": "test-model",
        })
        assert conn.execute(
            "SELECT match_score FROM ai_evaluations WHERE url = ?", ("https://example.test/s",)
        ).fetchone()[0] == 15
        assert fit_validate._snapshots(conn, "prompt-a", "profile-a")["https://example.test/s"]["match_score"] == 42


def test_cause_tags_and_profile_hash(tmp_path: Path):
    assert parse_tag_response("profile gap missing kubernetes") == {
        "action": "tag", "tag": "profile_gap", "note": "missing kubernetes",
    }
    assert parse_tag_response("3")["tag"] == "hallucination"
    assert parse_tag_response("5: needs a person")["tag"] == "other"
    assert parse_tag_response("other")["action"] == "invalid"
    with pytest.raises(ValueError):
        Settings(fn_weight=1)

    db = str(tmp_path / "tags.sqlite3")
    profile = tmp_path / "profile.yaml"
    profile.write_text("name: one\n", encoding="utf-8")
    first = profile_version(profile)
    profile.write_text("name: two\n", encoding="utf-8")
    assert profile_version(profile) != first
    assert profile_version(tmp_path / "missing.yaml") == "missing"

    card = disagreement_card({
        "title": "Quilt Engineer",
        "description": "Sew the pipeline.",
        "label": "no",
        "note": "domain",
        "match_score": 91,
        "recommendation": "apply",
        "transferable_strengths": "strength text",
        "genuine_gaps": "gap text",
        "risk_factors": "risk text",
        "reasons": ["decision", "rank"],
        "percentile": 0.9,
        "company": "SecretCo",
    }, 55, "reject")
    assert "SecretCo" not in card
    assert "91" in card and "gap text" in card and "domain" in card

    settings = Settings(cutoff=55, margin=10, db_path=db)
    row = {
        "url": "https://example.test/d",
        "title": "Quilt Engineer",
        "description": "Sew the pipeline.",
        "label": "no",
        "note": "domain",
        "match_score": 91,
        "recommendation": "apply",
        "transferable_strengths": "strength text",
        "genuine_gaps": "gap text",
        "risk_factors": "risk text",
        "reasons": ["decision"],
        "percentile": 0.9,
        "rank_gap_value": 0.7,
    }
    with dedup.connect(db) as conn:
        ensure_schema(conn)
        sink = Sink()
        run_tag_session(conn, [row], scripted(["1 the profile hid it"]), sink, settings, "prompt-a", "profile-a")
        assert "SecretCo" not in sink.text
        assert "91" in sink.text
        assert "edit the profile" in sink.text
        with pytest.raises(ValueError):
            save_tag(conn, row["url"], "prompt-a", "profile-a", "other", None)
        sink = Sink()
        run_tag_session(conn, [row], scripted(["q"]), sink, settings, "prompt-a", "profile-a")
        assert "No untagged disagreements" in sink.text
        assert "edit the profile" in sink.text


def test_report_command_requires_a_sample(tmp_path: Path):
    from app.fit_validate import main
    db = str(tmp_path / "empty.sqlite3")
    with pytest.raises(SystemExit):
        main(["report", "--db", db])
