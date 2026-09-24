#!/usr/bin/env python3
"""Build canonical RepoCards from append-only repo catalog observations.

RepoCards are a derived query surface. Raw JSONL rounds remain append-only
provenance; this script can be rerun after new imports to refresh the current
best metadata row per canonical repo.
"""

import argparse
import json
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
RAW = BASE / "raw"
IDENTITY = BASE / "identity"
STAGING = BASE / "staging"
DB_PATH = IDENTITY / "identity.sqlite"
COVERAGE_JSON = STAGING / "repo-card-coverage.json"
COVERAGE_MD = STAGING / "repo-card-coverage.md"

FIELDS = (
    "full_name",
    "url",
    "stars",
    "language",
    "forks",
    "pushed_at",
    "created_at",
    "description",
    "license",
    "topics",
    "default_branch",
    "archived",
    "fork",
    "mirror",
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_int(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).replace(",", "").strip())
    except ValueError:
        return None


def text(value):
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def bool_or_none(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    lowered = str(value).strip().lower()
    if lowered in ("true", "yes", "1"):
        return 1
    if lowered in ("false", "no", "0"):
        return 0
    return None


def topics_json(value):
    if isinstance(value, list):
        topics = [text(item).lower() for item in value if text(item)]
    elif isinstance(value, str) and value.strip():
        topics = [text(item).lower() for item in value.split(",") if text(item)]
    else:
        topics = []
    seen = set()
    deduped = []
    for topic in topics:
        if topic in seen:
            continue
        seen.add(topic)
        deduped.append(topic)
    return json.dumps(deduped, sort_keys=True)


def has_value(card, field):
    value = card.get(field)
    if field in ("archived", "fork", "mirror"):
        return value is not None
    if field == "topics":
        try:
            return len(json.loads(value or "[]")) > 0
        except json.JSONDecodeError:
            return False
    return value not in (None, "", "NOASSERTION")


def schema_level(card):
    rich_fields = all(card.get(field) is not None for field in ("archived", "fork", "mirror"))
    if card.get("created_at") and rich_fields:
        return "rich_created"
    if rich_fields or card.get("license") or has_value(card, "topics") or card.get("default_branch"):
        return "rich"
    if card.get("language") or card.get("forks") is not None or card.get("pushed_at"):
        return "basic"
    return "seed"


def field_score(card):
    score = sum(1 for field in FIELDS if has_value(card, field))
    if schema_level(card) == "rich_created":
        score += 4
    elif schema_level(card) == "rich":
        score += 2
    return score


def raw_round_rank(raw_round):
    if not raw_round:
        return -1
    try:
        return int(str(raw_round).split("-")[-1])
    except ValueError:
        return -1


def row_to_card(row):
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    normalized_url = text(row.get("normalized_url"))
    observed_url = text(row.get("observed_url"))
    full_name = text(raw.get("full_name") or raw.get("owner_name") or row.get("observed_owner_name") or row.get("source_record_id"))
    url = text(raw.get("url") or raw.get("repo") or observed_url or normalized_url)
    card = {
        "canonical_id": text(row.get("canonical_id")),
        "normalized_url": normalized_url,
        "full_name": full_name,
        "url": url,
        "stars": parse_int(raw.get("stars") if raw.get("stars") is not None else row.get("stars")),
        "language": text(raw.get("language")),
        "forks": parse_int(raw.get("forks")),
        "pushed_at": text(raw.get("pushed_at")),
        "created_at": text(raw.get("created_at")),
        "description": text(raw.get("description") or row.get("description")),
        "license": text(raw.get("license")),
        "topics": topics_json(raw.get("topics")),
        "default_branch": text(raw.get("default_branch")),
        "archived": bool_or_none(raw.get("archived")),
        "fork": bool_or_none(raw.get("fork")),
        "mirror": bool_or_none(raw.get("mirror")),
        "homepage": text(raw.get("homepage")),
        "category": text(raw.get("category")),
        "source": text(row.get("source")),
        "source_format": text(row.get("source_format")),
        "source_record_id": text(row.get("source_record_id")),
        "raw_round": text(row.get("raw_round")),
        "imported_at": text(row.get("imported_at")),
    }
    card["schema_level"] = schema_level(card)
    card["field_score"] = field_score(card)
    return card


def card_sort_key(card):
    level_rank = {"seed": 0, "basic": 1, "rich": 2, "rich_created": 3}.get(card["schema_level"], 0)
    return (
        level_rank,
        card["field_score"],
        text(card.get("imported_at")),
        raw_round_rank(card.get("raw_round")),
    )


def iter_raw_rows():
    for path in sorted(RAW.glob("round-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    yield {"_error": "json_decode", "_path": str(path), "_line": line_no}
                    continue
                if isinstance(row, dict):
                    yield row


def choose_cards():
    best = {}
    raw_rows = 0
    malformed_rows = 0
    for row in iter_raw_rows():
        raw_rows += 1
        if row.get("_error"):
            malformed_rows += 1
            continue
        card = row_to_card(row)
        key = card.get("canonical_id") or card.get("normalized_url")
        if not key:
            malformed_rows += 1
            continue
        previous = best.get(key)
        if previous is None or card_sort_key(card) >= card_sort_key(previous):
            best[key] = card
    return best, raw_rows, malformed_rows


def connect():
    IDENTITY.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repo_card (
          canonical_id TEXT PRIMARY KEY,
          normalized_url TEXT NOT NULL,
          full_name TEXT,
          url TEXT,
          stars INTEGER,
          language TEXT,
          forks INTEGER,
          pushed_at TEXT,
          created_at TEXT,
          description TEXT,
          license TEXT,
          topics_json TEXT NOT NULL,
          default_branch TEXT,
          archived INTEGER,
          fork INTEGER,
          mirror INTEGER,
          homepage TEXT,
          category TEXT,
          source TEXT,
          source_format TEXT,
          source_record_id TEXT,
          raw_round TEXT,
          imported_at TEXT,
          schema_level TEXT NOT NULL,
          field_score INTEGER NOT NULL,
          built_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_repo_card_stars ON repo_card(stars);
        CREATE INDEX IF NOT EXISTS idx_repo_card_language ON repo_card(language);
        CREATE INDEX IF NOT EXISTS idx_repo_card_schema_level ON repo_card(schema_level);
        CREATE INDEX IF NOT EXISTS idx_repo_card_license ON repo_card(license);

        CREATE TABLE IF NOT EXISTS repo_card_build (
          build_id TEXT PRIMARY KEY,
          built_at TEXT NOT NULL,
          raw_rows INTEGER NOT NULL,
          malformed_rows INTEGER NOT NULL,
          cards INTEGER NOT NULL,
          coverage_json TEXT NOT NULL
        );
        """
    )
    conn.commit()


def replace_cards(conn, cards, raw_rows, malformed_rows, coverage):
    built_at = now_iso()
    build_id = f"repo-card-{built_at.replace(':', '').replace('-', '')}"
    rows = []
    for card in cards.values():
        rows.append(
            (
                card["canonical_id"],
                card["normalized_url"],
                card["full_name"],
                card["url"],
                card["stars"],
                card["language"],
                card["forks"],
                card["pushed_at"],
                card["created_at"],
                card["description"],
                card["license"],
                card["topics"],
                card["default_branch"],
                card["archived"],
                card["fork"],
                card["mirror"],
                card["homepage"],
                card["category"],
                card["source"],
                card["source_format"],
                card["source_record_id"],
                card["raw_round"],
                card["imported_at"],
                card["schema_level"],
                card["field_score"],
                built_at,
            )
        )

    with conn:
        conn.execute("DELETE FROM repo_card")
        conn.executemany(
            """
            INSERT INTO repo_card (
              canonical_id, normalized_url, full_name, url, stars, language, forks,
              pushed_at, created_at, description, license, topics_json, default_branch,
              archived, fork, mirror, homepage, category, source, source_format,
              source_record_id, raw_round, imported_at, schema_level, field_score, built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.execute(
            """
            INSERT INTO repo_card_build (
              build_id, built_at, raw_rows, malformed_rows, cards, coverage_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (build_id, built_at, raw_rows, malformed_rows, len(rows), json.dumps(coverage, sort_keys=True)),
        )
    return build_id, built_at


def band_for(stars):
    if stars is None:
        return "unknown"
    if stars >= 100000:
        return "100k+"
    if stars >= 50000:
        return "50k-100k"
    if stars >= 10000:
        return "10k-50k"
    if stars >= 5000:
        return "5k-10k"
    if stars >= 1000:
        return "1k-5k"
    if stars >= 500:
        return "500-999"
    if stars >= 100:
        return "100-499"
    return "<100"


def coverage_for_cards(cards, raw_rows=0, malformed_rows=0):
    by_band = defaultdict(lambda: {"repos": 0, "schema_level": defaultdict(int), "fields": defaultdict(int)})
    schema_level = defaultdict(int)
    fields = defaultdict(int)
    for card in cards.values():
        band = band_for(card.get("stars"))
        by_band[band]["repos"] += 1
        by_band[band]["schema_level"][card["schema_level"]] += 1
        schema_level[card["schema_level"]] += 1
        for field in FIELDS:
            if has_value(card, field):
                by_band[band]["fields"][field] += 1
                fields[field] += 1

    ordered_bands = ["100k+", "50k-100k", "10k-50k", "5k-10k", "1k-5k", "500-999", "100-499", "<100", "unknown"]
    return {
        "generated_at": now_iso(),
        "raw_rows": raw_rows,
        "malformed_rows": malformed_rows,
        "cards": len(cards),
        "schema_level": dict(sorted(schema_level.items())),
        "fields": {field: fields.get(field, 0) for field in FIELDS},
        "bands": {
            band: {
                "repos": by_band[band]["repos"],
                "schema_level": dict(sorted(by_band[band]["schema_level"].items())),
                "fields": {field: by_band[band]["fields"].get(field, 0) for field in FIELDS},
            }
            for band in ordered_bands
            if by_band[band]["repos"] or band in ("1k-5k", "500-999", "100-499")
        },
    }


def atomic_write_text(path, text_value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text_value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def write_coverage_artifacts(coverage):
    atomic_write_text(COVERAGE_JSON, json.dumps(coverage, indent=2, sort_keys=True) + "\n")
    lines = [
        "# RepoCard Coverage",
        "",
        f"Generated: `{coverage['generated_at']}`",
        f"Raw rows read: `{coverage['raw_rows']}`",
        f"Canonical RepoCards: `{coverage['cards']}`",
        f"Malformed rows skipped: `{coverage['malformed_rows']}`",
        "",
        "## By Band",
        "",
        "| Band | Repos | rich_created | rich | basic | seed | license | topics | created_at | default_branch |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for band, row in coverage["bands"].items():
        levels = row["schema_level"]
        fields = row["fields"]
        lines.append(
            "| {band} | {repos} | {rich_created} | {rich} | {basic} | {seed} | {license} | {topics} | {created_at} | {default_branch} |".format(
                band=band,
                repos=row["repos"],
                rich_created=levels.get("rich_created", 0),
                rich=levels.get("rich", 0),
                basic=levels.get("basic", 0),
                seed=levels.get("seed", 0),
                license=fields.get("license", 0),
                topics=fields.get("topics", 0),
                created_at=fields.get("created_at", 0),
                default_branch=fields.get("default_branch", 0),
            )
        )
    lines.extend(
        [
            "",
            "## Field Totals",
            "",
            "| Field | Repos With Field |",
            "| --- | ---: |",
        ]
    )
    for field in FIELDS:
        lines.append(f"| `{field}` | {coverage['fields'].get(field, 0)} |")
    atomic_write_text(COVERAGE_MD, "\n".join(lines) + "\n")


def cmd_rebuild(args):
    cards, raw_rows, malformed_rows = choose_cards()
    coverage = coverage_for_cards(cards, raw_rows=raw_rows, malformed_rows=malformed_rows)
    conn = connect()
    try:
        create_tables(conn)
        build_id, built_at = replace_cards(conn, cards, raw_rows, malformed_rows, coverage)
    finally:
        conn.close()
    if args.write_report:
        write_coverage_artifacts(coverage)
    print(f"rebuilt repo_card build_id={build_id} built_at={built_at} cards={len(cards)} raw_rows={raw_rows} malformed={malformed_rows}")
    if args.write_report:
        print(f"coverage_json={COVERAGE_JSON}")
        print(f"coverage_md={COVERAGE_MD}")


def load_cards_from_db():
    conn = connect()
    try:
        create_tables(conn)
        rows = conn.execute("SELECT * FROM repo_card").fetchall()
    finally:
        conn.close()
    cards = {}
    for row in rows:
        card = dict(row)
        card["topics"] = card.pop("topics_json")
        cards[card["canonical_id"]] = card
    return cards


def cmd_coverage(args):
    cards = load_cards_from_db()
    coverage = coverage_for_cards(cards)
    if args.write_report:
        write_coverage_artifacts(coverage)
    print(json.dumps(coverage, indent=2, sort_keys=True))


def cmd_sample(args):
    conn = connect()
    try:
        create_tables(conn)
        rows = conn.execute(
            """
            SELECT full_name, stars, language, license, schema_level, source, raw_round
            FROM repo_card
            ORDER BY stars DESC NULLS LAST, full_name
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        print(json.dumps(dict(row), sort_keys=True))


def build_parser():
    parser = argparse.ArgumentParser(description="build/query canonical RepoCards")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rebuild = sub.add_parser("rebuild", help="rebuild repo_card table from raw observations")
    rebuild.add_argument("--write-report", action="store_true", help="write coverage JSON/Markdown artifacts")
    rebuild.set_defaults(func=cmd_rebuild)
    coverage = sub.add_parser("coverage", help="print coverage from current repo_card table")
    coverage.add_argument("--write-report", action="store_true", help="write coverage JSON/Markdown artifacts")
    coverage.set_defaults(func=cmd_coverage)
    sample = sub.add_parser("sample", help="print top RepoCards")
    sample.add_argument("--limit", type=int, default=20)
    sample.set_defaults(func=cmd_sample)
    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
