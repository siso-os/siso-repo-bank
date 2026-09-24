#!/usr/bin/env python3
"""E3.0 — zero-fetch deterministic backfill for the Foundry value pipeline.

Per the 5-Opus E3 design synthesis (2026-06-26). Fills gating fields for the
whole saucy set BEFORE any network call or model spend. Pure metadata + the
[CODE|INFO|BOTH|license] prefix already embedded in repo_category.why.

Does, idempotently and additively (NULL-only fills, no recompute of existing
value columns):
  1. Add typed columns: value_type, legal_lane, depend_able (if absent).
  2. Dedup the 109 duplicate saucy full_name rows: keep the higher-liftability
     row, tie-break to unit_class_source='model' over 'free'. Loser rows get
     saucy=0 (NEVER deleted — append-only / never-delete invariant) + a note.
  3. Regex the why-prefix -> value_type {CODE,INFO,BOTH} for all saucy rows.
  4. Map repo_card.license SPDX -> legal_lane enum
     {shippable,reference_only,blocked,unknown} from the CARD LICENSE ONLY
     (never the why-prose), with dual-license handling. Default-deny: anything
     unresolved/copyleft/unknown is NOT shippable.
  5. depend_able heuristic from card metadata (refined later in E3.2 by manifest).

It does NOT re-run stage1_unitclass.py — that's step (6) in the build order and
is run separately (its WHERE widened reuse>=65 -> saucy=1). Kept separate so
each script stays single-purpose and re-runnable.

Run:  python3 e3_0_backfill.py            # apply
      python3 e3_0_backfill.py --dry-run  # report only, no writes
"""
import sqlite3
import sys
import re
from pathlib import Path

DB = Path(__file__).resolve().parent / "identity" / "identity.sqlite"

# --- legal lane lookup (card SPDX only; default-deny) ----------------------
PERMISSIVE = {
    "MIT", "MIT-0", "Apache-2.0", "BSD-3-Clause", "BSD-2-Clause", "BSD-2-Clause-Patent",
    "ISC", "0BSD", "Unlicense", "MPL-2.0", "Zlib", "BSL-1.0", "Boost-1.0",
    "CC0-1.0", "WTFPL", "PostgreSQL", "Python-2.0", "BlueOak-1.0.0", "Apache-2.0-with-LLVM-exception",
}
COPYLEFT_PREFIXES = ("GPL", "AGPL", "LGPL", "SSPL", "EUPL", "OSL", "CECILL")
REFERENCE_LICENSES = {"CC-BY-SA-4.0", "CC-BY-4.0", "CC-BY-NC-4.0", "EPL-2.0", "EPL-1.0", "CDDL-1.0"}
UNRESOLVED = {"NOASSERTION", "", None, "NONE", "OTHER"}


def legal_lane(card_license):
    """SPDX -> typed enum. Default-deny: only a clean permissive SPDX is shippable."""
    if card_license is None:
        return "unknown"
    lic = card_license.strip()
    up = lic.upper()
    if up in {x.upper() for x in UNRESOLVED if x}:
        return "unknown"
    if lic in PERMISSIVE:
        return "shippable"
    # dual-license in the SPDX expression: "GPL-2.0 OR MIT" -> resolve to permissive lane
    if " OR " in up:
        parts = [p.strip() for p in re.split(r"\s+OR\s+", lic, flags=re.I)]
        if any(p in PERMISSIVE for p in parts):
            return "shippable"
        # all-copyleft OR -> still blocked
        return "blocked"
    if " AND " in up:
        # AND means all terms apply; any copyleft term blocks
        parts = [p.strip() for p in re.split(r"\s+AND\s+", lic, flags=re.I)]
        if any(p.upper().startswith(COPYLEFT_PREFIXES) for p in parts):
            return "blocked"
        if all(p in PERMISSIVE for p in parts):
            return "shippable"
        return "unknown"
    if up.startswith(COPYLEFT_PREFIXES):
        return "blocked"
    if lic in REFERENCE_LICENSES:
        return "reference_only"
    return "unknown"  # default-deny


WHY_PREFIX = re.compile(r"^\[(CODE|INFO|BOTH|NEITHER)\|", re.I)


def value_type_from_why(why):
    if not why:
        return None
    m = WHY_PREFIX.match(why.strip())
    return m.group(1).upper() if m else None


def ensure_columns(cur):
    cols = {r[1] for r in cur.execute("PRAGMA table_info(repo_category)")}
    added = []
    for name, ddl in (
        ("value_type", "ALTER TABLE repo_category ADD COLUMN value_type TEXT"),
        ("legal_lane", "ALTER TABLE repo_category ADD COLUMN legal_lane TEXT"),
        ("depend_able", "ALTER TABLE repo_category ADD COLUMN depend_able INTEGER"),
    ):
        if name not in cols:
            cur.execute(ddl)
            added.append(name)
    return added


CHUNK = 400  # commit cadence: release the write lock often so the live categorizer isn't starved


