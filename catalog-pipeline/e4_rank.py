#!/usr/bin/env python3
"""
E4 — Within-Capability Deterministic Ranking (RANK-BEFORE-CLONE)

Pure-SQL composite that ranks saucy ExtractCards inside each capability leaf
(category_id) and emits the top 1-3 clean-shippable liftable winners per leaf.
ZERO model calls. Formula verbatim from the E4 design block (wna199sv1.output L25):
    raw = 0.40*liftability_norm + 0.22*unit_class_score + 0.18*freshness
        + 0.12*provides_proxy + 0.08*surface_score
    final_score = raw * legal_mult * liftable_gate
  - reuse_value EXCLUDED.  - stars only as final (log-damped) tiebreak.
  - K=1 if rank-1 beats rank-2 by >0.08, else up to 3 (trim <0.55*score[1]).
Read-only on repo_category/repo_card. Writes ONLY bank_capability_pick
(DROP+recreate=idempotent) + the HTML manifest.
"""
import sqlite3, html, datetime

DB = "/Users/shaansisodia/SISO_Workspace/SISO_Agent_Base/research/repo-catalog/identity/identity.sqlite"
HTML_OUT = "/Users/shaansisodia/SISO_Workspace/SISO_Agent_Base/research/repo-catalog/e4_download_manifest.html"

RANK_SQL = """
WITH lane(lic, mult) AS (
  VALUES ('MIT',1.0),('APACHE-2.0',1.0),('BSD-3-CLAUSE',1.0),('BSD-2-CLAUSE',1.0),
    ('ISC',1.0),('0BSD',1.0),('UNLICENSE',1.0),('CC0-1.0',1.0),('BSL-1.0',1.0),
    ('ZLIB',1.0),('WTFPL',1.0),('BLUEOAK-1.0.0',1.0),
    ('MPL-2.0',0.6),('LGPL-3.0',0.6),('LGPL-2.1',0.6),('EPL-1.0',0.6),('EPL-2.0',0.6),
    ('GPL-3.0',0.0),('GPL-2.0',0.0),('AGPL-3.0',0.0),('SSPL-1.0',0.0),('CC-BY-SA-4.0',0.0)),
scored AS (
  SELECT rc.category_id, rc.full_name, rc.liftability, rc.unit_class, rc.reuse_value,
    rc.confidence, rc.compose_note, rc.legal_lane,
    g.stars, g.license, g.pushed_at, g.archived,
    COALESCE((SELECT mult FROM lane WHERE lic=upper(g.license)),
      CASE WHEN COALESCE(NULLIF(g.license,''),'NOASSERTION')='NOASSERTION' THEN 0.15 ELSE 0.6 END) AS legal_mult,
    CASE WHEN COALESCE(rc.liftability,40)<30 OR rc.unit_class IN ('substrate','reference') THEN 0 ELSE 1 END AS liftable_gate,
    COALESCE(rc.liftability,40)/100.0 AS lift_n,
    CASE rc.unit_class WHEN 'component' THEN 1.00 WHEN 'capability' THEN 0.65 WHEN 'pattern' THEN 0.45 ELSE 0.20 END AS uc_s,
    (CASE WHEN g.archived=1 THEN 0.3 ELSE 1.0 END)*1.0/(1.0+((julianday('now')-julianday(g.pushed_at))/365.0)/2.0) AS fresh,
    MIN(1.0,(CASE WHEN NULLIF(rc.compose_note,'') IS NOT NULL THEN 0.5 ELSE 0 END)+COALESCE(rc.confidence,50)/200.0) AS prov,
    CASE WHEN rc.unit_class='component' THEN 0.8 ELSE 0.5 END AS surf
  FROM repo_category rc JOIN repo_card g ON g.full_name=rc.full_name WHERE rc.saucy=1),
composite AS (
  SELECT *, (0.40*lift_n+0.22*uc_s+0.18*fresh+0.12*prov+0.08*surf)*legal_mult*liftable_gate AS final_score FROM scored),
ranked AS (
  SELECT *, DENSE_RANK() OVER (PARTITION BY category_id
      ORDER BY final_score DESC, legal_mult DESC, liftability DESC, uc_s DESC, fresh DESC,
               log(2,1+COALESCE(stars,0)) DESC, full_name ASC) AS leaf_rank
  FROM composite WHERE final_score>0)
SELECT category_id, leaf_rank, full_name, ROUND(final_score,4) AS score, liftability, unit_class,
  license, legal_lane, legal_mult, COALESCE(stars,0) AS stars, substr(pushed_at,1,10) AS pushed
FROM ranked WHERE leaf_rank<=3
ORDER BY category_id, leaf_rank, legal_mult DESC, liftability DESC, uc_s DESC, fresh DESC,
         log(2,1+COALESCE(stars,0)) DESC, full_name ASC;
"""

