"""Validate fit scores against the user's own five-point apply labels.

The model's scores are not ground truth. Labels live in their own tables
in the same SQLite file as the jobs, and they are not rewritten when the
prompt or profile changes.

Usage (from the repo root):

    python -m app.fit_validate              # sample, if needed, then label
    python -m app.fit_validate report       # tuning-set metrics
    python -m app.fit_validate report --held-out
    python -m app.fit_validate tag          # why each disagreement happened
    python -m app.fit_validate noise        # rescore a handful of JDs N times
    python -m app.fit_validate gates        # record pass/fail constraint checks
"""
import argparse
import hashlib
import json
import math
import random
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import dedup
from app import html_to_markdown

LABEL_ORDINAL = {"strong_no": 0, "no": 1, "maybe": 2, "yes": 3, "strong_yes": 4}
LABEL_NAMES = {
    "1": "strong_no",
    "sn": "strong_no",
    "strong_no": "strong_no",
    "2": "no",
    "n": "no",
    "no": "no",
    "3": "maybe",
    "m": "maybe",
    "maybe": "maybe",
    "4": "yes",
    "y": "yes",
    "yes": "yes",
    "5": "strong_yes",
    "sy": "strong_yes",
    "strong_yes": "strong_yes",
}
LABEL_PHRASES = (
    ("strong no", "strong_no"),
    ("strong yes", "strong_yes"),
)
ACCEPT_LABELS = {"yes", "strong_yes"}
REJECT_LABELS = {"no", "strong_no"}
EXPECTED_PERCENTILE = {
    "strong_no": 0.1,
    "no": 0.3,
    "maybe": 0.5,
    "yes": 0.7,
    "strong_yes": 0.9,
}
NEAR_PERCENT = 60
HIGH_PERCENT = 20
LOW_PERCENT = 20
CAUSE_TAGS = (
    "profile_gap",
    "rubric_weighting",
    "hallucination",
    "hard_constraint",
    "other",
)
TAG_ALIASES = {
    "1": "profile_gap",
    "profile": "profile_gap",
    "profile gap": "profile_gap",
    "profile_gap": "profile_gap",
    "2": "rubric_weighting",
    "rubric": "rubric_weighting",
    "weighting": "rubric_weighting",
    "rubric weighting": "rubric_weighting",
    "rubric_weighting": "rubric_weighting",
    "3": "hallucination",
    "hallucination": "hallucination",
    "hallucinated": "hallucination",
    "4": "hard_constraint",
    "hard": "hard_constraint",
    "gate": "hard_constraint",
    "hard constraint": "hard_constraint",
    "hard_constraint": "hard_constraint",
    "5": "other",
    "other": "other",
}
GATE_FIELDS = (
    "location_allow",
    "location_deny",
    "title_allow",
    "title_deny",
    "description_allow",
    "description_deny",
)
PATTERN_GUIDANCE = {
    "profile": "Mostly profile gap: edit the profile content.",
    "prompt": "Mostly rubric weighting or hallucination: edit the scoring prompt or rubric.",
    "gates": "Mostly hard constraint missed: introduce or fix pass/fail gates.",
    "other": "Mostly other: read the notes before changing the prompt or profile.",
}
OVERALL_CAVEAT = (
    "Near-threshold, clear-high, and clear-low figures are not overall accuracy. "
    "Only the random control supports an overall accuracy claim."
)
NOISE_GUIDANCE = (
    "Spread is large relative to the near-threshold margin, so borderline results "
    "are mostly noise. Fix noise before tuning the prompt: tighten the rubric, "
    "lower the randomness settings, or score each criterion separately and aggregate "
    "those scores deterministically instead of asking for one holistic number."
)
NOISE_MISSING = (
    "Noise has not been measured for this prompt and profile. "
    "Measure it before reading the accuracy numbers as signal."
)
HELD_OUT_BANNER = (
    "Held-out confirmation. Do not use these results to edit the prompt or profile."
)
NUMERIC_LABEL = (
    "Use 1 through 5: strong no, no, maybe, yes, strong yes. A 0-100 score is not a label."
)
LABEL_HELP = (
    "1 strong no, 2 or n no, 3 or m maybe, 4 or y yes, 5 or sy strong yes. "
    "A note can follow (4 the domain matches). s = skip, b = back, q = quit."
)
ORDER_SALT = 9176

