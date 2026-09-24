#!/usr/bin/env python3
"""Export the frozen category tree + placements from the DB to a human-readable HTML view.
DB is the source of truth; this is a generated export. Re-run whenever the tree changes."""
import sqlite3, html, os

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")
OUT = "/Users/shaansisodia/SISO_Workspace/SISO_Agent_Base/foundry/brief/REPO-CATEGORY-TAXONOMY.html"

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
cats = con.execute("SELECT id,parent_id,name,slug,definition,level FROM category WHERE status='frozen' ORDER BY level,name").fetchall()
# placements per category
counts = {r["category_id"]: r["n"] for r in con.execute("SELECT category_id, COUNT(*) n FROM repo_category GROUP BY category_id")}
repos_by_cat = {}
for r in con.execute("""SELECT rc.category_id, rc.full_name, rc.reuse_value, rc.info_value, rc.overall_value, rc.saucy
                        FROM repo_category rc ORDER BY rc.overall_value DESC NULLS LAST, rc.full_name"""):
    repos_by_cat.setdefault(r["category_id"], []).append(r)

tops = [c for c in cats if c["level"] == 0]
subs = {}
for c in cats:
    if c["parent_id"] is not None:
        subs.setdefault(c["parent_id"], []).append(c)

def esc(s): return html.escape(str(s if s is not None else ""))

total_cats = len(cats); total_placed = sum(counts.values())
parts = [f"""<!doctype html><html lang=en><meta charset=utf-8><title>Foundry Category Taxonomy</title>
<style>body{{font:14px/1.6 -apple-system,system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 20px;color:#1a1a1a}}
h1{{border-bottom:3px solid #333;padding-bottom:8px}}h2{{margin-top:26px;color:#0a4}}h3{{margin:14px 0 4px;color:#357}}
.def{{color:#777;font-size:12.5px;margin:0 0 6px}}.count{{color:#999;font-weight:normal;font-size:12px}}
table{{border-collapse:collapse;width:100%;margin:4px 0 14px}}th,td{{border:1px solid #e3e3e3;padding:4px 8px;text-align:left;font-size:12.5px}}
th{{background:#f6f6f6}}.saucy{{background:#fff3cd}}a{{color:#06c;text-decoration:none}}.empty{{color:#bbb;font-style:italic}}</style>
<body><h1>🗂️ Foundry Category Taxonomy <span class=count>({total_cats} categories · {total_placed} repos placed · generated from DB)</span></h1>
<p class=count>Source of truth = <code>category</code> + <code>repo_category</code> tables in identity.sqlite. This file is an export — do not hand-edit.</p>"""]

for top in tops:
    n = counts.get(top["id"], 0)
    sub_total = n + sum(counts.get(s["id"],0) for s in subs.get(top["id"],[]))
    parts.append(f"<h2>{esc(top['name'])} <span class=count>({sub_total} repos)</span></h2>")
    parts.append(f"<p class=def>{esc(top['definition'])}</p>")
    # repos placed directly at top level
    for cat in [top] + sorted(subs.get(top["id"],[]), key=lambda x:-counts.get(x["id"],0)):
        rs = repos_by_cat.get(cat["id"], [])
        if cat["id"] != top["id"]:
            parts.append(f"<h3>{esc(cat['name'])} <span class=count>({len(rs)})</span></h3>")
            parts.append(f"<p class=def>{esc(cat['definition'])}</p>")
        if not rs:
            if cat["id"] != top["id"]: parts.append("<p class=empty>— no repos yet —</p>")
            continue
        parts.append("<table><tr><th>repo</th><th>reuse</th><th>info</th><th>overall</th></tr>")
        for r in rs:
            cls = " class=saucy" if r["saucy"] else ""
            ov = r["overall_value"] if r["overall_value"] is not None else "—"
            ru = r["reuse_value"] if r["reuse_value"] is not None else "—"
            inf = r["info_value"] if r["info_value"] is not None else "—"
            parts.append(f"<tr{cls}><td><a href='https://github.com/{esc(r['full_name'])}'>{esc(r['full_name'])}</a></td><td>{ru}</td><td>{inf}</td><td>{ov}</td></tr>")
        parts.append("</table>")

parts.append("</body></html>")
open(OUT,"w").write("\n".join(parts))
print(f"wrote {OUT}  ({total_cats} cats, {total_placed} placed)")
con.close()