WINNER_LIFT_GATE = 70
SHIPPABLE_LANE = "shippable"


def main():
    con = sqlite3.connect(DB, timeout=30.0)
    con.execute("PRAGMA busy_timeout=30000")
    cur = con.cursor()

    cat_name = {}
    try:
        for cid, label in cur.execute(
                "SELECT id, COALESCE(NULLIF(name,''), slug, CAST(id AS TEXT)) FROM category"):
            cat_name[cid] = label
    except sqlite3.OperationalError:
        for (cid,) in cur.execute("SELECT DISTINCT category_id FROM repo_category WHERE saucy=1"):
            cat_name[cid] = str(cid)

    cols = ["category_id", "leaf_rank", "full_name", "score", "liftability",
            "unit_class", "license", "legal_lane", "legal_mult", "stars", "pushed"]
    records = [dict(zip(cols, r)) for r in cur.execute(RANK_SQL).fetchall()]

    leaves = {}
    for rec in records:
        leaves.setdefault(rec["category_id"], []).append(rec)

    picks = []
    for cid, grp in leaves.items():
        grp.sort(key=lambda r: r["leaf_rank"])
        s1 = grp[0]["score"]
        winners = {id(grp[0])}
        if len(grp) > 1 and (s1 - grp[1]["score"]) <= 0.08:
            for r in grp[1:3]:
                if r["score"] >= 0.55 * s1:
                    winners.add(id(r))
        for r in grp:
            r["is_winner"] = 1 if id(r) in winners else 0
            picks.append(r)

    cur.execute("DROP TABLE IF EXISTS bank_capability_pick")
    cur.execute("""CREATE TABLE bank_capability_pick (
        id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL,
        leaf_rank INTEGER NOT NULL, full_name TEXT NOT NULL, score REAL NOT NULL,
        liftability INTEGER, unit_class TEXT, license TEXT, legal_lane TEXT,
        legal_mult REAL, is_winner INTEGER NOT NULL DEFAULT 0, stars INTEGER,
        pushed TEXT, built_at TEXT DEFAULT (datetime('now')))""")
    cur.executemany("""INSERT INTO bank_capability_pick
        (category_id, leaf_rank, full_name, score, liftability, unit_class,
         license, legal_lane, legal_mult, is_winner, stars, pushed)
        VALUES (:category_id,:leaf_rank,:full_name,:score,:liftability,:unit_class,
                :license,:legal_lane,:legal_mult,:is_winner,:stars,:pushed)""", picks)
    cur.execute("CREATE INDEX idx_bcp_cat ON bank_capability_pick(category_id)")
    cur.execute("CREATE INDEX idx_bcp_winner ON bank_capability_pick(is_winner)")
    con.commit()

    def clone_ready(r):
        return (r["is_winner"] == 1 and (r["liftability"] or 0) >= WINNER_LIFT_GATE
                and (r["legal_lane"] or "") == SHIPPABLE_LANE)

    clone_ready_rows = [r for r in picks if clone_ready(r)]
    total_clone_ready = len(clone_ready_rows)

    write_html(picks, leaves, cat_name, total_clone_ready, clone_ready_rows)

    total_leaves = len(leaves)
    total_winners = sum(1 for r in picks if r["is_winner"])
    print("=" * 70)
    print("E4 within-capability ranking complete")
    print("=" * 70)
    print(f"  ranked rows (<=3/leaf) written : {len(picks)}")
    print(f"  capability leaves ranked       : {total_leaves}")
    print(f"  winners (is_winner=1)          : {total_winners}")
    print(f"  READY TO CLONE TODAY           : {total_clone_ready}")
    print("     (winner AND liftability>=70 AND legal_lane='shippable')\n")

    print("  TOP WINNER for 10 capabilities (rank-1, gate-passing):")
    shown = 0
    for cid in sorted(leaves.keys()):
        grp = sorted(leaves[cid], key=lambda r: r["leaf_rank"])
        w = next((r for r in grp if r["is_winner"]), None)
        if not w:
            continue
        nm = cat_name.get(cid, str(cid))
        print(f"    [{nm[:24]:<24}] {w['full_name'][:38]:<38} score={w['score']:.3f} "
              f"lift={w['liftability']} uc={w['unit_class']} lic={w['license']}")
        shown += 1
        if shown >= 10:
            break
    print()
    prove_giant_excluded(cur)
    con.close()
    print(f"\n  TABLE : bank_capability_pick (in identity.sqlite)")
    print(f"  HTML  : {HTML_OUT}")
    print(f"  CLONE-READY TOTAL: {total_clone_ready}")