SCHEMA = """
CREATE TABLE IF NOT EXISTS fit_sample_sets (
    sample_set_id TEXT PRIMARY KEY,
    seed INTEGER NOT NULL,
    target_size INTEGER NOT NULL,
    cutoff INTEGER NOT NULL,
    margin INTEGER NOT NULL,
    random_size INTEGER NOT NULL,
    split_seed INTEGER NOT NULL,
    tuning_percent INTEGER NOT NULL,
    low_not_floor_percent INTEGER NOT NULL,
    floor_cut INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    shortfall_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit_samples (
    sample_set_id TEXT NOT NULL,
    url TEXT NOT NULL,
    stratum TEXT NOT NULL,
    low_kind TEXT,
    split TEXT NOT NULL,
    position INTEGER NOT NULL,
    score_at_sample INTEGER NOT NULL,
    PRIMARY KEY (sample_set_id, url)
);
CREATE TABLE IF NOT EXISTS fit_labels (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    location TEXT,
    description TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit_label_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit_noise_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    run_index INTEGER NOT NULL,
    match_score INTEGER NOT NULL,
    recommendation TEXT,
    genuine_gaps TEXT,
    transferable_strengths TEXT,
    risk_factors TEXT,
    model TEXT,
    prompt_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fit_score_snapshots (
    url TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    match_score INTEGER NOT NULL,
    recommendation TEXT,
    genuine_gaps TEXT,
    transferable_strengths TEXT,
    risk_factors TEXT,
    model TEXT,
    source TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (url, prompt_version, profile_version)
);
CREATE TABLE IF NOT EXISTS fit_disagreement_tags (
    url TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    cause_tag TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (url, prompt_version, profile_version)
);
CREATE TABLE IF NOT EXISTS fit_gate_results (
    url TEXT NOT NULL,
    gate_version TEXT NOT NULL,
    passed INTEGER NOT NULL,
    failing_constraint TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (url, gate_version)
);
CREATE TABLE IF NOT EXISTS fit_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_set_id TEXT NOT NULL,
    split TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    gate_version TEXT,
    scores_fingerprint TEXT NOT NULL,
    body TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class Settings:
    def __init__(
        self,
        cutoff=55,
        margin=10,
        target_size=80,
        random_size=20,
        seed=0,
        split_seed=0,
        tuning_percent=50,
        low_not_floor_percent=50,
        maybe_as="reject",
        fn_weight=3.0,
        rank_gap=0.40,
        noise_count=12,
        noise_runs=5,
        profile_path="profile.yaml",
        gates_path="gates.yaml",
        db_path=None,
    ):
        if fn_weight <= 1:
            raise ValueError("fn_weight must be greater than 1")
        if not 1 <= tuning_percent <= 99:
            raise ValueError("tuning_percent must be from 1 to 99")
        if not 0 <= low_not_floor_percent <= 100:
            raise ValueError("low_not_floor_percent must be from 0 to 100")
        if margin < 0 or cutoff < 0 or target_size < 1 or random_size < 0:
            raise ValueError("cutoff, margin, target_size, and random_size are out of range")
        if maybe_as not in ("accept", "reject"):
            raise ValueError("maybe_as must be accept or reject")
        if not 0 <= rank_gap <= 1:
            raise ValueError("rank_gap must be from 0 to 1")
        if noise_count < 1 or noise_runs < 2:
            raise ValueError("noise_count must be at least 1 and noise_runs at least 2")
        self.cutoff = cutoff
        self.margin = margin
        self.target_size = target_size
        self.random_size = random_size
        self.seed = seed
        self.split_seed = split_seed
        self.tuning_percent = tuning_percent
        self.low_not_floor_percent = low_not_floor_percent
        self.maybe_as = maybe_as
        self.fn_weight = float(fn_weight)
        self.rank_gap = float(rank_gap)
        self.noise_count = noise_count
        self.noise_runs = noise_runs
        self.profile_path = profile_path
        self.gates_path = gates_path
        self.db_path = db_path or dedup.DB_PATH

    @property
    def floor_cut(self):
        # Scores at or below this, and below the near band, are the floor.
        # The rest of the low band is "low but not floor".
        return (self.cutoff - self.margin) // 2

    @property
    def sample_set_id(self):
        return (
            f"c{self.cutoff}-m{self.margin}-n{self.target_size}-r{self.random_size}"
            f"-seed{self.seed}-split{self.split_seed}-t{self.tuning_percent}"
            f"-lf{self.low_not_floor_percent}"
        )


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def stratum_quotas(target_size):
    """Integer shares of the stratified sample. Near/high/low, not the random control."""
    parts = (("near", NEAR_PERCENT), ("high", HIGH_PERCENT), ("low", LOW_PERCENT))
    base = {name: (target_size * weight) // 100 for name, weight in parts}
    left = target_size - sum(base.values())
    ranked = sorted(parts, key=lambda item: ((target_size * item[1]) % 100, item[1], item[0]), reverse=True)
    for name, _weight in ranked[:left]:
        base[name] += 1
    return base


def low_kind_quotas(low_count, low_not_floor_percent):
    not_floor = (low_count * low_not_floor_percent + 50) // 100
    not_floor = max(0, min(low_count, not_floor))
    return {"not_floor": not_floor, "floor": low_count - not_floor}


def classify_score(score, settings):
    low_edge = settings.cutoff - settings.margin
    high_edge = settings.cutoff + settings.margin
    if low_edge <= score <= high_edge:
        return "near", None
    if score > high_edge:
        return "high", None
    kind = "not_floor" if score > settings.floor_cut else "floor"
    return "low", kind


def _take(rng, pool, count):
    if count <= 0 or not pool:
        return []
    if len(pool) <= count:
        return list(pool)
    return rng.sample(pool, count)


def draw_sample(jobs, settings):
    """Stratified draw plus a separate uniform control. Short pools are not backfilled."""
    by_url = {}
    for job in jobs:
        if job.get("match_score") is None or not job.get("url"):
            continue
        by_url[job["url"]] = job
    ordered = sorted(by_url.values(), key=lambda job: job["url"])

    pools = {"near": [], "high": [], "low_floor": [], "low_not_floor": []}
    for job in ordered:
        stratum, kind = classify_score(job["match_score"], settings)
        if stratum == "low":
            pools["low_floor" if kind == "floor" else "low_not_floor"].append(job)
        else:
            pools[stratum].append(job)

    quotas = stratum_quotas(settings.target_size)
    low_quotas = low_kind_quotas(quotas["low"], settings.low_not_floor_percent)
    requested = {
        "near": quotas["near"],
        "high": quotas["high"],
        "low_floor": low_quotas["floor"],
        "low_not_floor": low_quotas["not_floor"],
        "random": settings.random_size,
    }
    available = {name: len(pool) for name, pool in pools.items()}

    rng = random.Random(settings.seed)
    chosen = []
    taken = set()
    plan = (
        ("near", "near", None),
        ("high", "high", None),
        ("low_not_floor", "low", "not_floor"),
        ("low_floor", "low", "floor"),
    )
    for pool_name, stratum, kind in plan:
        picked = _take(rng, pools[pool_name], requested[pool_name])
        for job in picked:
            taken.add(job["url"])
            chosen.append({
                "url": job["url"],
                "stratum": stratum,
                "low_kind": kind,
                "score_at_sample": job["match_score"],
                "title": job.get("title") or "",
            })

    remainder = [job for job in ordered if job["url"] not in taken]
    available["random"] = len(remainder)
    for job in _take(rng, remainder, settings.random_size):
        stratum, kind = classify_score(job["match_score"], settings)
        chosen.append({
            "url": job["url"],
            "stratum": "random",
            "low_kind": kind,
            "score_at_sample": job["match_score"],
            "title": job.get("title") or "",
        })

    drawn = {name: 0 for name in requested}
    for row in chosen:
        if row["stratum"] == "random":
            drawn["random"] += 1
        elif row["stratum"] == "low":
            drawn["low_floor" if row["low_kind"] == "floor" else "low_not_floor"] += 1
        else:
            drawn[row["stratum"]] += 1
    shortfalls = {name: requested[name] - drawn[name] for name in requested}

    order_rng = random.Random(settings.seed + ORDER_SALT)
    urls = [row["url"] for row in chosen]
    order_rng.shuffle(urls)
    position = {url: index for index, url in enumerate(urls)}
    for row in chosen:
        row["position"] = position[row["url"]]

    splits, split_warnings = assign_splits(chosen, settings)
    for row in chosen:
        row["split"] = splits[row["url"]]

    return {
        "rows": chosen,
        "requested": requested,
        "drawn": drawn,
        "available": available,
        "shortfalls": shortfalls,
        "split_warnings": split_warnings,
    }


def assign_splits(rows, settings):
    """Stable stratified split. A group of one JD can sit in only one half."""
    groups = {}
    for row in rows:
        if row["stratum"] == "low" and row.get("low_kind"):
            group = "low_" + row["low_kind"]
        else:
            group = row["stratum"]
        groups.setdefault(group, []).append(row["url"])

    rng = random.Random(settings.split_seed)
    assignments = {}
    warnings = []
    for group in sorted(groups):
        urls = sorted(groups[group])
        rng.shuffle(urls)
        count = len(urls)
        if count == 1:
            warnings.append(group)
            assignments[urls[0]] = "tuning" if settings.tuning_percent >= 50 else "held_out"
            continue
        tuning_n = (count * settings.tuning_percent + 50) // 100
        tuning_n = min(max(tuning_n, 1), count - 1)
        for index, url in enumerate(urls):
            assignments[url] = "tuning" if index < tuning_n else "held_out"
    return assignments, warnings


def format_sample_summary(result, settings, reset_held_out=False):
    lines = [
        f"Sample {settings.sample_set_id}",
        f"Cutoff {settings.cutoff}, margin {settings.margin}, floor at or below {settings.floor_cut}.",
        "Short pools are left short. Nothing is borrowed from another stratum.",
    ]
    if not 60 <= settings.target_size <= 100:
        lines.append("Target size is outside the recommended 60-100 range.")
    labels = (
        ("near", "Near-threshold"),
        ("high", "Clear highs"),
        ("low_not_floor", "Clear lows, low but not floor"),
        ("low_floor", "Clear lows, floor"),
        ("random", "Random control"),
    )
    for key, label in labels:
        lines.append(
            f"{label}: requested {result['requested'][key]}, "
            f"available {result['available'].get(key, 0)}, "
            f"drew {result['drawn'][key]}, shortfall {result['shortfalls'][key]}"
        )
    if result["split_warnings"]:
        joined = ", ".join(result["split_warnings"])
        lines.append(
            f"These groups have only one JD, so one half of the split does not contain them: {joined}."
        )
    if reset_held_out:
        lines.append("Redrawing resets the held-out guarantee.")
    else:
        lines.append("The tuning / held-out split is fixed for this sample. Redrawing resets it.")
    return "\n".join(lines)


def create_sample(conn, settings, redraw=False):
    ensure_schema(conn)
    sample_id = settings.sample_set_id
    existing = conn.execute(
        "SELECT shortfall_json, created_at FROM fit_sample_sets WHERE sample_set_id = ?",
        (sample_id,),
    ).fetchone()
    had_rows = False
    if existing and not redraw:
        rows = _load_sample_rows(conn, sample_id)
        # An empty draw is not a held-out split. Replace it once scored JDs exist.
        if rows or not load_scored_jobs(conn):
            payload = json.loads(existing[0])
            payload["rows"] = rows
            payload["created"] = False
            payload["reset_held_out"] = False
            return payload
        redraw = True
    elif existing and redraw:
        had_rows = bool(_load_sample_rows(conn, sample_id))

    drawn = draw_sample(load_scored_jobs(conn), settings)
    now = utc_now()
    stored = {
        "requested": drawn["requested"],
        "drawn": drawn["drawn"],
        "available": drawn["available"],
        "shortfalls": drawn["shortfalls"],
        "split_warnings": drawn["split_warnings"],
    }
    if existing and redraw:
        conn.execute("DELETE FROM fit_samples WHERE sample_set_id = ?", (sample_id,))
        conn.execute("DELETE FROM fit_sample_sets WHERE sample_set_id = ?", (sample_id,))
    conn.execute(
        "INSERT INTO fit_sample_sets "
        "(sample_set_id, seed, target_size, cutoff, margin, random_size, split_seed, "
        "tuning_percent, low_not_floor_percent, floor_cut, created_at, shortfall_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            sample_id, settings.seed, settings.target_size, settings.cutoff, settings.margin,
            settings.random_size, settings.split_seed, settings.tuning_percent,
            settings.low_not_floor_percent, settings.floor_cut, now, json.dumps(stored),
        ),
    )
    conn.executemany(
        "INSERT INTO fit_samples "
        "(sample_set_id, url, stratum, low_kind, split, position, score_at_sample) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sample_id, row["url"], row["stratum"], row["low_kind"], row["split"],
                row["position"], row["score_at_sample"],
            )
            for row in drawn["rows"]
        ],
    )
    drawn["created"] = True
    drawn["reset_held_out"] = bool(redraw and had_rows)
    return drawn


def _load_sample_rows(conn, sample_id):
    cur = conn.execute(
        "SELECT url, stratum, low_kind, split, position, score_at_sample "
        "FROM fit_samples WHERE sample_set_id = ? ORDER BY position",
        (sample_id,),
    )
    keys = ["url", "stratum", "low_kind", "split", "position", "score_at_sample"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def load_scored_jobs(conn):
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.description, "
        "ae.match_score, ae.recommendation, ae.genuine_gaps, "
        "ae.transferable_strengths, ae.risk_factors, ae.model "
        "FROM ai_evaluations ae JOIN job_details jd ON jd.url = ae.url "
        "WHERE ae.match_score IS NOT NULL"
    )
    keys = [
        "url", "company", "title", "location", "description", "match_score",
        "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors", "model",
    ]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def sample_progress(conn, sample_id):
    total = conn.execute(
        "SELECT COUNT(*) FROM fit_samples WHERE sample_set_id = ?", (sample_id,)
    ).fetchone()[0]
    labeled = conn.execute(
        "SELECT COUNT(*) FROM fit_samples s JOIN fit_labels l ON l.url = s.url "
        "WHERE s.sample_set_id = ?",
        (sample_id,),
    ).fetchone()[0]
    return {"total": total, "labeled": labeled, "unlabeled": total - labeled}


def save_label(conn, url, title, location, description, label, note):
    if label not in LABEL_ORDINAL:
        raise ValueError(f"Unknown label {label}")
    now = utc_now()
    existing = get_label(conn, url)
    if existing:
        title = existing["title"]
        location = existing["location"]
        description = existing["description"]
        created = existing["created_at"]
    else:
        created = now
    note = note.strip() if isinstance(note, str) else note
    note = note or None
    conn.execute(
        "INSERT INTO fit_labels (url, title, location, description, label, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, location=excluded.location, "
        "description=excluded.description, label=excluded.label, note=excluded.note, "
        "created_at=excluded.created_at, updated_at=excluded.updated_at",
        (url, title or "", location or "", description or "", label, note, created, now),
    )
    conn.execute(
        "INSERT INTO fit_label_history (url, label, note, recorded_at) VALUES (?, ?, ?, ?)",
        (url, label, note, now),
    )
    conn.commit()


def get_label(conn, url):
    row = conn.execute(
        "SELECT url, title, location, description, label, note, created_at, updated_at "
        "FROM fit_labels WHERE url = ?",
        (url,),
    ).fetchone()
    if row is None:
        return None
    keys = ["url", "title", "location", "description", "label", "note", "created_at", "updated_at"]
    return dict(zip(keys, row))


def label_history(conn, url):
    cur = conn.execute(
        "SELECT label, note, recorded_at FROM fit_label_history WHERE url = ? ORDER BY id",
        (url,),
    )
    return [{"label": row[0], "note": row[1], "recorded_at": row[2]} for row in cur.fetchall()]


def label_card(title, description, location=""):
    """Title, location, and JD. The company name and the model score are not on the card."""
    body = html_to_markdown.html_to_markdown(description or "") or "(no JD text)"
    place = f"Location: {location}\n" if location else ""
    return f"Title: {title}\n{place}\n{body}"


def parse_label_response(line):
    text = (line or "").strip()
    if not text:
        return {"action": "invalid", "message": LABEL_HELP}
    lower = text.lower()
    commands = {
        "q": "quit", "quit": "quit", "s": "skip", "skip": "skip",
        "b": "back", "back": "back", "?": "help", "help": "help",
    }
    if lower in commands:
        action = commands[lower]
        if action == "help":
            return {"action": "help", "message": LABEL_HELP}
        return {"action": action}
    if re.fullmatch(r"\d+", text) and text not in {"1", "2", "3", "4", "5"}:
        return {"action": "invalid", "message": NUMERIC_LABEL}
    phrases = LABEL_PHRASES
    for phrase, label in phrases:
        if lower == phrase or lower.startswith(phrase + " ") or lower.startswith(phrase + ":"):
            note = text[len(phrase):].lstrip(" :") or None
            return {"action": "label", "label": label, "note": note}
    for name, label in sorted(LABEL_NAMES.items(), key=lambda item: len(item[0]), reverse=True):
        if lower == name or lower.startswith(name + " ") or lower.startswith(name + ":"):
            note = text[len(name):].lstrip(" :") or None
            return {"action": "label", "label": label, "note": note}
    return {"action": "invalid", "message": LABEL_HELP}


def _label_queue(conn, sample_id, revise):
    labeled_clause = "AND l.url IS NOT NULL" if revise else "AND l.url IS NULL"
    cur = conn.execute(
        "SELECT s.url, s.position, jd.title, jd.location, jd.description, "
        "l.label, l.note, l.title, l.location, l.description "
        "FROM fit_samples s "
        "JOIN job_details jd ON jd.url = s.url "
        f"LEFT JOIN fit_labels l ON l.url = s.url WHERE s.sample_set_id = ? {labeled_clause} "
        "ORDER BY s.position",
        (sample_id,),
    )
    rows = []
    for url, position, title, location, description, label, note, labeled_title, labeled_location, labeled_description in cur.fetchall():
        if revise and label:
            title = labeled_title or title
            location = labeled_location if labeled_location is not None else location
            description = labeled_description if labeled_description is not None else description
        rows.append({
            "url": url,
            "position": position,
            "title": title or "",
            "location": location or "",
            "description": description or "",
            "label": label,
            "note": note,
        })
    return rows


def run_label_session(conn, sample_id, reader, writer, revise=False):
    items = _label_queue(conn, sample_id, revise)
    if not items:
        writer("Nothing left to label in this sample." if not revise else "No labeled JDs to revise.")
        return
    progress = sample_progress(conn, sample_id)
    writer(f"{progress['labeled']} labeled, {progress['unlabeled']} unlabeled, {progress['total']} in the sample.")
    index = 0
    while index < len(items):
        item = items[index]
        writer("")
        writer(f"--- {index + 1} of {len(items)} ---")
        writer(label_card(item["title"], item["description"], item.get("location") or ""))
        if revise and item["label"]:
            note = f" ({item['note']})" if item["note"] else ""
            writer(f"Current label: {item['label'].replace('_', ' ')}{note}")
        writer(LABEL_HELP)
        while True:
            try:
                line = reader("Label: ")
            except EOFError:
                writer("Stopped. Labels already saved are kept.")
                return
            parsed = parse_label_response(line)
            if parsed["action"] in {"invalid", "help"}:
                writer(parsed["message"])
                continue
            break
        action = parsed["action"]
        if action == "quit":
            writer("Stopped. Labels already saved are kept.")
            return
        if action == "back":
            index = max(0, index - 1)
            continue
        if action == "skip":
            index += 1
            continue
        save_label(
            conn, item["url"], item["title"], item["location"], item["description"],
            parsed["label"], parsed["note"],
        )
        index += 1
    writer("Reached the end of this pass.")


def prompt_version():
    import inspect

    from app import ai_evaluate

    parts = [
        ai_evaluate.SYSTEM_PROMPT,
        json.dumps(ai_evaluate.EVALUATION_SCHEMA, sort_keys=True),
        ai_evaluate.MODEL,
    ]
    try:
        parts.append(inspect.getsource(ai_evaluate.build_user_prompt))
    except OSError:
        pass
    return content_hash("\n".join(parts))


def profile_version(path):
    file_path = Path(path)
    if not file_path.exists():
        return "missing"
    return hashlib.sha256(file_path.read_bytes()).hexdigest()[:12]


def compile_gates(gates):
    compiled = []
    for gate in gates or []:
        if not isinstance(gate, dict) or not str(gate.get("name") or "").strip():
            raise ValueError("Every gate needs a name")
        unknown = [key for key in gate if key != "name" and key not in GATE_FIELDS]
        if unknown:
            raise ValueError(f"Gate {gate['name']} has unknown fields: {', '.join(unknown)}")
        item = {"name": str(gate["name"]).strip()}
        for field in GATE_FIELDS:
            patterns = gate.get(field) or []
            if isinstance(patterns, str):
                patterns = [patterns]
            compiled_patterns = []
            for pattern in patterns:
                try:
                    compiled_patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error as exc:
                    raise ValueError(f"Gate {item['name']} has an invalid pattern {pattern!r}") from exc
            item[field] = list(patterns)
            item["_" + field] = compiled_patterns
        compiled.append(item)
    return compiled


def load_gates(path="gates.yaml"):
    file_path = Path(path)
    if not file_path.exists():
        return None
    with open(file_path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return compile_gates(data.get("gates") or [])


def gate_version(gates):
    public = [{key: value for key, value in gate.items() if not key.startswith("_")} for gate in gates or []]
    return content_hash(json.dumps(public, sort_keys=True))


def _gated_text(job, field):
    if field.startswith("description"):
        raw = job.get("description") or ""
        converted = html_to_markdown.html_to_markdown(raw) or raw
        return converted
    if field.startswith("title"):
        return job.get("title") or ""
    return job.get("location") or ""


def evaluate_gates(job, gates):
    if not gates:
        return {"passed": True, "failing_constraint": None}
    for gate in gates:
        name = gate["name"]
        for field in GATE_FIELDS:
            patterns = gate.get("_" + field) or []
            if not patterns:
                continue
            text = _gated_text(job, field)
            hit = any(pattern.search(text) for pattern in patterns)
            if field.endswith("_allow") and not hit:
                return {"passed": False, "failing_constraint": name}
            if field.endswith("_deny") and hit:
                return {"passed": False, "failing_constraint": name}
    return {"passed": True, "failing_constraint": None}


def save_gate_result(conn, url, version, outcome):
    conn.execute(
        "INSERT INTO fit_gate_results (url, gate_version, passed, failing_constraint, recorded_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(url, gate_version) DO UPDATE SET passed=excluded.passed, "
        "failing_constraint=excluded.failing_constraint, recorded_at=excluded.recorded_at",
        (url, version, 1 if outcome["passed"] else 0, outcome.get("failing_constraint"), utc_now()),
    )


def get_gate_result(conn, url, version):
    row = conn.execute(
        "SELECT passed, failing_constraint FROM fit_gate_results WHERE url = ? AND gate_version = ?",
        (url, version),
    ).fetchone()
    if row is None:
        return None
    return {"passed": bool(row[0]), "failing_constraint": row[1]}


def record_gate(conn, job, gates, version=None):
    version = version or gate_version(gates)
    outcome = evaluate_gates(job, gates)
    save_gate_result(conn, job["url"], version, outcome)
    return outcome


def record_gates(conn, jobs, gates):
    version = gate_version(gates)
    failed = 0
    for job in jobs:
        outcome = evaluate_gates(job, gates)
        save_gate_result(conn, job["url"], version, outcome)
        if not outcome["passed"]:
            failed += 1
    conn.commit()
    return {"gate_version": version, "checked": len(jobs), "failed": failed}


def select_noise_jobs(jobs, count):
    ordered = sorted(
        [job for job in jobs if job.get("match_score") is not None],
        key=lambda job: (job["match_score"], job["url"]),
    )
    if not ordered:
        return [], count
    if len(ordered) <= count:
        return ordered, count - len(ordered)
    if count == 1:
        return [ordered[(len(ordered) - 1) // 2]], 0
    picked = []
    seen = set()
    last = len(ordered) - 1
    for index in range(count):
        cursor = (index * last) // (count - 1)
        if cursor in seen:
            continue
        seen.add(cursor)
        picked.append(ordered[cursor])
    return picked, count - len(picked)


def spread_stats(scores):
    values = [int(score) for score in scores]
    stdev = statistics.stdev(values) if len(values) >= 2 else None
    return {
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "stdev": stdev,
        "range": max(values) - min(values),
    }


def crosses_cutoff(scores, cutoff):
    return min(scores) < cutoff <= max(scores)


def summarize_noise(records, cutoff, margin):
    per_jd = []
    ranges = []
    for record in records:
        stats = spread_stats(record["scores"])
        stats["title"] = record["title"]
        stats["crosses"] = crosses_cutoff(record["scores"], cutoff)
        per_jd.append(stats)
        ranges.append(stats["range"])
    average = statistics.fmean(ranges) if ranges else None
    worst = max(ranges) if ranges else None
    band_width = 2 * margin
    fraction = None if average is None or band_width == 0 else average / band_width
    return {
        "per_jd": per_jd,
        "average_spread": average,
        "worst_spread": worst,
        "band_width": band_width,
        "fraction_of_band": fraction,
        "guidance": average is not None and margin > 0 and average >= margin,
        "crossing_titles": [item["title"] for item in per_jd if item["crosses"]],
    }


def format_noise(summary, missing=False, other_versions=None):
    if missing or not summary or not summary["per_jd"]:
        return NOISE_MISSING
    lines = ["Noise (repeated scores, not a cached copy of the stored score)"]
    for item in summary["per_jd"]:
        stdev = "undefined" if item["stdev"] is None else f"{item['stdev']:.2f}"
        flag = " CROSSES THE DECISION CUTOFF" if item["crosses"] else ""
        lines.append(
            f"- {item['title']}: mean {item['mean']:.1f}, min {item['min']}, "
            f"max {item['max']}, stdev {stdev}, range {item['range']}{flag}"
        )
    lines.append(
        f"Average spread {summary['average_spread']:.1f}. "
        f"Worst-case spread {summary['worst_spread']}."
    )
    if summary["band_width"] == 0:
        lines.append("The near-threshold margin is 0, so the band has no width.")
    else:
        percent = summary["fraction_of_band"] * 100
        wider = " The noise is wider than the band." if summary["fraction_of_band"] > 1 else ""
        lines.append(
            f"Observed noise width covers {percent:.0f}% of the near-threshold band.{wider}"
        )
    if summary["crossing_titles"]:
        lines.append(
            "Unstable at the cutoff: " + "; ".join(summary["crossing_titles"])
        )
    if summary["guidance"]:
        lines.append(NOISE_GUIDANCE)
    for version in other_versions or []:
        lines.append(
            f"Other version {version['prompt_version']} / {version['profile_version']}: "
            f"average spread {version['average_spread']:.1f} (not this prompt and profile)."
        )
    return "\n".join(lines)


def _noise_rows(conn, prompt_version, profile_version):
    cur = conn.execute(
        "SELECT n.url, jd.title, n.run_index, n.match_score "
        "FROM fit_noise_runs n JOIN job_details jd ON jd.url = n.url "
        "WHERE n.prompt_version = ? AND n.profile_version = ? "
        "ORDER BY n.url, n.run_index",
        (prompt_version, profile_version),
    )
    grouped = {}
    for url, title, _run_index, score in cur.fetchall():
        bucket = grouped.setdefault(url, {"url": url, "title": title or "", "scores": []})
        bucket["scores"].append(score)
    return list(grouped.values())


def measure_noise(conn, jobs, runs, prompt_version, profile_version, score_fn, force=False):
    """Score each JD `runs` times. Does not write production ai_evaluations."""
    ensure_schema(conn)
    if force:
        for job in jobs:
            conn.execute(
                "DELETE FROM fit_noise_runs WHERE url = ? AND prompt_version = ? AND profile_version = ?",
                (job["url"], prompt_version, profile_version),
            )
        conn.commit()
    calls = 0
    for job in jobs:
        have = conn.execute(
            "SELECT COUNT(*) FROM fit_noise_runs WHERE url = ? AND prompt_version = ? AND profile_version = ?",
            (job["url"], prompt_version, profile_version),
        ).fetchone()[0]
        for run_index in range(have, runs):
            evaluation = score_fn(job)
            calls += 1
            conn.execute(
                "INSERT INTO fit_noise_runs "
                "(url, run_index, match_score, recommendation, genuine_gaps, transferable_strengths, "
                "risk_factors, model, prompt_version, profile_version, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["url"], run_index, int(evaluation["match_score"]),
                    evaluation.get("recommendation"), evaluation.get("genuine_gaps"),
                    evaluation.get("transferable_strengths"), evaluation.get("risk_factors"),
                    evaluation.get("model"), prompt_version, profile_version, utc_now(),
                ),
            )
            conn.commit()
    return calls


def spearman(xs, ys):
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    return _pearson(rx, ry)


def _average_ranks(values):
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(values):
        end = cursor
        while end + 1 < len(values) and values[order[end + 1]] == values[order[cursor]]:
            end += 1
        average = (cursor + end) / 2 + 1
        for index in range(cursor, end + 1):
            ranks[order[index]] = average
        cursor = end + 1
    return ranks


def _pearson(xs, ys):
    count = len(xs)
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(xs, ys))
    den_x = math.sqrt(sum((a - mean_x) ** 2 for a in xs))
    den_y = math.sqrt(sum((b - mean_y) ** 2 for b in ys))
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def human_accepts(label, maybe_as):
    """Yes and strong yes accept. No and strong no reject. Maybe follows maybe_as."""
    if label in ACCEPT_LABELS:
        return True
    if label in REJECT_LABELS:
        return False
    if label == "maybe":
        return maybe_as == "accept"
    raise ValueError(f"Unknown label {label}")


def model_accepts(row, cutoff, use_gates):
    if use_gates and row.get("gate_passed") is False:
        return False
    return row["match_score"] >= cutoff


def confusion(rows, cutoff, maybe_as, use_gates=False):
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    for row in rows:
        predicted = model_accepts(row, cutoff, use_gates)
        actual = human_accepts(row["label"], maybe_as)
        if predicted and actual:
            counts["tp"] += 1
        elif predicted and not actual:
            counts["fp"] += 1
        elif not predicted and actual:
            counts["fn"] += 1
        else:
            counts["tn"] += 1
    return counts


def weighted_error(counts, fn_weight):
    weighted = fn_weight * counts["fn"] + counts["fp"]
    denominator = fn_weight * (counts["tp"] + counts["fn"]) + (counts["tn"] + counts["fp"])
    rate = None if denominator == 0 else weighted / denominator
    return {"weighted_errors": weighted, "weighted_error_rate": rate}


def metric_block(rows, cutoff, maybe_as, fn_weight, use_gates=False):
    if not rows:
        return {"n": 0, "spearman": None, "confusion": None, "weighted": None}
    counts = confusion(rows, cutoff, maybe_as, use_gates)
    return {
        "n": len(rows),
        "spearman": spearman(
            [row["match_score"] for row in rows],
            [LABEL_ORDINAL[row["label"]] for row in rows],
        ),
        "confusion": counts,
        "weighted": weighted_error(counts, fn_weight),
    }


def percentile_rank(score, scores):
    count = len(scores)
    below = sum(1 for value in scores if value < score)
    ties = sum(1 for value in scores if value == score)
    return (below + 0.5 * ties) / count


def find_disagreements(rows, cutoff, maybe_as, rank_gap):
    if not rows:
        return []
    scores = [row["match_score"] for row in rows]
    found = []
    for row in rows:
        reasons = []
        predicted = model_accepts(row, cutoff, False)
        actual = human_accepts(row["label"], maybe_as)
        if predicted != actual:
            reasons.append("decision")
        rank = percentile_rank(row["match_score"], scores)
        expected = EXPECTED_PERCENTILE[row["label"]]
        gap = abs(rank - expected)
        if gap > rank_gap:
            reasons.append("rank")
        if not reasons:
            continue
        item = dict(row)
        item["reasons"] = reasons
        item["percentile"] = rank
        item["rank_gap_value"] = gap
        found.append(item)
    return found


def sanity_failures(rows, cutoff, margin):
    high_no = []
    low_yes = []
    for row in rows:
        score = row["match_score"]
        if score > cutoff + margin and row["label"] in REJECT_LABELS:
            high_no.append(row)
        if score < cutoff - margin and row["label"] in ACCEPT_LABELS:
            low_yes.append(row)
    return high_no, low_yes


def score_histogram(scores):
    bins = []
    for start in range(0, 100, 10):
        end = 100 if start == 90 else start + 9
        count = sum(1 for score in scores if start <= score <= end)
        bins.append((start, end, count))
    return bins


def score_percentiles(scores):
    if not scores:
        return {}
    ordered = sorted(scores)
    result = {}
    for pct in (10, 25, 50, 75, 90):
        cursor = (len(ordered) - 1) * pct / 100
        low = math.floor(cursor)
        high = min(low + 1, len(ordered) - 1)
        frac = cursor - low
        result[pct] = ordered[low] * (1 - frac) + ordered[high] * frac
    return result


def pattern_guidance(counts):
    buckets = {
        "profile": counts.get("profile_gap", 0),
        "prompt": counts.get("rubric_weighting", 0) + counts.get("hallucination", 0),
        "gates": counts.get("hard_constraint", 0),
        "other": counts.get("other", 0),
    }
    top = max(buckets.values()) if buckets else 0
    if top <= 0:
        return "No cause tags yet."
    return " ".join(PATTERN_GUIDANCE[name] for name, value in buckets.items() if value == top)


def scores_fingerprint(rows):
    pairs = [f"{row['url']}:{row['match_score']}" for row in sorted(rows, key=lambda item: item["url"])]
    return content_hash("\n".join(pairs))


def filter_split(rows, split):
    return [row for row in rows if row.get("split") == split]


def _fmt_spearman(value):
    if value is None:
        return "undefined"
    return f"{value:.3f}"


def _fmt_rate(value):
    if value is None:
        return "undefined"
    return f"{value:.3f}"


def format_metric_block(title, block, fn_weight, maybe_as, cutoff):
    if block["n"] == 0:
        return f"{title}\n  No labeled JDs."
    counts = block["confusion"]
    weighted = block["weighted"]
    maybe = "accept" if maybe_as == "accept" else "reject"
    return "\n".join([
        title,
        f"  Rank agreement (Spearman): {_fmt_spearman(block['spearman'])}",
        f"  Confusion at cutoff {cutoff} (maybe counts as {maybe}):",
        f"    Human accept / model accept: {counts['tp']}",
        f"    Human accept / model reject: {counts['fn']}",
        f"    Human reject / model accept: {counts['fp']}",
        f"    Human reject / model reject: {counts['tn']}",
        f"  False negatives: {counts['fn']} (weight {fn_weight:g})",
        f"  False positives: {counts['fp']} (weight 1)",
        f"  Weighted errors: {weighted['weighted_errors']:g}",
        f"  Weighted error rate: {_fmt_rate(weighted['weighted_error_rate'])}",
    ])


def format_distribution(scores):
    if not scores:
        return "Random control score distribution: no labeled JDs."
    lines = ["Random control score distribution (this is the score distribution, not an accuracy claim):"]
    percentiles = score_percentiles(scores)
    parts = [f"p{pct} {value:.1f}" for pct, value in percentiles.items()]
    lines.append("  Percentiles: " + ", ".join(parts))
    for start, end, count in score_histogram(scores):
        lines.append(f"  {start:3d}-{end:<3d} {count}")
    return "\n".join(lines)


def format_sanity(rows, cutoff, margin):
    high_no, low_yes = sanity_failures(rows, cutoff, margin)
    clear_high = sum(1 for row in rows if row["match_score"] > cutoff + margin)
    clear_low = sum(1 for row in rows if row["match_score"] < cutoff - margin)
    lines = ["Sanity checks"]
    if clear_high == 0:
        lines.append("  No clear-high JDs in this set.")
    elif not high_no:
        lines.append("  PASS: no clear-high JD is labeled no or strong no.")
    else:
        lines.append("  FAIL: clear-high JD labeled no or strong no.")
        for row in high_no:
            lines.append(f"    - {row['title']} (score {row['match_score']})")
    if clear_low == 0:
        lines.append("  No clear-low JDs in this set.")
    elif not low_yes:
        lines.append("  PASS: no clear-low JD is labeled yes or strong yes.")
    else:
        lines.append("  FAIL: clear-low JD labeled yes or strong yes.")
        for row in low_yes:
            lines.append(f"    - {row['title']} (score {row['match_score']})")
    passed = not high_no and not low_yes
    lines.append("SANITY: PASS" if passed else "SANITY: FAIL")
    return "\n".join(lines), passed


def format_disagreement_lines(items):
    if not items:
        return "Disagreements: none in this set."
    lines = [f"Disagreements: {len(items)}"]
    for item in items:
        reasons = ", ".join(item["reasons"])
        lines.append(
            f"- {item['title']} | {item['label'].replace('_', ' ')} | "
            f"score {item['match_score']} | {reasons}"
        )
    return "\n".join(lines)


def format_tag_counts(grouped, current_prompt, current_profile):
    if not grouped:
        return "Cause tags: none yet.\n" + pattern_guidance({})
    lines = ["Cause tags by prompt / profile version"]
    current = {}
    for (prompt, profile, tag), count in sorted(grouped.items()):
        lines.append(f"- {prompt} / {profile} / {tag}: {count}")
        if prompt == current_prompt and profile == current_profile:
            current[tag] = count
    lines.append(pattern_guidance(current))
    return "\n".join(lines)


def fingerprint_warning(prior_rows, fingerprint, prompt_version):
    for row in prior_rows:
        if row["scores_fingerprint"] == fingerprint and row["prompt_version"] != prompt_version:
            return (
                "These scores match a report from prompt "
                f"{row['prompt_version']}. This report is not a new scoring run."
            )
    return None


def load_sample_frame(conn, sample_id):
    cur = conn.execute(
        "SELECT s.url, s.stratum, s.low_kind, s.split, s.position, s.score_at_sample, "
        "jd.title, jd.location, jd.description "
        "FROM fit_samples s JOIN job_details jd ON jd.url = s.url "
        "WHERE s.sample_set_id = ? ORDER BY s.position",
        (sample_id,),
    )
    keys = [
        "url", "stratum", "low_kind", "split", "position", "score_at_sample",
        "title", "location", "description",
    ]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def _live_scores(conn):
    scores = {}
    for job in load_scored_jobs(conn):
        scores[job["url"]] = job
    return scores


def _snapshots(conn, prompt_version, profile_version):
    cur = conn.execute(
        "SELECT url, match_score, recommendation, genuine_gaps, transferable_strengths, risk_factors, model "
        "FROM fit_score_snapshots WHERE prompt_version = ? AND profile_version = ?",
        (prompt_version, profile_version),
    )
    keys = [
        "url", "match_score", "recommendation", "genuine_gaps",
        "transferable_strengths", "risk_factors", "model",
    ]
    return {row[0]: dict(zip(keys, row)) for row in cur.fetchall()}


def attach_scores(frame, live, snapshots_by_url):
    rows = []
    unpinned = []
    for item in frame:
        row = dict(item)
        snap = snapshots_by_url.get(item["url"])
        source = snap or live.get(item["url"])
        if source is None:
            continue
        if snap is None:
            unpinned.append(item["url"])
        row["match_score"] = source["match_score"]
        row["recommendation"] = source.get("recommendation")
        row["genuine_gaps"] = source.get("genuine_gaps")
        row["transferable_strengths"] = source.get("transferable_strengths")
        row["risk_factors"] = source.get("risk_factors")
        row["score_source"] = "snapshot" if snap else "live"
        rows.append(row)
    return rows, unpinned


def attach_labels_and_gates(conn, rows, gate_version):
    labeled = []
    for row in rows:
        label = get_label(conn, row["url"])
        if label is None:
            row["labeled"] = False
            continue
        row["labeled"] = True
        row["label"] = label["label"]
        row["note"] = label["note"]
        row["title"] = label["title"] or row["title"]
        row["description"] = label["description"]
        if gate_version:
            gate = get_gate_result(conn, row["url"], gate_version)
            row["gate_recorded"] = gate is not None
            row["gate_passed"] = None if gate is None else gate["passed"]
            row["failing_constraint"] = None if gate is None else gate["failing_constraint"]
        else:
            row["gate_recorded"] = False
            row["gate_passed"] = None
            row["failing_constraint"] = None
        labeled.append(row)
    return labeled


def tag_counts(conn):
    cur = conn.execute(
        "SELECT prompt_version, profile_version, cause_tag, COUNT(*) "
        "FROM fit_disagreement_tags GROUP BY prompt_version, profile_version, cause_tag"
    )
    return {(row[0], row[1], row[2]): row[3] for row in cur.fetchall()}


def prior_reports(conn, sample_id):
    cur = conn.execute(
        "SELECT prompt_version, profile_version, split, scores_fingerprint, summary_json, recorded_at "
        "FROM fit_reports WHERE sample_set_id = ? ORDER BY id",
        (sample_id,),
    )
    rows = []
    for prompt, profile, split, fingerprint, summary, recorded in cur.fetchall():
        rows.append({
            "prompt_version": prompt,
            "profile_version": profile,
            "split": split,
            "scores_fingerprint": fingerprint,
            "summary": json.loads(summary),
            "recorded_at": recorded,
        })
    return rows


def other_noise_versions(conn, prompt_version, profile_version):
    cur = conn.execute(
        "SELECT prompt_version, profile_version, url, match_score FROM fit_noise_runs"
    )
    grouped = {}
    for prompt, profile, url, score in cur.fetchall():
        if prompt == prompt_version and profile == profile_version:
            continue
        bucket = grouped.setdefault((prompt, profile), {})
        bucket.setdefault(url, []).append(score)
    versions = []
    for (prompt, profile), by_url in grouped.items():
        ranges = [max(scores) - min(scores) for scores in by_url.values() if scores]
        if not ranges:
            continue
        versions.append({
            "prompt_version": prompt,
            "profile_version": profile,
            "average_spread": statistics.fmean(ranges),
        })
    return versions


def build_report(conn, settings, split, prompt_version_text, profile_version_text, gates):
    sample_id = settings.sample_set_id
    frame = load_sample_frame(conn, sample_id)
    if not frame:
        raise LookupError("No sample for these settings. Start labeling first.")
    scored, unpinned = attach_scores(frame, _live_scores(conn), _snapshots(conn, prompt_version_text, profile_version_text))
    gate_ver = None if gates is None else gate_version(gates)
    labeled = attach_labels_and_gates(conn, scored, gate_ver)
    chosen = filter_split(labeled, split)
    noise_records = _noise_rows(conn, prompt_version_text, profile_version_text)
    noise = summarize_noise(noise_records, settings.cutoff, settings.margin) if noise_records else None
    strata = (
        ("Near-threshold", [row for row in chosen if row["stratum"] == "near"]),
        ("Clear highs", [row for row in chosen if row["stratum"] == "high"]),
        ("Clear lows", [row for row in chosen if row["stratum"] == "low"]),
        ("Subset of clear lows: low but not floor", [
            row for row in chosen if row["stratum"] == "low" and row.get("low_kind") == "not_floor"
        ]),
        ("Random control (only this stratum supports an overall accuracy claim)", [
            row for row in chosen if row["stratum"] == "random"
        ]),
    )
    blocks = [
        (title, metric_block(rows, settings.cutoff, settings.maybe_as, settings.fn_weight, False))
        for title, rows in strata
    ]
    gated_blocks = None
    gates_recorded = any(row.get("gate_recorded") for row in chosen)
    if gates is not None and gates_recorded:
        gated_blocks = [
            (title, metric_block(rows, settings.cutoff, settings.maybe_as, settings.fn_weight, True))
            for title, rows in strata
        ]
    disagreements = find_disagreements(chosen, settings.cutoff, settings.maybe_as, settings.rank_gap)
    sanity_text, sanity_pass = format_sanity(chosen, settings.cutoff, settings.margin)
    random_rows = [row for row in chosen if row["stratum"] == "random"]
    random_block = metric_block(random_rows, settings.cutoff, settings.maybe_as, settings.fn_weight, False)
    fingerprint = scores_fingerprint(chosen)
    warning = fingerprint_warning(prior_reports(conn, sample_id), fingerprint, prompt_version_text)
    stored = conn.execute(
        "SELECT shortfall_json FROM fit_sample_sets WHERE sample_set_id = ?", (sample_id,)
    ).fetchone()
    shortfall = json.loads(stored[0]) if stored else {}

    lines = [
        f"Fit-score check ({split})",
        f"Prompt {prompt_version_text}  Profile {profile_version_text}",
        f"Sample {sample_id}",
    ]
    if split == "held_out":
        lines.append(HELD_OUT_BANNER)
    if unpinned:
        lines.append(
            f"{len(unpinned)} JD(s) use the stored production score and are not pinned to this prompt and profile. "
            "Run pin or rescore to bind them."
        )
    else:
        lines.append("Scores are pinned to this prompt and profile.")
    if shortfall:
        short_bits = [
            f"{name} {count}" for name, count in shortfall.get("shortfalls", {}).items() if count
        ]
        if short_bits:
            lines.append("Sampling shortfalls: " + ", ".join(short_bits))
    lines.append("")
    lines.append(format_noise(noise, missing=noise is None, other_versions=other_noise_versions(
        conn, prompt_version_text, profile_version_text
    )))
    lines.append("")
    lines.append(OVERALL_CAVEAT)
    lines.append(
        "Stratum is the band the JD was drawn from. The score in the metrics is the score for this version."
    )
    lines.append(
        "Labels map to strong no=0, no=1, maybe=2, yes=3, strong yes=4 for rank agreement. "
        "Yes and strong yes count as accept. No and strong no count as reject."
    )
    for title, block in blocks:
        lines.append("")
        lines.append(format_metric_block(title, block, settings.fn_weight, settings.maybe_as, settings.cutoff))
    lines.append("")
    lines.append(sanity_text)
    lines.append("")
    lines.append(format_distribution([row["match_score"] for row in random_rows]))
    lines.append("")
    if gates is None:
        lines.append("Hard-constraint gating is not configured (no gates.yaml).")
    elif not gates_recorded:
        lines.append(
            "Gate checks are configured but not recorded for this sample. "
            "Metrics above use the fit score only. Run: python -m app.fit_validate gates"
        )
    else:
        lines.append("With hard-constraint gating (a failed gate counts as a reject):")
        flipped = 0
        for row in chosen:
            if row.get("gate_passed") is False and row["match_score"] >= settings.cutoff:
                flipped += 1
                lines.append(
                    f"- Gated: {row['title']} | {row['failing_constraint']} | score {row['match_score']}"
                )
        lines.append(f"Gating changes {flipped} accept score(s) to reject.")
        for title, block in gated_blocks:
            lines.append("")
            lines.append(format_metric_block(
                title + " with gating", block, settings.fn_weight, settings.maybe_as, settings.cutoff
            ))
    lines.append("")
    lines.append(format_disagreement_lines(disagreements))
    lines.append("")
    lines.append(format_tag_counts(tag_counts(conn), prompt_version_text, profile_version_text))
    if warning:
        lines.append("")
        lines.append(warning)
    body = "\n".join(lines)
    random_weighted = None if random_block["weighted"] is None else random_block["weighted"]["weighted_error_rate"]
    summary = {
        "random_spearman": random_block["spearman"],
        "random_weighted_error_rate": random_weighted,
        "sanity_pass": sanity_pass,
        "labeled": len(chosen),
        "disagreements": len(disagreements),
        "noise_average_spread": None if noise is None else noise["average_spread"],
    }
    return {
        "body": body,
        "summary": summary,
        "fingerprint": fingerprint,
        "gate_version": gate_ver,
        "disagreements": disagreements,
        "split": split,
    }


def save_report(conn, settings, report, prompt_version_text, profile_version_text):
    conn.execute(
        "INSERT INTO fit_reports "
        "(sample_set_id, split, prompt_version, profile_version, gate_version, "
        "scores_fingerprint, body, summary_json, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            settings.sample_set_id, report["split"], prompt_version_text, profile_version_text,
            report["gate_version"], report["fingerprint"], report["body"],
            json.dumps(report["summary"]), utc_now(),
        ),
    )
    conn.commit()


def format_history(rows):
    if not rows:
        return "No saved reports."
    lines = ["Saved reports"]
    for row in rows:
        summary = row["summary"]
        spearman_text = _fmt_spearman(summary.get("random_spearman"))
        rate = _fmt_rate(summary.get("random_weighted_error_rate"))
        lines.append(
            f"- {row['recorded_at']} {row['split']} prompt {row['prompt_version']} "
            f"profile {row['profile_version']} random Spearman {spearman_text} "
            f"random weighted error rate {rate}"
        )
    return "\n".join(lines)


def pin_scores(conn, urls, prompt_version_text, profile_version_text):
    live = _live_scores(conn)
    pinned = 0
    for url in urls:
        source = live.get(url)
        if source is None:
            continue
        _write_snapshot(conn, source, prompt_version_text, profile_version_text, "pin")
        pinned += 1
    conn.commit()
    return pinned


def rescore_jobs(conn, jobs, prompt_version_text, profile_version_text, score_fn):
    """Store a fresh score snapshot. Does not replace the production score."""
    for job in jobs:
        evaluation = score_fn(job)
        _write_snapshot(
            conn,
            {
                "url": job["url"],
                "match_score": int(evaluation["match_score"]),
                "recommendation": evaluation.get("recommendation"),
                "genuine_gaps": evaluation.get("genuine_gaps"),
                "transferable_strengths": evaluation.get("transferable_strengths"),
                "risk_factors": evaluation.get("risk_factors"),
                "model": evaluation.get("model"),
            },
            prompt_version_text,
            profile_version_text,
            "rescore",
        )
        conn.commit()
    return len(jobs)


def _write_snapshot(conn, source, prompt_version_text, profile_version_text, source_name):
    conn.execute(
        "INSERT INTO fit_score_snapshots "
        "(url, prompt_version, profile_version, match_score, recommendation, genuine_gaps, "
        "transferable_strengths, risk_factors, model, source, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url, prompt_version, profile_version) DO UPDATE SET "
        "match_score=excluded.match_score, recommendation=excluded.recommendation, "
        "genuine_gaps=excluded.genuine_gaps, transferable_strengths=excluded.transferable_strengths, "
        "risk_factors=excluded.risk_factors, model=excluded.model, source=excluded.source, "
        "recorded_at=excluded.recorded_at",
        (
            source["url"], prompt_version_text, profile_version_text, int(source["match_score"]),
            source.get("recommendation"), source.get("genuine_gaps"), source.get("transferable_strengths"),
            source.get("risk_factors"), source.get("model"), source_name, utc_now(),
        ),
    )


def parse_tag_response(line):
    text = (line or "").strip()
    if not text:
        return {"action": "invalid", "message": "Enter a cause tag, or q to quit."}
    lower = text.lower()
    if lower in {"q", "quit"}:
        return {"action": "quit"}
    if lower in {"s", "skip"}:
        return {"action": "skip"}
    if lower in {"b", "back"}:
        return {"action": "back"}
    if lower in {"?", "help"}:
        return {"action": "help", "message": _tag_help()}
    for alias in sorted(TAG_ALIASES, key=len, reverse=True):
        if lower == alias or lower.startswith(alias + " ") or lower.startswith(alias + ":"):
            tag = TAG_ALIASES[alias]
            note = text[len(alias):].lstrip(" :") or None
            if tag == "other" and not note:
                return {"action": "invalid", "message": "Other requires a note."}
            return {"action": "tag", "tag": tag, "note": note}
    return {"action": "invalid", "message": _tag_help()}


def _tag_help():
    return (
        "1 profile gap, 2 rubric weighting, 3 hallucination, "
        "4 hard constraint, 5 other (note required). s skip, b back, q quit."
    )


def save_tag(conn, url, prompt_version_text, profile_version_text, tag, note):
    if tag not in CAUSE_TAGS:
        raise ValueError(f"Unknown cause tag {tag}")
    if tag == "other" and not (note or "").strip():
        raise ValueError("Other requires a note")
    conn.execute(
        "INSERT INTO fit_disagreement_tags "
        "(url, prompt_version, profile_version, cause_tag, note, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url, prompt_version, profile_version) DO UPDATE SET "
        "cause_tag=excluded.cause_tag, note=excluded.note, updated_at=excluded.updated_at",
        (url, prompt_version_text, profile_version_text, tag, (note or None), utc_now()),
    )
    conn.commit()


def disagreement_card(row, cutoff, maybe_as):
    predicted = "accept" if model_accepts(row, cutoff, False) else "reject"
    actual = "accept" if human_accepts(row["label"], maybe_as) else "reject"
    note = row.get("note") or "(no note)"
    lines = [
        label_card(row["title"], row.get("description") or "", row.get("location") or ""),
        f"Your label: {row['label'].replace('_', ' ')}",
        f"Your note: {note}",
        f"Model score: {row['match_score']} ({row.get('recommendation') or 'no recommendation'})",
        f"At cutoff {cutoff} the model says {predicted}. Your label maps to {actual}.",
        f"Transferable strengths: {row.get('transferable_strengths') or '(none)'}",
        f"Genuine gaps: {row.get('genuine_gaps') or '(none)'}",
        f"Risk factors: {row.get('risk_factors') or '(none)'}",
        "Reasons: " + ", ".join(row["reasons"]),
    ]
    if "rank" in row["reasons"]:
        lines.append(
            f"Score percentile in this set: {row['percentile']:.2f}. "
            f"Expected for this label: {EXPECTED_PERCENTILE[row['label']]:.2f}."
        )
    return "\n".join(lines)


def _tagged_urls(conn, prompt_version_text, profile_version_text):
    cur = conn.execute(
        "SELECT url FROM fit_disagreement_tags WHERE prompt_version = ? AND profile_version = ?",
        (prompt_version_text, profile_version_text),
    )
    return {row[0] for row in cur.fetchall()}


def run_tag_session(conn, rows, reader, writer, settings, prompt_version_text, profile_version_text, revise=False):
    tagged = set() if revise else _tagged_urls(conn, prompt_version_text, profile_version_text)
    pending = [row for row in rows if row["url"] not in tagged]
    if not pending:
        writer("No untagged disagreements in this set.")
        writer(pattern_guidance(_counts_for_version(conn, prompt_version_text, profile_version_text)))
        return
    index = 0
    while index < len(pending):
        row = pending[index]
        writer("")
        writer(f"--- disagreement {index + 1} of {len(pending)} ---")
        writer(disagreement_card(row, settings.cutoff, settings.maybe_as))
        writer(_tag_help())
        while True:
            try:
                line = reader("Cause: ")
            except EOFError:
                writer("Stopped.")
                return
            parsed = parse_tag_response(line)
            if parsed["action"] in {"invalid", "help"}:
                writer(parsed["message"])
                continue
            break
        if parsed["action"] == "quit":
            writer("Stopped.")
            return
        if parsed["action"] == "back":
            index = max(0, index - 1)
            continue
        if parsed["action"] == "skip":
            index += 1
            continue
        save_tag(conn, row["url"], prompt_version_text, profile_version_text, parsed["tag"], parsed["note"])
        index += 1
    writer(pattern_guidance(_counts_for_version(conn, prompt_version_text, profile_version_text)))


def _counts_for_version(conn, prompt_version_text, profile_version_text):
    counts = {}
    for (prompt, profile, tag), count in tag_counts(conn).items():
        if prompt == prompt_version_text and profile == profile_version_text:
            counts[tag] = count
    return counts


def _sample_jobs_for_split(conn, settings, split):
    frame = load_sample_frame(conn, settings.sample_set_id)
    if split:
        frame = [row for row in frame if row["split"] == split]
    return frame


def _confirm(message, reader, writer, assume_yes):
    writer(message)
    if assume_yes:
        return True
    try:
        answer = reader("Proceed? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def _make_scorer(profile_path):
    import os

    from app import ai_evaluate

    try:
        import anthropic
    except ImportError:
        sys.exit("Missing dependency: pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY env var not set.")
    client = anthropic.Anthropic(api_key=api_key)
    profile = ai_evaluate.load_profile(profile_path)

    def score(job):
        evaluation = ai_evaluate.evaluate_one(client, profile, job)
        evaluation["model"] = ai_evaluate.MODEL
        return evaluation

    return score


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=dedup.DB_PATH)
    common.add_argument("--profile", default="profile.yaml")
    common.add_argument("--gates-file", default="gates.yaml")
    common.add_argument("--cutoff", type=int, default=55)
    common.add_argument("--margin", type=int, default=10)
    common.add_argument("--target", type=int, default=80)
    common.add_argument("--random-size", type=int, default=20)
    common.add_argument("--seed", type=int, default=0)
    common.add_argument("--split-seed", type=int, default=0)
    common.add_argument("--tuning-percent", type=int, default=50)
    common.add_argument("--low-not-floor-percent", type=int, default=50)
    common.add_argument("--maybe-as", choices=["accept", "reject"], default="reject")
    common.add_argument("--fn-weight", type=float, default=3.0)
    common.add_argument("--rank-gap", type=float, default=0.40)
    common.add_argument("--noise-count", type=int, default=12)
    common.add_argument("--noise-runs", type=int, default=5)

    parser = argparse.ArgumentParser(
        prog="python -m app.fit_validate",
        description="Label JDs and check how trustworthy the fit scores are.",
    )
    sub = parser.add_subparsers(dest="command")

    sample = sub.add_parser("sample", parents=[common], help="Draw the labeling sample")
    sample.add_argument("--redraw", action="store_true", help="Draw again and reset the held-out split")

    label = sub.add_parser("label", parents=[common], help="Label JDs without seeing the score")
    label.add_argument("--revise", action="store_true", help="Walk labeled JDs and edit the label")

    report = sub.add_parser("report", parents=[common], help="Metrics for the tuning set")
    report.add_argument("--held-out", action="store_true", help="Confirm on the held-out set")

    tag = sub.add_parser("tag", parents=[common], help="Assign one cause to each disagreement")
    tag.add_argument("--held-out", action="store_true")
    tag.add_argument("--revise", action="store_true")

    noise = sub.add_parser("noise", parents=[common], help="Score a score-range sample several times")
    noise.add_argument("--yes", action="store_true", help="Run the paid rescoring without asking")
    noise.add_argument("--force", action="store_true", help="Discard existing repeats for this version")

    rescore = sub.add_parser("rescore", parents=[common], help="Rescore and snapshot, leaving production scores in place")
    rescore.add_argument("--yes", action="store_true")
    rescore.add_argument("--held-out", action="store_true", help="Rescore the held-out half too")

    sub.add_parser("pin", parents=[common], help="Bind the current production scores to this prompt and profile")
    sub.add_parser("gates", parents=[common], help="Record hard-constraint pass/fail results")
    sub.add_parser("history", parents=[common], help="List saved reports")
    return parser


def settings_from_args(args):
    try:
        return Settings(
            cutoff=args.cutoff,
            margin=args.margin,
            target_size=args.target,
            random_size=args.random_size,
            seed=args.seed,
            split_seed=args.split_seed,
            tuning_percent=args.tuning_percent,
            low_not_floor_percent=args.low_not_floor_percent,
            maybe_as=args.maybe_as,
            fn_weight=args.fn_weight,
            rank_gap=args.rank_gap,
            noise_count=args.noise_count,
            noise_runs=args.noise_runs,
            profile_path=args.profile,
            gates_path=args.gates_file,
            db_path=args.db,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _require_sample(conn, settings):
    sample_id = settings.sample_set_id
    row = conn.execute(
        "SELECT 1 FROM fit_sample_sets WHERE sample_set_id = ?", (sample_id,)
    ).fetchone()
    if row is None:
        raise SystemExit("No sample for these settings. Run python -m app.fit_validate first.")
    return sample_id


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"sample", "label", "report", "tag", "noise", "rescore", "pin", "gates", "history"}
    if not argv or (argv[0] not in commands and argv[0] not in {"-h", "--help"}):
        argv = ["label", *argv]
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    command = args.command

    with dedup.connect(settings.db_path) as conn:
        ensure_schema(conn)
        if command == "sample":
            result = create_sample(conn, settings, redraw=args.redraw)
            print(format_sample_summary(result, settings, reset_held_out=result.get("reset_held_out", False)))
            return
        if command == "label":
            result = create_sample(conn, settings, redraw=False)
            if result.get("created"):
                print(format_sample_summary(result, settings))
            else:
                progress = sample_progress(conn, settings.sample_set_id)
                print(
                    f"Resuming {settings.sample_set_id}. "
                    f"{progress['labeled']} labeled, {progress['unlabeled']} unlabeled."
                )
            run_label_session(conn, settings.sample_set_id, input, print, revise=args.revise)
            return
        if command == "history":
            _require_sample(conn, settings)
            print(format_history(prior_reports(conn, settings.sample_set_id)))
            return

        sample_id = _require_sample(conn, settings)
        prompt = prompt_version()
        profile = profile_version(settings.profile_path)
        gates = load_gates(settings.gates_path)

        if command == "gates":
            if gates is None:
                raise SystemExit(f"No {settings.gates_path}. Copy gates.yaml.example and edit it.")
            jobs = load_sample_frame(conn, sample_id)
            outcome = record_gates(conn, jobs, gates)
            print(
                f"Recorded {outcome['checked']} gate result(s), "
                f"{outcome['failed']} failed, version {outcome['gate_version']}."
            )
            return
        if command == "pin":
            urls = [row["url"] for row in load_sample_frame(conn, sample_id)]
            count = pin_scores(conn, urls, prompt, profile)
            print(f"Pinned {count} production score(s) to prompt {prompt} and profile {profile}.")
            return
        if command == "report":
            split = "held_out" if args.held_out else "tuning"
            report = build_report(conn, settings, split, prompt, profile, gates)
            save_report(conn, settings, report, prompt, profile)
            print(report["body"])
            return
        if command == "tag":
            split = "held_out" if args.held_out else "tuning"
            if split == "held_out":
                print(HELD_OUT_BANNER)
            report = build_report(conn, settings, split, prompt, profile, gates)
            run_tag_session(
                conn, report["disagreements"], input, print, settings, prompt, profile, revise=args.revise
            )
            return
        if command == "noise":
            jobs, shortfall = select_noise_jobs(load_scored_jobs(conn), settings.noise_count)
            if not jobs:
                raise SystemExit("No scored JDs to measure.")
            message = (
                f"This scores {len(jobs)} JD(s) {settings.noise_runs} times each "
                f"({len(jobs) * settings.noise_runs} model calls) with no score cache."
            )
            if shortfall:
                message += f" Shortfall {shortfall}: fewer scored JDs than requested."
            if not _confirm(message, input, print, args.yes):
                print("Cancelled.")
                return
            calls = measure_noise(
                conn, jobs, settings.noise_runs, prompt, profile,
                _make_scorer(settings.profile_path), force=args.force,
            )
            summary = summarize_noise(_noise_rows(conn, prompt, profile), settings.cutoff, settings.margin)
            print(f"New model calls: {calls}")
            print(format_noise(summary))
            return
        if command == "rescore":
            split = None if args.held_out else "tuning"
            jobs = _sample_jobs_for_split(conn, settings, split)
            # Rescore needs the fields the production prompt reads, including company.
            live = {job["url"]: job for job in load_scored_jobs(conn)}
            queue = []
            for job in jobs:
                source = live.get(job["url"])
                if source:
                    queue.append(source)
            if not queue:
                raise SystemExit("Nothing to rescore.")
            scope = "the whole sample" if args.held_out else "the tuning set"
            message = (
                f"This rescores {len(queue)} JD(s) in {scope} and stores a snapshot. "
                "Production scores stay as they are."
            )
            if args.held_out:
                message += " " + HELD_OUT_BANNER
            if not _confirm(message, input, print, args.yes):
                print("Cancelled.")
                return
            count = rescore_jobs(conn, queue, prompt, profile, _make_scorer(settings.profile_path))
            print(f"Snapshotted {count} score(s) for prompt {prompt} and profile {profile}.")
            return
    raise SystemExit(f"Unknown command {command}")


if __name__ == "__main__":
    main()
