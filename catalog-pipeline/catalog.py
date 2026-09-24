#!/usr/bin/env python3
"""Append-only repo catalog and identity-resolution store.

Phase 2 only: import existing catalogs into raw/round-0000.jsonl and
identity/identity.sqlite. No network calls, no harvest lanes, no curated writes.
"""

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


BASE = Path(__file__).resolve().parent
REPO_ROOT = BASE.parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
RAW = BASE / "raw"
IDENTITY = BASE / "identity"
CURATED = BASE / "curated"
STAGING = BASE / "staging"
DB_PATH = IDENTITY / "identity.sqlite"
ROUND = "round-0000"
ROUND_PATH = RAW / f"{ROUND}.jsonl"
VALID_FORMATS = (
    "combined-json",
    "broad-csv",
    "candidate-json",
    "research-log-md",
    "catalog-md",
    "harvest-jsonl",
)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def die(message, code=1):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def ensure_dirs():
    for path in (RAW, IDENTITY, CURATED, STAGING):
        path.mkdir(parents=True, exist_ok=True)


def fsync_dir(path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        fsync_dir(path.parent)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


@contextmanager
def locked(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def connect():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS repo_identity (
          canonical_id TEXT PRIMARY KEY,
          github_node_id TEXT UNIQUE,
          confidence REAL NOT NULL,
          default_branch TEXT,
          pushed_at TEXT,
          fork INTEGER,
          mirror INTEGER,
          first_seen TEXT NOT NULL,
          last_seen TEXT NOT NULL,
          normalized_url TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS repo_observation (
          obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
          canonical_id TEXT NOT NULL REFERENCES repo_identity(canonical_id),
          observed_owner_name TEXT,
          observed_url TEXT NOT NULL,
          normalized_url TEXT NOT NULL,
          source TEXT NOT NULL,
          source_format TEXT NOT NULL,
          source_record_id TEXT,
          stars INTEGER,
          description TEXT,
          raw_round TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          UNIQUE(source, normalized_url)
        );

        CREATE INDEX IF NOT EXISTS idx_repo_observation_canonical
          ON repo_observation(canonical_id);
        CREATE INDEX IF NOT EXISTS idx_repo_observation_source
          ON repo_observation(source);
        """
    )
    conn.commit()


def path_label(path):
    resolved = Path(path).expanduser().resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT))
    except ValueError:
        pass
    try:
        return str(resolved.relative_to(WORKSPACE_ROOT))
    except ValueError:
        return str(resolved)


def clean_url(value):
    if value is None:
        return ""
    url = str(value).strip()
    url = url.strip("<>")
    url = url.rstrip(").,;")
    if url.startswith("git+"):
        url = url[4:]
    return url


def normalize_github_url(value):
    url = clean_url(value)
    if not url:
        return None
    if url.startswith("github.com/"):
        url = "https://" + url
    if url.startswith("www.github.com/"):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host == "www.github.com":
        host = "github.com"
    if host != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[0].strip().lower()
    repo = parts[1].strip().lower()
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo or " " in owner or " " in repo:
        return None
    return f"https://github.com/{owner}/{repo}"


def canonical_id_for(normalized_url):
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"provisional:{digest}"


def owner_name_from_normalized(normalized_url):
    parsed = urlparse(normalized_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def parse_int(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    return int(match.group(0))


def text_or_empty(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def source_path(path):
    resolved = Path(path).expanduser()
    if resolved.exists():
        return resolved
    rel = REPO_ROOT / path
    if rel.exists():
        return rel
    sibling = WORKSPACE_ROOT / path
    if sibling.exists():
        return sibling
    die(f"source not found: {path}")


def record(url, source_record_id="", stars=None, description="", raw=None, default_branch=None, pushed_at=None, fork=None, mirror=None):
    normalized = normalize_github_url(url)
    if not normalized:
        return None
    return {
        "observed_url": clean_url(url),
        "normalized_url": normalized,
        "canonical_id": canonical_id_for(normalized),
        "observed_owner_name": owner_name_from_normalized(normalized),
        "source_record_id": text_or_empty(source_record_id),
        "stars": parse_int(stars),
        "description": text_or_empty(description),
        "default_branch": default_branch,
        "pushed_at": pushed_at,
        "fork": fork,
        "mirror": mirror,
        "raw": raw or {},
    }


def parse_combined_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("entries", []):
        rec = record(
            entry.get("repo") or entry.get("homepage"),
            source_record_id=entry.get("name") or entry.get("npm") or "",
            description=entry.get("description", ""),
            raw=entry,
        )
        if rec:
            yield rec


def parse_broad_csv(path):
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rec = record(
                row.get("repo"),
                source_record_id=row.get("package") or row.get("name") or "",
                description=row.get("description", ""),
                raw=row,
            )
            if rec:
                yield rec


def parse_candidate_json(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data.get("rows", []):
        pushed_at = None
        last_commit = row.get("lastCommit") or ""
        match = re.search(r"(\d{4}-\d{2}-\d{2}T[0-9:+-]+)", last_commit)
        if match:
            pushed_at = match.group(1)
        rec = record(
            row.get("url"),
            source_record_id=row.get("id") or row.get("name") or "",
            description=row.get("description", ""),
            raw=row,
            pushed_at=pushed_at,
        )
        if rec:
            yield rec


def parse_research_log_md(path):
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n(?=### )", text)
    heading_re = re.compile(r"^###\s+([^\s]+/[^\s]+)\s+\(([\d,]+)\s+stars\)", re.MULTILINE)
    url_re = re.compile(r"^\*\*URL:\*\*\s+(https://github\.com/\S+)", re.MULTILINE)
    desc_re = re.compile(r"^\*\*Description:\*\*\s*(.*)$", re.MULTILINE)
    for block in blocks:
        heading = heading_re.search(block)
        url_match = url_re.search(block)
        if not heading or not url_match:
            continue
        desc = desc_re.search(block)
        rec = record(
            url_match.group(1),
            source_record_id=heading.group(1),
            stars=heading.group(2),
            description=desc.group(1) if desc else "",
            raw={"block": block.strip()},
        )
        if rec:
            yield rec


def split_md_row(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def strip_md(text):
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "").replace("`", "")
    return text_or_empty(text)


def parse_catalog_md(path):
    link_re = re.compile(r"\[([^\]]+)\]\((https://github\.com/[^)]+)\)")
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.lstrip().startswith("|") or "github.com/" not in line:
            continue
        cells = split_md_row(line)
        for label, url in link_re.findall(line):
            description = ""
            if len(cells) >= 6:
                description = strip_md(cells[-2] if "github.com/" not in cells[-2] else cells[-1])
            rec = record(
                url,
                source_record_id=label,
                description=description,
                raw={"line": line, "line_no": line_no},
            )
            if rec:
                yield rec


def parse_harvest_jsonl(path):
    """Phase 3 harvest output: one JSON object per line.
    Accepts {url|repo, owner_name, stars, description, category, source}.
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = record(
                row.get("url") or row.get("repo") or row.get("html_url"),
                source_record_id=row.get("owner_name") or row.get("full_name") or "",
                stars=row.get("stars") or row.get("stargazers_count"),
                description=row.get("description", ""),
                raw=row,
            )
            if rec:
                yield rec


PARSERS = {
    "combined-json": parse_combined_json,
    "broad-csv": parse_broad_csv,
    "candidate-json": parse_candidate_json,
    "research-log-md": parse_research_log_md,
    "catalog-md": parse_catalog_md,
    "harvest-jsonl": parse_harvest_jsonl,
}


def raw_append(row):
    RAW.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, sort_keys=True) + "\n"
    fd = os.open(str(ROUND_PATH), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def identity_confidence(conn, canonical_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM repo_observation WHERE canonical_id = ?",
        (canonical_id,),
    ).fetchone()
    count = row["n"] if row else 0
    return min(0.95, 0.50 + (max(count, 1) - 1) * 0.05)


def upsert_identity(conn, rec, observed_at):
    existing = conn.execute(
        "SELECT canonical_id FROM repo_identity WHERE canonical_id = ?",
        (rec["canonical_id"],),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE repo_identity
            SET last_seen = ?,
                confidence = ?,
                default_branch = COALESCE(default_branch, ?),
                pushed_at = COALESCE(?, pushed_at),
                fork = COALESCE(fork, ?),
                mirror = COALESCE(mirror, ?)
            WHERE canonical_id = ?
            """,
            (
                observed_at,
                identity_confidence(conn, rec["canonical_id"]),
                rec.get("default_branch"),
                rec.get("pushed_at"),
                rec.get("fork"),
                rec.get("mirror"),
                rec["canonical_id"],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO repo_identity (
          canonical_id, github_node_id, confidence, default_branch, pushed_at,
          fork, mirror, first_seen, last_seen, normalized_url
        ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec["canonical_id"],
            0.50,
            rec.get("default_branch"),
            rec.get("pushed_at"),
            rec.get("fork"),
            rec.get("mirror"),
            observed_at,
            observed_at,
            rec["normalized_url"],
        ),
    )


def import_records(conn, source, source_format, records):
    inserted = 0
    skipped = 0
    source_label = path_label(source)
    imported_at = now_iso()
    for rec in records:
        with conn:
            exists = conn.execute(
                "SELECT obs_id FROM repo_observation WHERE source = ? AND normalized_url = ?",
                (source_label, rec["normalized_url"]),
            ).fetchone()
            if exists:
                skipped += 1
                continue
            upsert_identity(conn, rec, imported_at)
            conn.execute(
                """
                INSERT INTO repo_observation (
                  canonical_id, observed_owner_name, observed_url, normalized_url,
                  source, source_format, source_record_id, stars, description,
                  raw_round, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec["canonical_id"],
                    rec["observed_owner_name"],
                    rec["observed_url"],
                    rec["normalized_url"],
                    source_label,
                    source_format,
                    rec["source_record_id"],
                    rec["stars"],
                    rec["description"],
                    ROUND,
                    imported_at,
                ),
            )
            confidence = identity_confidence(conn, rec["canonical_id"])
            conn.execute(
                "UPDATE repo_identity SET confidence = ?, last_seen = ? WHERE canonical_id = ?",
                (confidence, imported_at, rec["canonical_id"]),
            )
        raw_append({
            "raw_round": ROUND,
            "imported_at": imported_at,
            "source": source_label,
            "source_format": source_format,
            "source_record_id": rec["source_record_id"],
            "canonical_id": rec["canonical_id"],
            "observed_owner_name": rec["observed_owner_name"],
            "observed_url": rec["observed_url"],
            "normalized_url": rec["normalized_url"],
            "stars": rec["stars"],
            "description": rec["description"],
            "raw": rec["raw"],
        })
        inserted += 1
    return inserted, skipped, source_label


def cmd_import_raw(args):
    ensure_dirs()
    global ROUND, ROUND_PATH
    if getattr(args, "round", None):
        if not re.fullmatch(r"round-\d{4}", args.round):
            die("--round must look like round-NNNN")
        ROUND = args.round
        ROUND_PATH = RAW / f"{ROUND}.jsonl"
    source = source_path(args.source)
    parser = PARSERS[args.format]
    with locked(BASE / ".catalog.lock"):
        conn = connect()
        try:
            init_db(conn)
            inserted, skipped, source_label = import_records(conn, source, args.format, parser(source))
        finally:
            conn.close()
    print(f"imported source={source_label} format={args.format} inserted={inserted} skipped={skipped} raw_round={ROUND}")


def raw_row_count():
    total = 0
    for path in sorted(RAW.glob("round-*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            total += sum(1 for line in fh if line.strip())
    return total


def cmd_stats(_args):
    conn = connect()
    try:
        init_db(conn)
        identities = conn.execute("SELECT COUNT(*) AS n FROM repo_identity").fetchone()["n"]
        observations = conn.execute("SELECT COUNT(*) AS n FROM repo_observation").fetchone()["n"]
        real = conn.execute(
            "SELECT COUNT(*) AS n FROM repo_identity WHERE github_node_id IS NOT NULL AND github_node_id != ''"
        ).fetchone()["n"]
        provisional = identities - real
        print("repo-catalog stats")
        print(f"raw_rows: {raw_row_count()}")
        print(f"unique_canonical_ids: {identities}")
        print(f"observations: {observations}")
        print(f"identity_real_node_id: {real}")
        print(f"identity_provisional: {provisional}")
        print("by_source:")
        for row in conn.execute(
            "SELECT source, COUNT(*) AS n FROM repo_observation GROUP BY source ORDER BY source"
        ):
            print(f"  {row['source']}: {row['n']}")
    finally:
        conn.close()


def cmd_resolve(args):
    conn = connect()
    try:
        init_db(conn)
        identity = conn.execute(
            "SELECT * FROM repo_identity WHERE canonical_id = ?",
            (args.canonical_id,),
        ).fetchone()
        if not identity:
            die(f"canonical_id not found: {args.canonical_id}", code=2)
        print("identity:")
        for key in identity.keys():
            print(f"  {key}: {identity[key]}")
        print("observations:")
        rows = conn.execute(
            """
            SELECT obs_id, source, source_format, source_record_id, observed_owner_name,
                   observed_url, stars, description, raw_round, observed_at
            FROM repo_observation
            WHERE canonical_id = ?
            ORDER BY obs_id
            """,
            (args.canonical_id,),
        ).fetchall()
        for row in rows:
            print(f"- obs_id: {row['obs_id']}")
            for key in row.keys():
                if key == "obs_id":
                    continue
                value = row[key]
                if key == "description" and value and len(value) > 220:
                    value = value[:217] + "..."
                print(f"  {key}: {value if value is not None else ''}")
    finally:
        conn.close()


def build_parser():
    parser = argparse.ArgumentParser(prog="catalog.py", description="append-only repo catalog")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser("import-raw", help="import a source catalog into raw and identity store")
    p_import.add_argument("--source", required=True)
    p_import.add_argument("--format", required=True, choices=VALID_FORMATS)
    p_import.add_argument("--round", default=None, help="target raw round (round-NNNN); default round-0000")
    p_import.set_defaults(fn=cmd_import_raw)

    p_stats = sub.add_parser("stats", help="print catalog counts")
    p_stats.set_defaults(fn=cmd_stats)

    p_resolve = sub.add_parser("resolve", help="print one identity and all observations")
    p_resolve.add_argument("canonical_id")
    p_resolve.set_defaults(fn=cmd_resolve)

    return parser


def main():
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
