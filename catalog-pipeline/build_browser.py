#!/usr/bin/env python3
"""
build_browser.py — Foundry corpus TREE-DRILL category navigator generator.

Reads the identity catalog DB (READ-ONLY) and emits a single self-contained,
offline HTML file (vanilla JS + inline CSS, no server / no build / no CDN) that
lets you navigate the value-mined corpus by its 3-level category taxonomy:
  - a collapsible left tree sidebar (13 L0 roots -> 213 L1 -> 38 L2),
  - breadcrumbs + a child-category drill strip in the main panel,
  - the rich sortable repo table scoped to the selected node,
  - a global search box that flips to flat-search-across-everything mode,
  - in-node filters (lang / license / value_type / legal_lane / unit_class /
    liftability slider / clone-ready toggle).

Embeds the SAUCY repos (~24.7k) WITH their saucy category_ids so the client can
filter the embedded set by the selected tree node without re-querying the DB.

Output: foundry_browser.html  (next to this script)
"""

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")
OUT = os.path.join(HERE, "foundry_browser.html")

DESC_TRUNC = 200


def connect_ro():
    """Open the live, WAL-written DB strictly read-only with a busy timeout."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def scalar(cur, q, *a):
    return cur.execute(q, a).fetchone()[0]


def truncate(s, n=DESC_TRUNC):
    if not s:
        return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def safe_json_array(raw):
    """provides/requires are JSON-in-TEXT; tolerate nulls / bad rows."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v if x]
    except Exception:
        pass
    return []


def collect_stats(cur):
    return {
        "total_repos": scalar(cur, "SELECT COUNT(*) FROM repo_card"),
        "categorized": scalar(cur, "SELECT COUNT(DISTINCT full_name) FROM repo_category"),
        "saucy": scalar(cur, "SELECT COUNT(DISTINCT full_name) FROM repo_category WHERE saucy=1"),
        "categories": scalar(cur, "SELECT COUNT(*) FROM category"),
        "winners": scalar(cur, "SELECT COUNT(*) FROM bank_capability_pick WHERE is_winner=1"),
        "contractcards": scalar(cur, "SELECT COUNT(*) FROM extract_draft WHERE one_liner IS NOT NULL AND one_liner != ''"),
    }


def collect_tree(cur):
    """
    Full category tree as a flat list of nodes (client nests via parent_id),
    each carrying own counts. Rollup totals (over all descendants) are computed
    here in Python so the client gets them for free.
    """
    cats = cur.execute(
        "SELECT id, parent_id, name, slug, level FROM category ORDER BY level, name"
    ).fetchall()

    # own counts per category_id: total placements + saucy placements
    own = {}  # id -> {"total": n, "saucy": n}
    for r in cur.execute(
        "SELECT category_id, COUNT(*) AS total, "
        "SUM(CASE WHEN saucy=1 THEN 1 ELSE 0 END) AS saucy "
        "FROM repo_category GROUP BY category_id"
    ):
        own[r["category_id"]] = {"total": r["total"], "saucy": r["saucy"] or 0}

    nodes = {}
    children = {}  # parent_id -> [child id, ...]
    order = []
    for c in cats:
        cid = c["id"]
        oc = own.get(cid, {"total": 0, "saucy": 0})
        nodes[cid] = {
            "id": cid,
            "p": c["parent_id"],
            "name": c["name"],
            "slug": c["slug"],
            "level": c["level"],
            "ot": oc["total"],   # own_total
            "os": oc["saucy"],   # own_saucy
            "rt": 0,             # rollup_total (filled below)
            "rs": 0,             # rollup_saucy (filled below)
        }
        order.append(cid)
        if c["parent_id"] is not None:
            children.setdefault(c["parent_id"], []).append(cid)

    # Rollup = own + sum over all descendants. Compute via post-order DFS from
    # roots (any node whose parent is None or not in the table).
    def rollup(cid):
        nd = nodes[cid]
        rt, rs = nd["ot"], nd["os"]
        for ch in children.get(cid, []):
            crt, crs = rollup(ch)
            rt += crt
            rs += crs
        nd["rt"], nd["rs"] = rt, rs
        return rt, rs

    roots = [cid for cid in order if nodes[cid]["p"] is None or nodes[cid]["p"] not in nodes]
    for rid in roots:
        rollup(rid)

    # Emit in a stable order: roots first (level then name), client builds nesting.
    tree = [nodes[cid] for cid in order]
    return tree, roots


