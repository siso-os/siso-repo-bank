#!/usr/bin/env python3
"""E3.1 fetch stage — pull README + manifest at a PINNED commit SHA for saucy repos.

Reuses the proven enrichment_pilot.py engine (GraphQL build/fetch, raw README
ladder, MANIFESTS_BY_LANGUAGE) with three required changes:

  1. SELECT population: repo_category(saucy=1, role=primary) JOIN repo_card on
     full_name, DISTINCT full_name, ORDER BY liftability DESC, stars DESC.
     (NOT the stars-banded repo_card-only sampler.)
  2. PIN the commit SHA: take GraphQL defaultBranchRef.target.oid (40 chars) and
     use it in the raw-CDN URL ref slot, storing it as commit_oid. If no token is
     available, fall back to unauthenticated raw-CDN at default_branch and set
     commit_oid = NULL.
  3. content_sha = sha256(readme_text + '\\x00' + manifest_text).

Plus: flag monorepo=true when the root package.json has private:true + workspaces
(noted in the digest; no packages/* fan-out). Build the <=1.5KB digest
(WHY + first 400 README words with badges/HTML-comments stripped + export names).

Reads:  repo_category, repo_card (read-only), GitHub GraphQL, raw.githubusercontent.com
Writes: repo_source_signal ONLY (INSERT OR REPLACE, one row per full_name).

Idempotent: full_names already at status='ok' are skipped. If repo_source_signal
is missing, exits 2 (the table already exists; this stage does not create it).
"""

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

# Reuse the proven engine verbatim.
from enrichment_pilot import (
    HTTP_TIMEOUT,
    MANIFESTS_BY_LANGUAGE,
    build_graphql_query,
    fetch_text_url,
    manifest_paths,
    now_iso,
    owner_repo,
    parse_package_json,
    raw_url,
)

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "identity" / "identity.sqlite"

README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "docs/README.md")
README_MAX_CHARS = 24000          # categorizer cap (spec)
MANIFEST_MAX_BYTES = 64 * 1024    # spec
DIGEST_MAX_BYTES = 1536           # hard cap (spec)
GRAPHQL_BATCH = 25
MAX_ATTEMPTS = 3
BUILT_BY = "e3-fetch-w1"

# Badge / HTML-comment / inline-image strippers for digest README words.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](url)  (badges)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")          # [text](url) -> text
_HTML_TAG = re.compile(r"<[^>]+>")                       # <img ...>, <a ...>, etc.