def prove_giant_excluded(cur):
    print("  PROOF — low-lift giants are NOT winners (excluded by gate):")
    giants = ["rails/rails", "ManimCommunity/manim", "3b1b/manim", "django/django",
              "torvalds/linux", "kubernetes/kubernetes", "tensorflow/tensorflow", "facebook/react"]
    ph = ",".join("?" for _ in giants)
    found = False
    for (full_name,) in cur.execute(
            f"SELECT DISTINCT full_name FROM repo_category WHERE saucy=1 AND full_name IN ({ph})", giants):
        win = cur.execute("SELECT COUNT(*) FROM bank_capability_pick WHERE full_name=? AND is_winner=1",
                          (full_name,)).fetchone()[0]
        lift, uc, rv = cur.execute(
            "SELECT liftability, unit_class, reuse_value FROM repo_category WHERE full_name=? AND saucy=1 LIMIT 1",
            (full_name,)).fetchone()
        status = "WINNER (!!)" if win else "correctly NOT a winner"
        print(f"    {full_name:<30} lift={lift} uc={uc} reuse_value={rv} -> {status}")
        found = True
    # Always also show high-reuse/low-lift study-only repos that the gate excluded,
    # to prove reuse_value does NOT buy a win (the whole point of E4).
    print("    -- high-reuse_value study-only repos (proof reuse_value != win):")
    for full_name, lift, uc, rv in cur.execute(
            "SELECT full_name, liftability, unit_class, reuse_value FROM repo_category "
            "WHERE saucy=1 AND unit_class IN ('substrate','reference') AND reuse_value>=85 "
            "ORDER BY reuse_value DESC, liftability ASC LIMIT 6"):
        win = cur.execute("SELECT COUNT(*) FROM bank_capability_pick WHERE full_name=? AND is_winner=1",
                          (full_name,)).fetchone()[0]
        status = "WINNER (!!)" if win else "correctly NOT a winner"
        print(f"    {full_name:<30} lift={lift} uc={uc} reuse_value={rv} -> {status}")
    if not found:
        print("    (none of the named hardcoded giants are in the saucy set)")


def write_html(picks, leaves, cat_name, total_clone_ready, clone_ready_rows):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total_leaves = len(leaves)
    total_winners = sum(1 for r in picks if r["is_winner"])
    esc = lambda x: html.escape(str(x if x is not None else ""))

    def lane_badge(r):
        lane, mult = r.get("legal_lane") or "", r.get("legal_mult")
        if lane == "shippable" or mult == 1.0:
            return '<span class="lane ship">shippable</span>'
        if mult == 0.0:
            return '<span class="lane block">copyleft-blocked</span>'
        if mult == 0.15:
            return '<span class="lane unk">unknown-license</span>'
        if mult == 0.6:
            return '<span class="lane link">weak-copyleft</span>'
        return f'<span class="lane">{esc(lane)}</span>'

    p = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>E4 Download Manifest</title><style>