def collect_repos(cur):
    """
    One row per saucy repo, but carrying ALL category_ids it is saucy in (a repo
    can be placed in up to 2 categories), so the client can filter the embedded
    set by selected tree node. ContractCard one_liner + provides attached.
    """
    # ContractCards keyed by full_name.
    cards = {}
    for r in cur.execute(
        "SELECT full_name, one_liner, provides FROM extract_draft "
        "WHERE one_liner IS NOT NULL AND one_liner != ''"
    ):
        cards[r["full_name"]] = {
            "one_liner": r["one_liner"],
            "provides": safe_json_array(r["provides"]),
        }

    rows = cur.execute(
        """
        SELECT rc.full_name      AS full_name,
               rc.category_id    AS category_id,
               c.name            AS category,
               rc.overall_value  AS overall_value,
               rc.liftability    AS liftability,
               rc.unit_class     AS unit_class,
               rc.value_type     AS value_type,
               rc.legal_lane     AS legal_lane,
               card.stars        AS stars,
               card.language     AS language,
               card.license      AS license,
               card.description  AS description,
               card.url          AS url
        FROM repo_category rc
        LEFT JOIN category c   ON c.id = rc.category_id
        LEFT JOIN repo_card card ON card.full_name = rc.full_name
        WHERE rc.saucy = 1
        ORDER BY rc.overall_value DESC, card.stars DESC
        """
    ).fetchall()

    best = {}  # full_name -> packed row (first seen = highest overall_value)
    for r in rows:
        fn = r["full_name"]
        if fn in best:
            # repo already seen under a higher-value category; just record this
            # additional category_id so it shows up under that node too.
            if r["category_id"] is not None and r["category_id"] not in best[fn]["cs"]:
                best[fn]["cs"].append(r["category_id"])
            continue
        card = cards.get(fn)
        rec = {
            "n": fn,                                   # name
            "c": r["category"] or "",                 # primary category name (best)
            "cs": [r["category_id"]] if r["category_id"] is not None else [],  # all saucy category_ids
            "s": r["stars"] or 0,                      # stars
            "lg": r["language"] or "",                 # language
            "lc": r["license"] or "",                  # license
            "lf": r["liftability"] if r["liftability"] is not None else 0,  # liftability
            "uc": r["unit_class"] or "unknown",        # unit_class
            "vt": r["value_type"] or "unknown",        # value_type
            "ll": r["legal_lane"] or "unknown",        # legal_lane
            "ov": r["overall_value"] if r["overall_value"] is not None else 0,  # overall_value
            "d": truncate(r["description"]),           # description
            "u": r["url"] or ("https://github.com/" + fn),  # url
        }
        if card:
            rec["ol"] = card["one_liner"]              # contractcard one_liner
            rec["pv"] = card["provides"][:8]           # provides bullets (cap 8)
        best[fn] = rec

    repos = list(best.values())
    # Already sorted by overall_value via query order + first-seen insertion.
    return repos


def build_filter_lists(repos):
    langs = sorted({r["lg"] for r in repos if r["lg"]})
    lics = sorted({r["lc"] for r in repos if r["lc"]})
    units = sorted({r["uc"] for r in repos if r["uc"]})
    return langs, lics, units