def main():
    dry = "--dry-run" in sys.argv
    con = sqlite3.connect(str(DB), timeout=30)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("PRAGMA busy_timeout=30000")  # wait politely, don't SQLITE_BUSY (categorizer is live-writing)

    added = ensure_columns(cur)
    print(f"columns added: {added or 'none (already present)'}")

    # --- 2. dedup saucy duplicates: keep best, demote losers (never delete) ---
    dupes = cur.execute("""
        SELECT full_name FROM repo_category WHERE saucy=1
        GROUP BY full_name HAVING COUNT(*) > 1
    """).fetchall()
    demoted = 0
    for (fn,) in dupes:
        rows = cur.execute("""
            SELECT id, COALESCE(liftability,-1) lift, unit_class_source
            FROM repo_category WHERE saucy=1 AND full_name=?
        """, (fn,)).fetchall()
        # winner: highest liftability, then model-sourced over free
        winner = max(rows, key=lambda r: (r["lift"], 1 if (r["unit_class_source"] or "") == "model" else 0))
        for r in rows:
            if r["id"] != winner["id"]:
                demoted += 1
                if not dry:
                    cur.execute("""
                        UPDATE repo_category
                        SET saucy=0,
                            compose_note=COALESCE(compose_note,'') || ' [e3.0: demoted dup of id '||?||']'
                        WHERE id=?
                    """, (winner["id"], r["id"]))
    print(f"duplicate saucy rows demoted (saucy->0, kept higher-liftability): {demoted}")

    # --- 3. value_type from why-prefix (all saucy, NULL-only) ---
    rows = cur.execute("""
        SELECT id, why FROM repo_category
        WHERE saucy=1 AND (value_type IS NULL OR value_type='')
    """).fetchall()
    vt_filled, vt_unparsed = 0, 0
    for r in rows:
        vt = value_type_from_why(r["why"])
        if vt:
            vt_filled += 1
            if not dry:
                cur.execute("UPDATE repo_category SET value_type=? WHERE id=?", (vt, r["id"]))
        else:
            vt_unparsed += 1
    print(f"value_type filled: {vt_filled}  | unparsed (no prefix, flagged for model): {vt_unparsed}")

    # --- 4. legal_lane from card SPDX (all saucy, NULL-only), default-deny ---
    rows = cur.execute("""
        SELECT rc.id, c.license
        FROM repo_category rc JOIN repo_card c ON rc.full_name=c.full_name
        WHERE rc.saucy=1 AND (rc.legal_lane IS NULL OR rc.legal_lane='')
    """).fetchall()
    lane_counts = {}
    for r in rows:
        lane = legal_lane(r["license"])
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
        if not dry:
            cur.execute("UPDATE repo_category SET legal_lane=? WHERE id=?", (lane, r["id"]))
    print(f"legal_lane distribution (this run): {lane_counts}")

    # --- 5. depend_able coarse heuristic (manifest refines in E3.2) ---
    # coarse signal: has a language we can parse a manifest for + not archived.
    # this is a *candidate* flag; E3.2 confirms via actual manifest exports/bin.
    rows = cur.execute("""
        SELECT rc.id, c.language, c.archived
        FROM repo_category rc JOIN repo_card c ON rc.full_name=c.full_name
        WHERE rc.saucy=1 AND rc.depend_able IS NULL
    """).fetchall()
    MANIFEST_LANGS = {"JavaScript", "TypeScript", "Python", "Go", "Rust", "Ruby", "PHP", "Java", "Kotlin", "C#", "Dart", "Swift"}
    da = 0
    for r in rows:
        cand = 1 if (r["language"] in MANIFEST_LANGS and not r["archived"]) else 0
        da += cand
        if not dry:
            cur.execute("UPDATE repo_category SET depend_able=? WHERE id=?", (cand, r["id"]))
    print(f"depend_able candidate flag set 1 on: {da} (coarse; E3.2 manifest pass confirms)")

    if dry:
        print("\nDRY RUN — no writes committed.")
        con.rollback()
    else:
        con.commit()
        print("\nCOMMITTED.")

    # --- post-state report ---
    print("\n=== post-state (saucy) ===")
    for label, q in [
        ("saucy rows", "SELECT COUNT(*) FROM repo_category WHERE saucy=1"),
        ("value_type set", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND value_type IS NOT NULL"),
        ("legal_lane=shippable", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND legal_lane='shippable'"),
        ("legal_lane=blocked", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND legal_lane='blocked'"),
        ("legal_lane=unknown", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND legal_lane='unknown'"),
        ("legal_lane=reference_only", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND legal_lane='reference_only'"),
        ("depend_able=1", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND depend_able=1"),
        ("unit_class still NULL", "SELECT COUNT(*) FROM repo_category WHERE saucy=1 AND (unit_class IS NULL OR unit_class='')"),
    ]:
        print(f"  {label}: {cur.execute(q).fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    main()