def run_gh_token():
    """gh auth token, then GITHUB_TOKEN env, else None (-> unauthenticated CDN)."""
    import os

    try:
        tok = subprocess.check_output(
            ["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if tok:
            return tok
    except Exception:
        pass
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    return tok or None


def graphql_fetch(token, batch):
    body = json.dumps({"query": build_graphql_query(batch)}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "siso-foundry-e3-fetch",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request("https://api.github.com/graphql", data=body, headers=headers, method="POST")
    with urlopen(req, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        # Partial errors are common (renamed/deleted repos); keep the data, surface nothing fatal.
        pass
    return payload.get("data") or {}


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def select_candidates(conn, limit, rank_start, rank_end):
    """saucy=1 primary repos joined to repo_card, DISTINCT, ordered by liftability.

    Resume self-heals on the moving target: any saucy repo without an 'ok' (or
    terminal/retry-exhausted) signal row is eligible. The --rank window slices the
    DISTINCT-by-full_name ranking via LIMIT/OFFSET so waves can shard work.
    """
    where = [
        "c.saucy = 1",
        "c.role = 'primary'",
        # skip terminally-done or retry-exhausted full_names
        """NOT EXISTS (
            SELECT 1 FROM repo_source_signal s
            WHERE s.full_name = c.full_name
              AND ( s.status IN ('ok','no_readme','no_commit')
                 OR (s.status = 'fetch_err' AND s.attempt_count >= ?) )
        )""",
    ]
    params = [MAX_ATTEMPTS]

    sql = f"""
        SELECT c.full_name,
               r.normalized_url AS normalized_url,
               r.url            AS url,
               r.canonical_id   AS canonical_id,
               r.language       AS language,
               r.default_branch AS default_branch,
               r.stars          AS stars,
               MAX(c.liftability)   AS liftability,
               MAX(c.overall_value) AS overall_value,
               MAX(c.why)           AS why
        FROM repo_category c
        JOIN repo_card r ON r.full_name = c.full_name
        WHERE {' AND '.join(where)}
        GROUP BY c.full_name
        ORDER BY MAX(c.liftability) DESC, r.stars DESC, c.full_name
    """

    # rank window via LIMIT/OFFSET; --limit caps the final slice.
    offset = max(0, (rank_start or 1) - 1) if rank_start else 0
    window = None
    if rank_end is not None:
        window = max(0, rank_end - offset)
    if limit is not None:
        window = limit if window is None else min(window, limit)

    if window is not None:
        sql += "\n        LIMIT ? OFFSET ?"
        params.extend([window, offset])
    elif offset:
        sql += "\n        LIMIT -1 OFFSET ?"
        params.append(offset)

    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def fetch_readme(owner, repo, ref):
    """Raw README ladder at a pinned ref (oid or branch)."""
    for name in README_NAMES:
        result = fetch_text_url(raw_url(owner, repo, ref, name), README_MAX_CHARS)
        if result["status"] == 200:
            result["path"] = name
            return result
    return {"status": 404, "text": "", "truncated": False, "bytes": 0, "path": "", "url": ""}


def fetch_manifest(owner, repo, ref, language):
    """First resolvable manifest at the pinned ref; parse package.json hints."""
    for rel_path in manifest_paths(language):
        if "*" in rel_path:
            continue
        result = fetch_text_url(raw_url(owner, repo, ref, rel_path), MANIFEST_MAX_BYTES)
        if result["status"] != 200:
            continue
        hints = {}
        is_pkg = rel_path == "package.json"
        if is_pkg:
            hints = parse_package_json(result["text"])
        return {
            "path": rel_path,
            "status": 200,
            "text": result["text"],
            "bytes": result["bytes"],
            "hints": hints,
            "raw": result["text"] if is_pkg else "",
        }
    return {"path": "", "status": 404, "text": "", "bytes": 0, "hints": {}, "raw": ""}


def detect_monorepo(manifest):
    """Root package.json with private:true + workspaces => monorepo (no packages/* fan-out)."""
    if manifest.get("path") != "package.json" or not manifest.get("raw"):
        return False
    try:
        data = json.loads(manifest["raw"])
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(data.get("private") is True and data.get("workspaces"))


def export_names_from(manifest):
    """Parsed public export/dep names from manifest hints (JSON array)."""
    hints = manifest.get("hints") or {}
    names = []
    if hints.get("name"):
        names.append(hints["name"])
    names.extend(hints.get("dependencies") or [])
    # de-dupe preserving order
    seen = set()
    out = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out[:40]


def clean_readme(text):
    """Strip badges (md images), HTML comments, HTML tags; keep link text."""
    if not text:
        return ""
    t = _HTML_COMMENT.sub(" ", text)
    t = _MD_IMAGE.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _HTML_TAG.sub(" ", t)
    return t


def build_digest(why, readme_text, export_names, monorepo):
    """<=1.5KB pre-digested model context (WHY + first 400 README words + exports)."""
    first400 = " ".join(clean_readme(readme_text).split()[:400])
    exports = ", ".join((export_names or [])[:40])
    mono = " [MONOREPO: root package.json private+workspaces; no packages/* fanout]" if monorepo else ""
    d = (
        f"WHY: {(why or '').strip()}{mono}\n"
        f"EXPORTS: {exports}\n"
        f"README(first 400 words): {first400}"
    )
    b = d.encode("utf-8")
    if len(b) > DIGEST_MAX_BYTES:
        d = d.encode("utf-8")[: DIGEST_MAX_BYTES - 64].decode("utf-8", "ignore").rsplit(" ", 1)[0]
        b = d.encode("utf-8")
    return d, len(b)


def already_ok(conn, full_name):
    row = conn.execute(
        "SELECT 1 FROM repo_source_signal WHERE full_name = ? AND status = 'ok' LIMIT 1",
        (full_name,),
    ).fetchone()
    return row is not None


def prior_attempts(conn, full_name):
    row = conn.execute(
        "SELECT COALESCE(MAX(attempt_count), 0) AS n FROM repo_source_signal WHERE full_name = ?",
        (full_name,),
    ).fetchone()
    return int(row["n"]) if row else 0


def write_signal(conn, rec):
    conn.execute(
        """
        INSERT OR REPLACE INTO repo_source_signal
            (full_name, commit_oid, content_sha, status, default_branch,
             readme_path, readme_text, readme_bytes,
             manifest_path, manifest_text, export_names,
             digest, digest_bytes, http_status, attempt_count, error_text,
             fetched_at, built_by, built_at)
        VALUES
            (:full_name, :commit_oid, :content_sha, :status, :default_branch,
             :readme_path, :readme_text, :readme_bytes,
             :manifest_path, :manifest_text, :export_names,
             :digest, :digest_bytes, :http_status, :attempt_count, :error_text,
             :fetched_at, :built_by, :built_at)
        """,
        rec,
    )
    conn.commit()


def process_repo(conn, item, gql, token_present):
    full_name = item["full_name"]
    attempt = prior_attempts(conn, full_name) + 1
    base = {
        "full_name": full_name,
        "commit_oid": None,
        "content_sha": None,
        "status": "fetch_err",
        "default_branch": item.get("default_branch"),
        "readme_path": "",
        "readme_text": "",
        "readme_bytes": 0,
        "manifest_path": "",
        "manifest_text": "",
        "export_names": "[]",
        "digest": "",
        "digest_bytes": 0,
        "http_status": None,
        "attempt_count": attempt,
        "error_text": None,
        "fetched_at": None,
        "built_by": BUILT_BY,
        "built_at": now_iso(),
    }

    if not gql:
        base["status"] = "no_commit"
        base["error_text"] = "graphql could not resolve repo (renamed/deleted/private)"
        write_signal(conn, base)
        return base

    branch_ref = gql.get("defaultBranchRef") or {}
    default_branch = branch_ref.get("name") or item.get("default_branch") or "main"
    oid = ((branch_ref.get("target") or {}).get("oid")) if branch_ref else None
    base["default_branch"] = default_branch

    # Pin to the 40-char oid when present (auth path); else fall back to branch (commit_oid=NULL).
    if oid and len(oid) == 40:
        ref = oid
        base["commit_oid"] = oid
    else:
        ref = default_branch
        base["commit_oid"] = None  # unauthenticated / unresolved oid fallback

    owner, repo = owner_repo({"full_name": full_name, "normalized_url": item.get("normalized_url")})
    language = (gql.get("primaryLanguage") or {}).get("name") or item.get("language") or ""

    readme = fetch_readme(owner, repo, ref)
    base["http_status"] = readme.get("status")
    base["readme_path"] = readme.get("path", "")
    base["readme_text"] = readme.get("text", "")
    base["readme_bytes"] = readme.get("bytes", 0)

    manifest = fetch_manifest(owner, repo, ref, language)
    base["manifest_path"] = manifest.get("path", "")
    base["manifest_text"] = manifest.get("text", "")

    if readme.get("status") != 200 and not base["readme_text"]:
        base["status"] = "no_readme"
        base["error_text"] = f"no README across ladder (last http {readme.get('status')})"
        write_signal(conn, base)
        return base

    monorepo = detect_monorepo(manifest)
    exports = export_names_from(manifest)
    base["export_names"] = json.dumps(exports)

    content_sha = hashlib.sha256(
        base["readme_text"].encode("utf-8") + b"\x00" + base["manifest_text"].encode("utf-8")
    ).hexdigest()
    base["content_sha"] = content_sha

    digest, digest_bytes = build_digest(item.get("why"), base["readme_text"], exports, monorepo)
    base["digest"] = digest
    base["digest_bytes"] = digest_bytes

    if digest_bytes > DIGEST_MAX_BYTES:
        base["status"] = "digest_too_big"
        base["error_text"] = f"digest {digest_bytes}B exceeds {DIGEST_MAX_BYTES}"
        write_signal(conn, base)
        return base

    base["status"] = "ok"
    base["error_text"] = None
    base["fetched_at"] = now_iso()
    write_signal(conn, base)
    return base


def run(limit, rank_start, rank_end):
    if not DB_PATH.exists():
        print(f"missing database: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = connect()
    try:
        # Hard requirement: do not create the table; it must already exist.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_source_signal'"
        ).fetchone()
        if not exists:
            print("repo_source_signal table missing — refusing to create it", file=sys.stderr)
            sys.exit(2)

        token = run_gh_token()
        token_present = bool(token)

        candidates = select_candidates(conn, limit, rank_start, rank_end)
        # idempotent: drop any already-ok full_names defensively
        candidates = [c for c in candidates if not already_ok(conn, c["full_name"])]

        stats = {"selected": len(candidates), "written": 0, "ok": 0,
                 "readme_ok": 0, "manifest_ok": 0, "oid_pinned": 0,
                 "no_readme": 0, "no_commit": 0, "fetch_err": 0, "digest_too_big": 0}
        rows_out = []

        for offset in range(0, len(candidates), GRAPHQL_BATCH):
            batch = candidates[offset: offset + GRAPHQL_BATCH]
            try:
                data = graphql_fetch(token, batch)
            except Exception as err:
                data = {}
                # whole-batch GraphQL failure -> per-repo fetch_err, eligible for retry
                for item in batch:
                    rec = {
                        "full_name": item["full_name"], "commit_oid": None, "content_sha": None,
                        "status": "fetch_err", "default_branch": item.get("default_branch"),
                        "readme_path": "", "readme_text": "", "readme_bytes": 0,
                        "manifest_path": "", "manifest_text": "", "export_names": "[]",
                        "digest": "", "digest_bytes": 0, "http_status": None,
                        "attempt_count": prior_attempts(conn, item["full_name"]) + 1,
                        "error_text": f"graphql batch error: {err}", "fetched_at": None,
                        "built_by": BUILT_BY, "built_at": now_iso(),
                    }
                    write_signal(conn, rec)
                    stats["written"] += 1
                    stats["fetch_err"] += 1
                time.sleep(5)
                continue

            for index, item in enumerate(batch):
                gql = data.get(f"r{index}")
                rec = process_repo(conn, item, gql, token_present)
                stats["written"] += 1
                stats[rec["status"]] = stats.get(rec["status"], 0) + 1
                if rec["readme_bytes"]:
                    stats["readme_ok"] += 1
                if rec["manifest_path"]:
                    stats["manifest_ok"] += 1
                if rec["commit_oid"]:
                    stats["oid_pinned"] += 1
                rows_out.append(rec)
            time.sleep(1)

        return stats, rows_out, token_present
    finally:
        conn.close()


def build_parser():
    p = argparse.ArgumentParser(description="E3.1 fetch: README+manifest at pinned commit for saucy repos")
    p.add_argument("--limit", type=int, default=None, help="cap number of repos this run")
    p.add_argument("--rank-start", type=int, default=None, help="1-based start of the liftability-rank window")
    p.add_argument("--rank-end", type=int, default=None, help="end of the liftability-rank window (inclusive)")
    return p


def main():
    args = build_parser().parse_args()
    stats, rows, token_present = run(args.limit, args.rank_start, args.rank_end)

    print(f"token_present={token_present}  selected={stats['selected']}  written={stats['written']}")
    print("=" * 100)
    print(f"{'full_name':38} {'oid?':5} {'rdme_B':>7} {'manifest':14} {'content_sha':16} status")
    print("-" * 100)
    for r in rows:
        oid_flag = "yes" if r["commit_oid"] else "no"
        man = r["manifest_path"] or "-"
        sha = (r["content_sha"] or "")[:16] or "-"
        print(f"{r['full_name'][:38]:38} {oid_flag:5} {r['readme_bytes']:>7} {man[:14]:14} {sha:16} {r['status']}")
    print("=" * 100)

    written = stats["written"] or 1
    rd_rate = 100.0 * stats["readme_ok"] / written
    mf_rate = 100.0 * stats["manifest_ok"] / written
    print(
        f"ok={stats['ok']}  oid_pinned={stats['oid_pinned']}  "
        f"README success={stats['readme_ok']}/{stats['written']} ({rd_rate:.0f}%)  "
        f"manifest found={stats['manifest_ok']}/{stats['written']} ({mf_rate:.0f}%)"
    )
    print(
        f"breakdown: ok={stats['ok']} no_readme={stats['no_readme']} "
        f"no_commit={stats['no_commit']} fetch_err={stats['fetch_err']} "
        f"digest_too_big={stats['digest_too_big']}"
    )


if __name__ == "__main__":
    main()
