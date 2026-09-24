#!/usr/bin/env python3
"""Import scraped slice shards (urls_slice_*.jsonl) into repo_card. Idempotent: skips repos already present."""
import glob, hashlib, json, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")
SHARD_DIR = "/Users/shaansisodia/SISO_Workspace/.agents/scratch/github-farm"

def norm_url(full_name):
    return f"https://github.com/{full_name}".lower().rstrip("/")

def canon_id(full_name):
    return "provisional:" + hashlib.sha256(norm_url(full_name).encode()).hexdigest()

def main():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    existing = {r[0] for r in con.execute("SELECT full_name FROM repo_card")}
    print(f"[import] {len(existing)} repos already in DB", flush=True)

    shards = sorted(glob.glob(os.path.join(SHARD_DIR, "urls_slice_*.jsonl")))
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    inserted = skipped = 0
    seen_this_run = set()
    for shard in shards:
        for line in open(shard):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            fn = d.get("full_name")
            if not fn or fn in existing or fn in seen_this_run:
                skipped += 1
                continue
            seen_this_run.add(fn)
            topics = d.get("topics") or []
            con.execute("""INSERT OR IGNORE INTO repo_card
                (canonical_id, normalized_url, full_name, url, stars, language, forks,
                 pushed_at, created_at, description, license, topics_json, default_branch,
                 archived, fork, mirror, source, source_format, raw_round, imported_at,
                 schema_level, field_score, built_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (canon_id(fn), norm_url(fn), fn, d.get("url"), d.get("stars"), d.get("language"),
                 d.get("forks"), d.get("pushed_at"), d.get("created_at"), d.get("description"),
                 d.get("license"), json.dumps(topics), d.get("default_branch"),
                 1 if d.get("archived") else 0, 1 if d.get("fork") else 0, 1 if d.get("mirror") else 0,
                 os.path.basename(shard), "slice-jsonl", "slice-import", now,
                 "rich" if d.get("created_at") else "basic", 14, now))
            inserted += 1
            if inserted % 20000 == 0:
                con.commit(); print(f"[import] {inserted} inserted...", flush=True)
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM repo_card").fetchone()[0]
    print(f"[import] DONE: {inserted} inserted, {skipped} skipped (dupes/existing). repo_card now {total}", flush=True)
    con.close()

if __name__ == "__main__":
    main()
