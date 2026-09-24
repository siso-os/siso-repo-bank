#!/usr/bin/env python3
"""
E3 staging migration (idempotent). Run once before E3.1.

Creates two append-only, content_sha-keyed staging tables in identity.sqlite:
  - repo_source_signal : E3.1/E3.2 fetch + digest checkpoint (one row per repo per pinned content)
  - extract_draft      : E3.3 model extract-draft checkpoint (one row per (full_name, content_sha))

Both are physically SEPARATE from repo_category so the categorizer waves (which
write repo_category) never lock E3 rows. The DB is WAL + live-written by the
categorizer, so we use PRAGMA busy_timeout=30000 to wait rather than SQLITE_BUSY.

Safe to re-run: every statement is IF NOT EXISTS. Does NOT touch
repo_category / repo_card / bank_* / the categorizer.
"""

import sqlite3
import sys

DB_PATH = "/Users/shaansisodia/SISO_Workspace/SISO_Agent_Base/research/repo-catalog/identity/identity.sqlite"

# ============================================================
# E3 staging DDL (verbatim from the spec, w3l0fb6b1.output line 13)
# ============================================================

DDL = [
    # ---- E3.1/E3.2: fetch + digest checkpoint -------------------
    """
    CREATE TABLE IF NOT EXISTS repo_source_signal (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      full_name     TEXT    NOT NULL,                 -- github owner/repo (joins repo_category.full_name)
      commit_oid    TEXT,                             -- 40-char pinned default-branch HEAD oid from GraphQL; NULL only if fetch never resolved
      content_sha   TEXT,                             -- sha256(readme_text + '\\x00' + manifest_text); the immutable content fingerprint = checkpoint key
      status        TEXT    NOT NULL DEFAULT 'pending',-- enum below
      default_branch TEXT,                            -- resolved branch name (main/master/...)
      readme_path   TEXT,                             -- which README file resolved (README.md, readme.md, ...)
      readme_text   TEXT,                             -- raw README, capped 24000 chars (matches categorizer cap)
      readme_bytes  INTEGER DEFAULT 0,
      manifest_path TEXT,                             -- package.json / pyproject.toml / go.mod / ...
      manifest_text TEXT,                             -- raw manifest, capped 64KB
      export_names  TEXT,                             -- JSON array of parsed public export/dep names (from manifest hints)
      digest        TEXT,                             -- the <=1.5KB pre-digested model context (why + README-first-400-words + export_names)
      digest_bytes  INTEGER DEFAULT 0,                -- len(digest.encode()); guard: must be <=1536
      http_status   INTEGER,                          -- last README http status (200/404/...) for debugging
      attempt_count INTEGER NOT NULL DEFAULT 0,       -- incremented every fetch try; lets a re-run cap retries
      error_text    TEXT,                             -- last failure reason; NULL on success
      fetched_at    TEXT,                             -- datetime('now') of the successful fetch; NULL until ok
      built_by      TEXT,                             -- wave/agent id, e.g. 'e3-fetch-w1'
      built_at      TEXT    NOT NULL DEFAULT (datetime('now')),
      -- append-only + idempotent: at most one live row per (repo, pinned content).
      -- commit_oid alone is not enough (content can be identical across re-pins); content_sha is the true key.
      UNIQUE(full_name, content_sha)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rss_full_name ON repo_source_signal(full_name)",
    "CREATE INDEX IF NOT EXISTS idx_rss_status    ON repo_source_signal(status)",
    "CREATE INDEX IF NOT EXISTS idx_rss_sha       ON repo_source_signal(content_sha)",

    # ---- E3.3: model extract-draft checkpoint ------------------
    """
    CREATE TABLE IF NOT EXISTS extract_draft (
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      full_name     TEXT    NOT NULL,
      content_sha   TEXT    NOT NULL,                 -- FK-by-value to repo_source_signal.content_sha (the digest this draft was made from)
      commit_oid    TEXT,                             -- copied from signal row for traceability
      status        TEXT    NOT NULL DEFAULT 'pending',-- enum below
      model         TEXT    DEFAULT 'MiniMax-M3',
      raw_json      TEXT,                             -- verbatim model text/JSON (audit trail; Opus audit reads this)
      -- parsed, model-asserted fields (the promoter reads these):
      liftability   INTEGER,                          -- 0-100 model judgment
      legal_lane    TEXT,                             -- model's lane; SPDX cap reapplied AFTER (model may only DOWNGRADE)
      band          TEXT,                             -- gold band slug (id-util/config/cli/...)
      one_liner     TEXT,
      provides      TEXT,                             -- JSON array
      requires      TEXT,                             -- JSON array
      assumptions   TEXT,                             -- JSON array
      recipe        TEXT,                             -- JSON object
      smoke         TEXT,                             -- runnable smoke string
      surface       TEXT,                             -- small/medium/large note
      promoted      INTEGER NOT NULL DEFAULT 0,       -- 1 once a bank_contractcard row was written from this draft
      promoted_card_id TEXT,                          -- bank_contractcard.card_id written (NULL until promoted)
      attempt_count INTEGER NOT NULL DEFAULT 0,
      error_text    TEXT,
      fetched_at    TEXT,                             -- datetime('now') the model returned a parseable draft
      built_by      TEXT,
      built_at      TEXT    NOT NULL DEFAULT (datetime('now')),
      UNIQUE(full_name, content_sha)                  -- one draft per repo per pinned content; re-run is idempotent
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ed_full_name ON extract_draft(full_name)",
    "CREATE INDEX IF NOT EXISTS idx_ed_status    ON extract_draft(status)",
    "CREATE INDEX IF NOT EXISTS idx_ed_promoted  ON extract_draft(promoted)",
]

# Status enum reference (documented in spec; enforced at the application layer, not by SQLite):
#   repo_source_signal.status: pending | fetched | digested | error | no_repo | no_oid
#   extract_draft.status:      pending | ok | ungrounded | parse_fail | error


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        # Wait (don't SQLITE_BUSY) when the categorizer holds the write lock.
        conn.execute("PRAGMA busy_timeout=30000")
        # journal_mode is already 'wal' on this DB; do NOT re-set it inside a txn.

        for stmt in DDL:
            conn.execute(stmt)
        conn.commit()

        for table in ("repo_source_signal", "extract_draft"):
            print(f"\n=== schema: {table} ===")
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                print(f"  !! {table} NOT FOUND")
                continue
            for cid, name, ctype, notnull, dflt, pk in rows:
                flags = []
                if pk:
                    flags.append("PK")
                if notnull:
                    flags.append("NOT NULL")
                if dflt is not None:
                    flags.append(f"DEFAULT {dflt}")
                print(f"  {cid:>2}  {name:<17} {ctype:<8} {' '.join(flags)}")

            idx = conn.execute(f"PRAGMA index_list({table})").fetchall()
            if idx:
                print(f"  indexes: {', '.join(r[1] for r in idx)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