:root{{color-scheme:dark}}body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px 32px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:#8b949e;margin:0 0 20px}}
.hero{{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 28px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:14px 20px;min-width:150px}}
.card .n{{font-size:30px;font-weight:700}}.card.clone .n{{color:#3fb950}}
.card .l{{color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.5px}}
details{{background:#161b22;border:1px solid #30363d;border-radius:8px;margin:0 0 10px}}
summary{{cursor:pointer;padding:10px 14px;font-weight:600;display:flex;justify-content:space-between;gap:12px}}
summary::-webkit-details-marker{{color:#58a6ff}}.leafmeta{{color:#8b949e;font-weight:400;font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:6px 10px;border-top:1px solid #21262d}}
th{{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase}}
tr.winner td{{background:#15291a}}tr.winner td:first-child{{border-left:3px solid #3fb950}}
.rank{{font-weight:700;color:#58a6ff}}.score{{font-variant-numeric:tabular-nums}}
a{{color:#58a6ff;text-decoration:none}}a:hover{{text-decoration:underline}}
.lane{{font-size:11px;padding:1px 7px;border-radius:10px;border:1px solid #30363d}}
.lane.ship{{background:#15291a;color:#3fb950;border-color:#2ea04326}}
.lane.block{{background:#2a1518;color:#f85149;border-color:#f8514926}}
.lane.unk{{background:#2a2415;color:#d29922;border-color:#d2992226}}.lane.link{{background:#1a2330;color:#58a6ff}}
.winflag{{color:#3fb950;font-weight:700}}h2{{font-size:16px;border-bottom:1px solid #30363d;padding-bottom:6px}}
</style></head><body>
<h1>E4 — Download Manifest <span style="color:#8b949e">(rank-before-clone)</span></h1>
<p class="sub">Generated {esc(now)} · deterministic SQL over repo_category(saucy=1) join repo_card · reuse_value excluded · stars tiebreak only</p>
<div class="hero">
<div class="card clone"><div class="n">{total_clone_ready}</div><div class="l">Ready to clone today</div></div>
<div class="card"><div class="n">{total_winners}</div><div class="l">Total winners (1-3/leaf)</div></div>
<div class="card"><div class="n">{total_leaves}</div><div class="l">Capability leaves</div></div>
<div class="card"><div class="n">{len(picks)}</div><div class="l">Ranked rows (<=3/leaf)</div></div>
</div>"""]

    p.append(f'<section><h2>Ready to clone today ({total_clone_ready}) — winner · liftability&ge;70 · shippable</h2>')
    if clone_ready_rows:
        p.append('<table><tr><th>Capability</th><th>Repo</th><th>Score</th><th>Lift</th>'
                 '<th>Unit</th><th>License</th><th>Stars</th></tr>')
        for r in sorted(clone_ready_rows, key=lambda x: -x["score"]):
            nm = cat_name.get(r["category_id"], str(r["category_id"]))
            p.append(f'<tr><td>{esc(nm)}</td><td><a href="https://github.com/{esc(r["full_name"])}">'
                     f'{esc(r["full_name"])}</a></td><td class="score">{r["score"]:.3f}</td>'
                     f'<td>{esc(r["liftability"])}</td><td>{esc(r["unit_class"])}</td>'
                     f'<td>{esc(r["license"])}</td><td>{esc(r["stars"])}</td></tr>')
        p.append('</table>')
    else:
        p.append('<p class="sub">None pass the strict clone gate yet.</p>')
    p.append('</section>')

    p.append('<h2>Per-capability ranking</h2>')

    def leaf_sort_key(cid):
        grp = leaves[cid]
        best = max(r["score"] for r in grp)
        cr = sum(1 for r in grp if r["is_winner"] and (r["liftability"] or 0) >= WINNER_LIFT_GATE
                 and (r["legal_lane"] or "") == SHIPPABLE_LANE)
        return (-cr, -best)

    for cid in sorted(leaves.keys(), key=leaf_sort_key):
        grp = sorted(leaves[cid], key=lambda r: r["leaf_rank"])
        nm = cat_name.get(cid, str(cid))
        n_win = sum(1 for r in grp if r["is_winner"])
        top = grp[0]
        p.append(f'<details><summary><span>{esc(nm)} <span class="leafmeta">'
                 f'&mdash; {n_win} winner(s), {len(grp)} ranked</span></span>'
                 f'<span class="leafmeta">top: {esc(top["full_name"])} ({top["score"]:.3f})</span></summary>')
        p.append('<table><tr><th>Rank</th><th>Repo</th><th>Score</th><th>Lift</th><th>Unit</th>'
                 '<th>Lane</th><th>License</th><th>Stars</th><th>Pushed</th><th>Win</th></tr>')
        for r in grp:
            cls = ' class="winner"' if r["is_winner"] else ''
            wf = '<span class="winflag">&#10003;</span>' if r["is_winner"] else ''
            p.append(f'<tr{cls}><td class="rank">{r["leaf_rank"]}</td>'
                     f'<td><a href="https://github.com/{esc(r["full_name"])}">{esc(r["full_name"])}</a></td>'
                     f'<td class="score">{r["score"]:.3f}</td><td>{esc(r["liftability"])}</td>'
                     f'<td>{esc(r["unit_class"])}</td><td>{lane_badge(r)}</td><td>{esc(r["license"])}</td>'
                     f'<td>{esc(r["stars"])}</td><td>{esc(r["pushed"])}</td><td>{wf}</td></tr>')
        p.append('</table></details>')

    p.append('</body></html>')
    with open(HTML_OUT, "w") as f:
        f.write("\n".join(p))


if __name__ == "__main__":
    main()