def render_html(stats, repos, tree, roots):
    langs, lics, units = build_filter_lists(repos)
    data_json = json.dumps(repos, separators=(",", ":"), ensure_ascii=False)
    tree_json = json.dumps(tree, separators=(",", ":"), ensure_ascii=False)
    roots_json = json.dumps(roots, separators=(",", ":"), ensure_ascii=False)
    langs_json = json.dumps(langs, ensure_ascii=False)
    lics_json = json.dumps(lics, ensure_ascii=False)
    units_json = json.dumps(units, ensure_ascii=False)

    return TEMPLATE.format(
        data_json=data_json,
        tree_json=tree_json,
        roots_json=roots_json,
        langs_json=langs_json,
        lics_json=lics_json,
        units_json=units_json,
        embedded=len(repos),
        total_repos=stats["total_repos"],
        categorized=stats["categorized"],
        saucy=stats["saucy"],
        categories=stats["categories"],
        winners=stats["winners"],
        contractcards=stats["contractcards"],
    )


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Foundry Category Navigator</title>
<style>
:root {{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2128; --elev:#21262d;
  --border:#262c36; --border-strong:#30363d;
  --fg:#e6edf3; --fg2:#c9d1d9; --muted:#8b949e; --faint:#6e7681;
  --accent:#58a6ff; --accent-soft:rgba(88,166,255,.14); --accent-line:rgba(88,166,255,.35);
  --mono-fg:#d2a8ff; --green:#3fb950; --red:#f85149; --amber:#d29922; --pink:#f778ba;
  --radius:10px; --radius-sm:7px;
}}
* {{ box-sizing:border-box; }}
html,body {{ margin:0; padding:0; height:100%; background:var(--bg); color:var(--fg);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased; }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.mono {{ font-family:"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,monospace; }}
::-webkit-scrollbar {{ width:11px; height:11px; }}
::-webkit-scrollbar-thumb {{ background:#30363d; border-radius:6px; border:3px solid transparent;
  background-clip:padding-box; }}
::-webkit-scrollbar-thumb:hover {{ background:#3d444d; background-clip:padding-box; border:3px solid transparent; }}
::-webkit-scrollbar-track {{ background:transparent; }}

/* ============ header ============ */
header {{ padding:14px 26px 13px; border-bottom:1px solid var(--border);
  background:linear-gradient(180deg,#161b22,#13171e);
  display:flex; align-items:center; justify-content:space-between; gap:24px; flex-wrap:wrap; }}
.brand {{ display:flex; align-items:baseline; gap:11px; }}
.brand .logo {{ width:9px; height:9px; border-radius:50%; background:var(--accent);
  box-shadow:0 0 0 4px var(--accent-soft); align-self:center; }}
.title {{ font-size:16px; font-weight:650; letter-spacing:.2px; }}
.title small {{ color:var(--muted); font-weight:400; font-size:12.5px; margin-left:2px; }}
.stats {{ display:flex; gap:0; align-items:stretch; }}
.stat {{ padding:0 18px; text-align:center; border-right:1px solid var(--border); }}
.stat:last-child {{ border-right:none; padding-right:0; }}
.stat:first-child {{ padding-left:0; }}
.stat b {{ font-size:16px; display:block; color:var(--fg); font-weight:650;
  font-variant-numeric:tabular-nums; letter-spacing:-.2px; }}
.stat span {{ color:var(--faint); font-size:10.5px; text-transform:uppercase; letter-spacing:.6px; }}

/* ============ layout ============ */
.layout {{ display:flex; height:calc(100vh - 60px); min-height:420px; }}

/* ---- sidebar ---- */
.sidebar {{ width:312px; min-width:240px; max-width:46%; flex:0 0 auto;
  border-right:1px solid var(--border); background:#13171e; overflow:auto;
  padding:0 0 60px; resize:horizontal; }}
.sbhdr {{ position:sticky; top:0; z-index:3; padding:13px 18px 10px;
  background:#13171e; border-bottom:1px solid var(--border);
  display:flex; justify-content:space-between; align-items:center; }}
.sbhdr .sbtitle {{ font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:var(--muted); font-weight:600; }}
.sbhdr .sbtools button {{ background:none; border:1px solid var(--border-strong); color:var(--muted);
  border-radius:6px; font-size:10.5px; padding:3px 8px; cursor:pointer; margin-left:5px; transition:.12s; }}
.sbhdr .sbtools button:hover {{ color:var(--fg); border-color:var(--accent-line); background:var(--accent-soft); }}
.treewrap {{ padding:8px 8px 0; }}

.tnode {{ user-select:none; }}
.trow {{ position:relative; display:flex; align-items:flex-start; gap:7px; padding:5px 9px; cursor:pointer;
  border-radius:var(--radius-sm); margin:1px 0; transition:background .1s; }}
.trow:hover {{ background:#1b2129; }}
.trow.sel {{ background:var(--accent-soft); }}
.trow.sel::before {{ content:""; position:absolute; left:0; top:4px; bottom:4px; width:3px;
  border-radius:3px; background:var(--accent); }}
.tcaret {{ width:13px; min-width:13px; text-align:center; color:var(--faint); font-size:9px;
  line-height:21px; transition:transform .12s; flex:0 0 13px; }}
.tnode.open > .trow > .tcaret {{ transform:rotate(90deg); }}
.tcaret.leaf {{ color:#3d444d; font-size:5px; }}
.tname {{ flex:1; line-height:1.35; color:var(--fg2); overflow-wrap:anywhere; }}
.trow:hover .tname {{ color:var(--fg); }}
.trow.sel .tname {{ color:var(--accent); font-weight:600; }}
.tlvl0 > .trow .tname {{ font-weight:600; color:var(--fg); font-size:13.5px; }}
.tlvl1 > .trow .tname {{ color:var(--fg2); }}
.tlvl2 > .trow .tname {{ color:var(--muted); font-size:13px; }}
.tcount {{ font-size:10.5px; color:var(--faint); font-variant-numeric:tabular-nums;
  flex:0 0 auto; line-height:21px; padding-left:4px; }}
.trow.sel .tcount {{ color:var(--accent); opacity:.85; }}
.tchildren {{ display:none; }}
.tnode.open > .tchildren {{ display:block; }}

/* ---- main ---- */
.main {{ flex:1; display:flex; flex-direction:column; overflow:hidden; background:var(--bg); }}

.crumbs {{ padding:13px 28px 11px; font-size:13px; display:flex; align-items:center;
  gap:8px; flex-wrap:wrap; }}
.crumbs a {{ cursor:pointer; color:var(--muted); }}
.crumbs a:hover {{ color:var(--accent); text-decoration:none; }}
.crumbs .sep {{ color:#3d444d; font-size:11px; }}
.crumbs .cur {{ color:var(--fg); font-weight:600; }}

/* big category heading block */
.titleblock {{ padding:2px 28px 16px; }}
.titleblock h1 {{ margin:0; font-size:25px; font-weight:680; letter-spacing:-.5px; color:var(--fg); }}
.titleblock .sub {{ margin-top:6px; color:var(--muted); font-size:13px; display:flex; gap:7px;
  align-items:center; flex-wrap:wrap; }}
.titleblock .sub b {{ color:var(--fg2); font-weight:600; font-variant-numeric:tabular-nums; }}
.titleblock .subhint {{ color:var(--faint); }}
.titleblock .subhint .arrow {{ color:var(--accent); }}
.dot {{ color:#3d444d; }}

/* ---- sticky filter bar ---- */
.controls {{ position:sticky; top:0; z-index:5; padding:11px 28px;
  background:rgba(13,17,23,.86); backdrop-filter:blur(10px);
  border-top:1px solid var(--border); border-bottom:1px solid var(--border);
  display:flex; flex-wrap:wrap; gap:9px; align-items:center; }}
.searchbox {{ position:relative; flex:1; min-width:240px; }}
.searchbox svg {{ position:absolute; left:12px; top:50%; transform:translateY(-50%);
  width:15px; height:15px; stroke:var(--faint); pointer-events:none; }}
#q {{ width:100%; background:var(--panel2); border:1px solid var(--border-strong);
  color:var(--fg); border-radius:var(--radius-sm); padding:9px 12px 9px 36px; font-size:13.5px;
  transition:.14s; }}
#q::placeholder {{ color:var(--faint); }}
#q:focus {{ outline:none; border-color:var(--accent-line); background:#1b222c;
  box-shadow:0 0 0 3px var(--accent-soft); }}
#q.searching {{ border-color:var(--amber); }}
#q.searching ~ svg {{ stroke:var(--amber); }}
select {{ background:var(--panel2); border:1px solid var(--border-strong); color:var(--fg2);
  border-radius:var(--radius-sm); padding:7px 26px 7px 11px; font-size:12.5px; max-width:170px;
  cursor:pointer; transition:.12s; appearance:none;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath d='M1 3l4 4 4-4' stroke='%238b949e' fill='none' stroke-width='1.4'/%3E%3C/svg%3E");
  background-repeat:no-repeat; background-position:right 9px center; }}
select:hover {{ border-color:var(--border-strong); color:var(--fg); }}
select:focus {{ outline:none; border-color:var(--accent-line); }}
.slider-wrap {{ display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted);
  background:var(--panel2); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:6px 12px; }}
.slider-wrap #liftval {{ color:var(--fg2); min-width:16px; text-align:right; }}
input[type=range] {{ accent-color:var(--accent); width:78px; }}
.toggle {{ display:flex; align-items:center; gap:7px; font-size:12.5px; color:var(--fg2);
  background:var(--panel2); border:1px solid var(--border-strong); border-radius:var(--radius-sm);
  padding:7px 12px; cursor:pointer; transition:.12s; }}
.toggle:hover {{ border-color:var(--accent-line); }}
.toggle input {{ accent-color:var(--green); }}
button.clear {{ background:transparent; border:1px solid var(--border-strong); color:var(--muted);
  border-radius:var(--radius-sm); padding:7px 13px; font-size:12.5px; cursor:pointer; transition:.12s; }}
button.clear:hover {{ color:var(--fg); border-color:var(--accent-line); background:var(--accent-soft); }}
#count {{ color:var(--faint); font-size:12px; margin-left:auto; white-space:nowrap;
  font-variant-numeric:tabular-nums; }}
#count b {{ color:var(--fg2); font-weight:600; }}

/* ---- repo list (card rows) ---- */
.scroller {{ flex:1; overflow:auto; padding:14px 28px 80px; }}
.listhead {{ display:grid; grid-template-columns:1fr 132px 92px 64px 92px;
  gap:14px; padding:0 18px 9px; font-size:10.5px; text-transform:uppercase; letter-spacing:.7px;
  color:var(--faint); font-weight:600; }}
.listhead .col {{ cursor:pointer; user-select:none; white-space:nowrap; transition:color .1s; }}
.listhead .col:hover {{ color:var(--fg2); }}
.listhead .col.active {{ color:var(--accent); }}
.listhead .num {{ text-align:right; }}
.listhead .arr {{ font-size:8px; }}

.list {{ display:flex; flex-direction:column; gap:6px; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:var(--radius);
  overflow:hidden; transition:border-color .12s, background .12s; }}
.card:hover {{ border-color:var(--border-strong); background:#181d25; }}
.card.open {{ border-color:var(--accent-line); background:#181d25; }}
.rowgrid {{ display:grid; grid-template-columns:1fr 132px 92px 64px 92px; gap:14px;
  align-items:center; padding:13px 18px; cursor:pointer; }}
.r-main {{ min-width:0; display:flex; align-items:center; gap:10px; }}
.r-caret {{ color:var(--faint); font-size:9px; transition:transform .12s; flex:0 0 9px; line-height:1; }}
.card.open .r-caret {{ transform:rotate(90deg); color:var(--accent); }}
.r-text {{ min-width:0; }}
.r-name {{ font-weight:600; font-size:14px; color:var(--mono-fg); display:block;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.r-name:hover {{ text-decoration:underline; }}
.r-desc {{ color:var(--muted); font-size:12px; margin-top:2px; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; }}
.r-cat {{ color:var(--faint); font-size:11.5px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.r-pills {{ display:flex; gap:6px; align-items:center; flex-wrap:wrap; }}
.r-stars {{ text-align:right; font-variant-numeric:tabular-nums; color:var(--fg2); font-size:13px; font-weight:600; }}
.r-stars .ico {{ color:var(--amber); font-size:11px; margin-right:3px; }}
.r-lift {{ text-align:right; }}
.r-lift .scale {{ font-size:10px; color:#3d444d; font-weight:500; }}

.pill {{ display:inline-flex; align-items:center; padding:2px 9px; border-radius:20px; font-size:11px;
  border:1px solid var(--border-strong); color:var(--muted); white-space:nowrap; font-weight:500; line-height:1.5; }}
.pill.lang {{ color:var(--fg2); }}
.pill.lang::before {{ content:""; width:8px; height:8px; border-radius:50%; background:var(--faint);
  margin-right:6px; }}
.lang-js::before,.lang-ts::before {{ background:#f1e05a; }}
.lang-py::before {{ background:#3572A5; }}
.lang-go::before {{ background:#00ADD8; }}
.lang-rust::before {{ background:#dea584; }}
.lang-rb::before {{ background:#701516; }}
.lang-java::before {{ background:#b07219; }}
.lang-c::before {{ background:#555; }}
.pill.lic {{ font-size:10.5px; }}
.pill.vt-CODE {{ color:var(--accent); border-color:var(--accent-line); background:var(--accent-soft); }}
.pill.vt-INFO {{ color:var(--pink); border-color:rgba(247,120,186,.3); background:rgba(247,120,186,.08); }}
.pill.vt-BOTH {{ color:#a371f7; border-color:rgba(163,113,247,.3); background:rgba(163,113,247,.08); }}
.lane-shippable {{ color:var(--green); border-color:rgba(63,185,80,.35); background:rgba(63,185,80,.1); }}
.lane-blocked {{ color:var(--red); border-color:rgba(248,81,73,.3); background:rgba(248,81,73,.08); }}
.lane-unknown, .lane-reference_only {{ color:var(--faint); }}
.lift {{ font-weight:700; font-variant-numeric:tabular-nums; font-size:13.5px; }}
.lift-hi {{ color:var(--green); }}
.lift-mid {{ color:var(--amber); }}
.lift-lo {{ color:var(--faint); }}
.lift .lbl {{ font-size:9.5px; color:var(--faint); font-weight:500; margin-left:2px; }}

/* ---- expanded contractcard ---- */
.exp {{ display:none; padding:0 18px 18px; }}
.card.open .exp {{ display:block; }}
.exp-inner {{ border-top:1px solid var(--border); padding-top:15px; margin-top:2px; }}
.exp .desc {{ color:var(--fg2); margin-bottom:13px; font-size:13px; line-height:1.6; max-width:80ch; }}
.ccard {{ background:rgba(63,185,80,.05); border:1px solid rgba(63,185,80,.22);
  border-radius:var(--radius-sm); padding:13px 15px; margin-bottom:13px; }}
.ccard .card-tag {{ display:inline-block; font-size:9.5px; color:var(--green); background:rgba(63,185,80,.14);
  border:1px solid rgba(63,185,80,.3); border-radius:5px; padding:2px 8px; margin-bottom:9px;
  text-transform:uppercase; letter-spacing:.7px; font-weight:600; }}
.ccard .card-ol {{ color:var(--fg); font-weight:600; font-size:13.5px; line-height:1.5; }}
.ccard ul {{ margin:11px 0 0; padding-left:2px; list-style:none;
  display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:5px 18px; }}
.ccard li {{ color:var(--fg2); font-size:12.5px; padding-left:18px; position:relative; }}
.ccard li::before {{ content:"›"; position:absolute; left:4px; color:var(--green); font-weight:700; }}
.exp .meta {{ display:flex; gap:22px; color:var(--muted); font-size:12px; flex-wrap:wrap; align-items:center; }}
.exp .meta .mlabel {{ color:var(--faint); text-transform:uppercase; letter-spacing:.5px; font-size:10px; margin-right:5px; }}
.exp .meta b {{ color:var(--fg2); font-weight:600; font-variant-numeric:tabular-nums; }}
.exp .meta .gh {{ margin-left:auto; }}

.empty {{ text-align:center; color:var(--muted); padding:70px 20px; }}
.empty .big {{ font-size:32px; opacity:.4; margin-bottom:10px; }}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="logo"></span>
    <span class="title">Foundry <small>value-mined OSS bank</small></span>
  </div>
  <div class="stats">
    <div class="stat"><b>{embedded:,}</b><span>saucy</span></div>
    <div class="stat"><b>{total_repos:,}</b><span>total repos</span></div>
    <div class="stat"><b>{categorized:,}</b><span>categorized</span></div>
    <div class="stat"><b>{categories:,}</b><span>categories</span></div>
    <div class="stat"><b>{winners:,}</b><span>winners</span></div>
    <div class="stat"><b>{contractcards:,}</b><span>contractcards</span></div>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="sbhdr">
      <span class="sbtitle">Categories</span>
      <span class="sbtools"><button id="expandall">expand</button><button id="collapseall">collapse</button></span>
    </div>
    <div class="treewrap"><div id="tree"></div></div>
  </aside>

  <section class="main">
    <div class="crumbs" id="crumbs"></div>
    <div class="titleblock" id="titleblock"></div>

    <div class="controls">
      <div class="searchbox">
        <input id="q" type="search" placeholder="Search all embedded repos by name, description, category…" autocomplete="off">
        <svg viewBox="0 0 24 24" fill="none" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
      </div>
      <select id="flang"><option value="">All languages</option></select>
      <select id="flic"><option value="">All licenses</option></select>
      <select id="fvt">
        <option value="">All value</option>
        <option value="CODE">CODE</option><option value="INFO">INFO</option>
        <option value="BOTH">BOTH</option><option value="NEITHER">NEITHER</option>
        <option value="unknown">unknown</option>
      </select>
      <select id="fll">
        <option value="">All lanes</option>
        <option value="shippable">shippable</option><option value="blocked">blocked</option>
        <option value="unknown">unknown</option><option value="reference_only">reference_only</option>
      </select>
      <select id="fuc"><option value="">All units</option></select>
      <div class="slider-wrap">lift &ge;&nbsp;<input id="flift" type="range" min="0" max="95" value="0" step="5"><span id="liftval" class="mono">0</span></div>
      <label class="toggle"><input id="cloneready" type="checkbox"> clone-ready</label>
      <button class="clear" id="clearbtn">Clear</button>
      <span id="count">…</span>
    </div>

    <div class="scroller">
      <div class="listhead">
        <div class="col" data-k="n">Repository</div>
        <div class="col" data-k="lg">Lang / License</div>
        <div class="col" data-k="vt">Value / Lane</div>
        <div class="col num" data-k="lf">Lift</div>
        <div class="col num" data-k="s">Stars</div>
      </div>
      <div class="list" id="rows"></div>
      <div id="empty" class="empty" style="display:none"><div class="big">∅</div>no repos match these filters</div>
    </div>
  </section>
</div>

<script>
"use strict";
var DATA = {data_json};
var TREE = {tree_json};      // flat node list: {{id,p,name,slug,level,ot,os,rt,rs}}
var ROOTS = {roots_json};    // root category ids
var LANGS = {langs_json};
var LICS = {lics_json};
var UNITS = {units_json};

// --- index tree ---
var NODE = {{}};       // id -> node
var KIDS = {{}};       // id -> [childId,...]
for (var i=0;i<TREE.length;i++) {{ NODE[TREE[i].id] = TREE[i]; }}
for (var i=0;i<TREE.length;i++) {{
  var n=TREE[i];
  if (n.p!=null && NODE[n.p]) {{ (KIDS[n.p]=KIDS[n.p]||[]).push(n.id); }}
}}
// sort children by rollup-saucy desc, then name
function sortKids(arr){{
  arr.sort(function(a,b){{
    var na=NODE[a], nb=NODE[b];
    if (nb.rs!==na.rs) return nb.rs-na.rs;
    return na.name<nb.name?-1:(na.name>nb.name?1:0);
  }});
}}
for (var k in KIDS) sortKids(KIDS[k]);
var ROOTLIST = ROOTS.slice(); sortKids(ROOTLIST);

// --- build index: which repos belong to each category id (saucy placement) ---
var BYCAT = {{}};   // catId -> [repo,...]
for (var i=0;i<DATA.length;i++) {{
  var r=DATA[i], cs=r.cs||[];
  for (var j=0;j<cs.length;j++) {{ (BYCAT[cs[j]]=BYCAT[cs[j]]||[]).push(r); }}
}}

// --- state ---
var selId = null;        // selected category id (null = global search / All)
var searchMode = false;  // true when search box drives a flat-across-all view
var sortKey="ov", sortDir=-1;

var rowsEl=document.getElementById("rows");
var emptyEl=document.getElementById("empty");
var countEl=document.getElementById("count");
var crumbsEl=document.getElementById("crumbs");
var titleEl=document.getElementById("titleblock");
var treeEl=document.getElementById("tree");
var qEl=document.getElementById("q");

// --- helpers ---
function nfmt(n){{ return (n||0).toLocaleString(); }}
function liftClass(v){{ return v>=70?"lift-hi":(v>=40?"lift-mid":"lift-lo"); }}
function laneClass(l){{ return "lane-"+(l||"unknown"); }}
function esc(s){{ return (s==null?"":String(s)).replace(/[&<>"]/g,function(c){{return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[c];}}); }}
// compact sidebar count: own (rollup) when they differ, else single
function badge(n){{ return n.os===n.rs ? nfmt(n.os) : nfmt(n.os)+" · "+nfmt(n.rs); }}
var LANG_SLUG={{javascript:"js",typescript:"ts",python:"py",go:"go",rust:"rust",ruby:"rb",java:"java",c:"c","c++":"c","c#":"c"}};
function langClass(l){{ return LANG_SLUG[(l||"").toLowerCase()]||""; }}

// --- populate dropdowns ---
function fill(id, arr) {{
  var sel = document.getElementById(id);
  for (var i=0;i<arr.length;i++) {{
    var o=document.createElement("option"); o.value=arr[i]; o.textContent=arr[i]; sel.appendChild(o);
  }}
}}
fill("flang",LANGS); fill("flic",LICS); fill("fuc",UNITS);

// ===================== TREE SIDEBAR =====================
function buildTree(){{
  var html = ROOTLIST.map(function(id){{ return treeNodeHTML(id, 0); }}).join("");
  treeEl.innerHTML = html;
}}
function treeNodeHTML(id, depth){{
  var n=NODE[id];
  var kids=KIDS[id]||[];
  var hasKids=kids.length>0;
  var pad = 9 + depth*16;
  var caret = hasKids ? '<span class="tcaret">▶</span>' : '<span class="tcaret leaf">•</span>';
  var inner =
    '<div class="trow" data-id="'+id+'" style="padding-left:'+pad+'px">'+
      caret+
      '<span class="tname">'+esc(n.name)+'</span>'+
      '<span class="tcount" title="own saucy · rollup saucy">'+badge(n)+'</span>'+
    '</div>';
  var children = "";
  if (hasKids){{
    children = '<div class="tchildren">'+kids.map(function(c){{return treeNodeHTML(c, depth+1);}}).join("")+'</div>';
  }}
  return '<div class="tnode tlvl'+n.level+'" data-node="'+id+'">'+inner+children+'</div>';
}}

// tree click: caret toggles expand; row body selects the node
treeEl.addEventListener("click", function(e){{
  var trow=e.target.closest(".trow");
  if (!trow) return;
  var id=parseInt(trow.getAttribute("data-id"),10);
  var nodeWrap=trow.parentElement; // .tnode
  if (e.target.classList.contains("tcaret") && !e.target.classList.contains("leaf")){{
    nodeWrap.classList.toggle("open");
    return;
  }}
  selectNode(id);
}});

function setSel(id){{
  var prev=treeEl.querySelector(".trow.sel");
  if (prev) prev.classList.remove("sel");
  if (id!=null){{
    var el=treeEl.querySelector('.trow[data-id="'+id+'"]');
    if (el){{ el.classList.add("sel");
      // open ancestors so the selection is visible
      var node=NODE[id];
      while (node && node.p!=null && NODE[node.p]){{
        var pw=treeEl.querySelector('.tnode[data-node="'+node.p+'"]');
        if (pw) pw.classList.add("open");
        node=NODE[node.p];
      }}
      el.scrollIntoView({{block:"nearest"}});
    }}
  }}
}}

document.getElementById("expandall").addEventListener("click", function(){{
  var all=treeEl.querySelectorAll(".tnode"); for (var i=0;i<all.length;i++) all[i].classList.add("open");
}});
document.getElementById("collapseall").addEventListener("click", function(){{
  var all=treeEl.querySelectorAll(".tnode"); for (var i=0;i<all.length;i++) all[i].classList.remove("open");
}});

// ===================== NAVIGATION =====================
function ancestry(id){{
  var path=[], n=NODE[id];
  while (n){{ path.unshift(n); n = (n.p!=null)?NODE[n.p]:null; }}
  return path;
}}

function selectNode(id){{
  selId=id;
  searchMode=false;
  if (qEl.value){{ qEl.value=""; qEl.classList.remove("searching"); }}
  setSel(id);
  render();
}}

function goAll(){{
  selId=null; searchMode=false;
  qEl.value=""; qEl.classList.remove("searching");
  setSel(null);
  render();
}}

// breadcrumbs — purely for climbing the tree
function renderCrumbs(){{
  if (searchMode){{
    crumbsEl.innerHTML='<a id="bcAll">All</a><span class="sep">/</span><span class="cur">Search</span>';
    document.getElementById("bcAll").onclick=goAll;
    return;
  }}
  if (selId==null){{ crumbsEl.innerHTML='<span class="cur">All categories</span>'; return; }}
  var path=ancestry(selId);
  var parts=['<a id="bcAll">All</a>'];
  for (var i=0;i<path.length;i++){{
    parts.push('<span class="sep">/</span>');
    if (i===path.length-1) parts.push('<span class="cur">'+esc(path[i].name)+'</span>');
    else parts.push('<a class="bcseg" data-id="'+path[i].id+'">'+esc(path[i].name)+'</a>');
  }}
  crumbsEl.innerHTML=parts.join("");
  document.getElementById("bcAll").onclick=goAll;
  var segs=crumbsEl.querySelectorAll(".bcseg");
  for (var s=0;s<segs.length;s++){{
    segs[s].onclick=function(){{ selectNode(parseInt(this.getAttribute("data-id"),10)); }};
  }}
}}

// big title block — category name + a subtle "N subcategories" hint (NO chip wall)
function renderTitle(){{
  if (searchMode){{
    titleEl.innerHTML='<h1>Search results</h1>'+
      '<div class="sub">across all <b>'+nfmt(DATA.length)+'</b> embedded repos'+
      '<span class="dot">·</span><span class="subhint">matching “'+esc(qEl.value.trim())+'”</span></div>';
    return;
  }}
  if (selId==null){{
    titleEl.innerHTML='<h1>All categories</h1>'+
      '<div class="sub"><b>'+nfmt(DATA.length)+'</b> saucy repos embedded'+
      '<span class="dot">·</span><b>'+nfmt(TREE.length)+'</b> categories'+
      '<span class="dot">·</span><span class="subhint">pick a category in the sidebar to scope</span></div>';
    return;
  }}
  var n=NODE[selId];
  var kids=KIDS[selId]||[];
  var sub='<b>'+nfmt(n.os)+'</b> repos here';
  if (n.rs>n.os) sub+='<span class="dot">·</span><b>'+nfmt(n.rs)+'</b> across this branch';
  if (kids.length) sub+='<span class="dot">·</span><span class="subhint">'+kids.length+
    ' subcategor'+(kids.length===1?'y':'ies')+' <span class="arrow">— expand in sidebar →</span></span>';
  titleEl.innerHTML='<h1>'+esc(n.name)+'</h1><div class="sub">'+sub+'</div>';
}}

// ===================== FILTERS + RENDER =====================
function getFilters(){{
  return {{
    q: qEl.value.trim().toLowerCase(),
    lang: document.getElementById("flang").value,
    lic: document.getElementById("flic").value,
    vt: document.getElementById("fvt").value,
    ll: document.getElementById("fll").value,
    uc: document.getElementById("fuc").value,
    lift: parseInt(document.getElementById("flift").value,10),
    clone: document.getElementById("cloneready").checked
  }};
}}

function passFilters(r,f){{
  if (f.clone) {{ if (!(r.lf>=70 && r.ll==="shippable")) return false; }}
  if (f.lang && r.lg!==f.lang) return false;
  if (f.lic && r.lc!==f.lic) return false;
  if (f.vt && r.vt!==f.vt) return false;
  if (f.ll && r.ll!==f.ll) return false;
  if (f.uc && r.uc!==f.uc) return false;
  if (r.lf < f.lift) return false;
  if (f.q) {{
    var hay = (r.n+" "+(r.d||"")+" "+(r.c||"")).toLowerCase();
    if (hay.indexOf(f.q)===-1) return false;
  }}
  return true;
}}

function sortRows(arr){{
  var k=sortKey, d=sortDir;
  arr.sort(function(a,b){{
    var av=a[k], bv=b[k];
    if (typeof av==="number" || typeof bv==="number") {{
      av=av||0; bv=bv||0; if (av!==bv) return (av-bv)*d;
      // tiebreak by overall_value desc
      return (b.ov||0)-(a.ov||0);
    }}
    av=(av||"").toString().toLowerCase(); bv=(bv||"").toString().toLowerCase();
    return av<bv?-1*d:(av>bv?1*d:0);
  }});
  return arr;
}}

// the base pool of repos depends on mode
function basePool(){{
  if (searchMode) return DATA;            // flat across everything
  if (selId==null) return DATA;           // "All" landing = whole embedded set
  return BYCAT[selId] || [];              // node's own saucy repos
}}

var MAX_RENDER = 1500;
function render(){{
  renderCrumbs();
  renderTitle();
  var f=getFilters();
  var pool=basePool();
  var out=[];
  for (var i=0;i<pool.length;i++){{ if (passFilters(pool[i],f)) out.push(pool[i]); }}
  var total=out.length;
  sortRows(out);
  var shown = out.slice(0, MAX_RENDER);
  var html=[];
  for (var kk=0;kk<shown.length;kk++){{ html.push(cardHTML(shown[kk],kk)); }}
  rowsEl.innerHTML = html.join("");
  emptyEl.style.display = total? "none":"block";
  document.querySelector(".listhead").style.display = total? "grid":"none";
  var capMsg = total>MAX_RENDER ? (" · first "+nfmt(MAX_RENDER)+" shown") : "";
  countEl.innerHTML = "<b>"+nfmt(total)+"</b> repos"+capMsg;
}}

function cardHTML(r,i){{
  var lift=r.lf;
  var langPill = r.lg ? '<span class="pill lang lang-'+langClass(r.lg)+'">'+esc(r.lg)+'</span>' : '';
  var licPill  = r.lc ? '<span class="pill lic">'+esc(r.lc)+'</span>' : '';
  var vtPill   = (r.vt && r.vt!=="unknown") ? '<span class="pill vt-'+esc(r.vt)+'">'+esc(r.vt)+'</span>' : '';
  var lanePill = '<span class="pill '+laneClass(r.ll)+'">'+esc(r.ll)+'</span>';
  return ''+
    '<div class="card" data-i="'+i+'">'+
      '<div class="rowgrid">'+
        '<div class="r-main">'+
          '<span class="r-caret">▶</span>'+
          '<div class="r-text">'+
            '<a class="r-name mono" href="'+esc(r.u)+'" target="_blank" rel="noopener" onclick="event.stopPropagation()">'+esc(r.n)+'</a>'+
            (r.d ? '<div class="r-desc">'+esc(r.d)+'</div>' : (r.c?'<div class="r-cat">'+esc(r.c)+'</div>':''))+
          '</div>'+
        '</div>'+
        '<div class="r-pills">'+langPill+licPill+'</div>'+
        '<div class="r-pills">'+vtPill+lanePill+'</div>'+
        '<div class="r-lift"><span class="lift '+liftClass(lift)+'">'+lift+'</span><span class="scale">/100</span></div>'+
        '<div class="r-stars"><span class="ico">★</span>'+nfmt(r.s)+'</div>'+
      '</div>'+
      expHTML(r)+
    '</div>';
}}

function expHTML(r){{
  var pv="";
  if (r.pv && r.pv.length){{
    pv='<ul>'; for (var j=0;j<r.pv.length;j++){{ pv+='<li>'+esc(r.pv[j])+'</li>'; }} pv+='</ul>';
  }}
  var card="";
  if (r.ol){{
    card='<div class="ccard"><span class="card-tag">ContractCard</span>'+
      '<div class="card-ol">'+esc(r.ol)+'</div>'+pv+'</div>';
  }}
  return ''+
    '<div class="exp"><div class="exp-inner">'+
      '<div class="desc">'+(r.d?esc(r.d):'<i style="color:#6e7681">no description</i>')+'</div>'+
      card+
      '<div class="meta">'+
        '<span><span class="mlabel">category</span><b>'+esc(r.c||"—")+'</b></span>'+
        '<span><span class="mlabel">unit</span><b>'+esc(r.uc)+'</b></span>'+
        '<span><span class="mlabel">value</span><b>'+(r.ov||0)+'</b></span>'+
        '<span><span class="mlabel">lift</span><b>'+r.lf+'</b></span>'+
        '<span class="gh"><a href="'+esc(r.u)+'" target="_blank" rel="noopener">Open on GitHub →</a></span>'+
      '</div>'+
    '</div></div>';
}}

// --- expand/collapse repo cards (event delegation) ---
rowsEl.addEventListener("click", function(e){{
  var card=e.target.closest(".card");
  if (!card) return;
  card.classList.toggle("open");
}});

// --- sorting (list header) ---
var cols=document.querySelectorAll(".listhead .col");
function setSortArrows(){{
  for (var u=0;u<cols.length;u++){{
    var c=cols[u];
    c.innerHTML=c.innerHTML.replace(/ <span class="arr">.*<\/span>/,"");
    c.classList.remove("active");
    if (c.getAttribute("data-k")===sortKey){{
      c.classList.add("active");
      c.innerHTML += ' <span class="arr">'+(sortDir>0?"▲":"▼")+'</span>';
    }}
  }}
}}
for (var t=0;t<cols.length;t++){{
  cols[t].addEventListener("click", function(){{
    var k=this.getAttribute("data-k");
    if (sortKey===k) sortDir=-sortDir; else {{ sortKey=k; sortDir=(k==="n"||k==="lg"||k==="vt")?1:-1; }}
    setSortArrows();
    render();
  }});
}}

// --- search box: typing flips to flat-search-all mode; clearing returns ---
var deb;
qEl.addEventListener("input", function(){{
  clearTimeout(deb);
  deb=setTimeout(function(){{
    var v=qEl.value.trim();
    if (v){{
      if (!searchMode){{ searchMode=true; qEl.classList.add("searching"); }}
    }} else {{
      if (searchMode){{ searchMode=false; qEl.classList.remove("searching"); }}
    }}
    render();
  }}, 120);
}});

["flang","flic","fvt","fll","fuc"].forEach(function(id){{
  document.getElementById(id).addEventListener("change", render);
}});
var liftEl=document.getElementById("flift");
liftEl.addEventListener("input", function(){{ document.getElementById("liftval").textContent=liftEl.value; render(); }});
document.getElementById("cloneready").addEventListener("change", render);
document.getElementById("clearbtn").addEventListener("click", function(){{
  qEl.value=""; qEl.classList.remove("searching"); searchMode=false;
  ["flang","flic","fvt","fll","fuc"].forEach(function(id){{ document.getElementById(id).value=""; }});
  liftEl.value=0; document.getElementById("liftval").textContent="0";
  document.getElementById("cloneready").checked=false;
  sortKey="ov"; sortDir=-1; setSortArrows(); render();
}});

// ===================== INIT =====================
buildTree();
// open the L0 roots by default so the 13 top categories are immediately visible
var topNodes=treeEl.querySelectorAll(".tlvl0");
for (var i=0;i<topNodes.length;i++) topNodes[i].classList.add("open");
setSortArrows();
goAll();   // land on "All" view
</script>
</body>
</html>
"""


def main():
    if not os.path.exists(DB):
        print(f"ERROR: DB not found: {DB}", file=sys.stderr)
        sys.exit(1)

    con = connect_ro()
    cur = con.cursor()
    try:
        stats = collect_stats(cur)
        tree, roots = collect_tree(cur)
        repos = collect_repos(cur)
    finally:
        con.close()

    html = render_html(stats, repos, tree, roots)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)

    size = os.path.getsize(OUT)
    print(json.dumps({
        "output": OUT,
        "size_bytes": size,
        "size_human": f"{size/1024/1024:.2f} MB",
        "embedded_repos": len(repos),
        "categories_in_tree": len(tree),
        "roots": len(roots),
        "stats": stats,
    }, indent=2))


if __name__ == "__main__":
    main()
