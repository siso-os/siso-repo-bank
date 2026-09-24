#!/usr/bin/env python3
"""
Foundry extraction funnel — Layer 1: deterministic topic/description router.

Free, no fetch, no model. Runs over every repo_card and assigns >=0 capability
families by matching the mined taxonomy's signal keywords against the repo's
topics (author-assigned ground truth) and description (weaker, marketing text).

Exit buckets per repo:
  clean     = exactly 1 family            -> done forever, never needs a model
  ambiguous = >=2 families                -> needs L2 deps / L3 cluster / L4 read
  dark      = 0 families                  -> needs L2 deps / L3 cluster / L4 read

Outputs:
  - writes family_tags + match_source + bucket back into a new l1_route table
  - prints aggregate coverage by bucket and star tier
  - dumps a ~200-row stratified HTML sample (clean/ambiguous/dark x star tiers)
    for human eyeballing at staging/l1-sample.html

Usage: python3 l1_router.py
"""
import json, sqlite3, re, html, os, hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")
TAX = "/tmp/foundry_taxonomy.json"
OUT_HTML = os.path.join(HERE, "staging", "l1-sample.html")

# topic-match is high-confidence (author tagged it); desc-match is a substring
# on a word boundary so "api" doesn't fire inside "rapid".
def load_taxonomy():
    fams = json.load(open(TAX))
    # normalize signals to lowercase; build per-family signal sets
    out = []
    for f in fams:
        sigs = [s.strip().lower() for s in f["signals"] if s.strip()]
        out.append({"family": f["family"], "signals": sigs})
    return out

def star_tier(stars):
    if stars is None: return "unknown"
    if stars >= 10000: return ">=10000"
    if stars >= 1000: return "1000-9999"
    if stars >= 500: return "500-999"
    if stars >= 100: return "100-499"
    return "<100"

def route(topics, desc, taxonomy):
    """Return (family_tags, match_source). topics is a set; desc is lowercased str."""
    topics = set(t.lower() for t in topics)
    desc = (desc or "").lower()
    hits = {}  # family -> source ('topic' wins over 'desc')
    for f in taxonomy:
        fam = f["family"]
        src = None
        for sig in f["signals"]:
            if sig in topics:
                src = "topic"; break
        if src is None:
            for sig in f["signals"]:
                # word-boundary substring on description
                if re.search(r"(?<![a-z0-9])" + re.escape(sig) + r"(?![a-z0-9])", desc):
                    src = "desc"; break
        if src:
            hits[fam] = src
    if not hits:
        return [], "none"
    src = "topic" if "topic" in hits.values() else "desc"
    return list(hits.keys()), src

def bucket(tags):
    n = len(tags)
    if n == 0: return "dark"
    if n == 1: return "clean"
    return "ambiguous"

def main():
    taxonomy = load_taxonomy()
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("DROP TABLE IF EXISTS l1_route")
    cur.execute("""CREATE TABLE l1_route(
        canonical_id TEXT PRIMARY KEY, full_name TEXT, stars INTEGER,
        bucket TEXT, family_tags TEXT, match_source TEXT, star_tier TEXT)""")

    rows = cur.execute("SELECT canonical_id, full_name, stars, description, topics_json FROM repo_card").fetchall()

    agg = {}        # (bucket, tier) -> count
    bucket_tot = {"clean":0,"ambiguous":0,"dark":0}
    fam_count = {}  # family -> count (for precision sampling later)
    samples = {}    # (bucket, tier) -> list of rows (cap small)
    SAMPLE_CAP = 12

    ins = []
    for r in rows:
        try:
            topics = json.loads(r["topics_json"]) if r["topics_json"] else []
        except Exception:
            topics = []
        tags, src = route(topics, r["description"], taxonomy)
        b = bucket(tags); tier = star_tier(r["stars"])
        bucket_tot[b] += 1
        agg[(b,tier)] = agg.get((b,tier),0)+1
        for fam in tags: fam_count[fam] = fam_count.get(fam,0)+1
        ins.append((r["canonical_id"], r["full_name"], r["stars"], b, json.dumps(tags), src, tier))
        key = (b, tier)
        if tier in (">=10000","1000-9999","500-999","100-499"):
            samples.setdefault(key, [])
            if len(samples[key]) < SAMPLE_CAP:
                samples[key].append({
                    "full_name": r["full_name"], "stars": r["stars"],
                    "description": r["description"] or "",
                    "topics": topics[:12], "tags": tags, "src": src, "bucket": b,
                })

    cur.executemany("INSERT INTO l1_route VALUES (?,?,?,?,?,?,?)", ins)
    con.commit()

    total = len(rows)
    print(f"=== L1 ROUTER — {total} repos ===")
    for b in ("clean","ambiguous","dark"):
        print(f"  {b:10s}: {bucket_tot[b]:>7,} ({100*bucket_tot[b]/total:5.1f}%)")
    print("\n=== by bucket x star tier ===")
    tiers = [">=10000","1000-9999","500-999","100-499","<100","unknown"]
    print(f"  {'tier':12s} {'clean':>9s} {'ambig':>9s} {'dark':>9s}")
    for tier in tiers:
        c = agg.get(("clean",tier),0); a = agg.get(("ambiguous",tier),0); d = agg.get(("dark",tier),0)
        if c+a+d: print(f"  {tier:12s} {c:>9,} {a:>9,} {d:>9,}")
    print("\n=== top 24 families by count ===")
    for fam,n in sorted(fam_count.items(), key=lambda x:-x[1]):
        print(f"  {n:>8,}  {fam}")

    # ---- HTML sample ----
    def esc(s): return html.escape(str(s))
    parts = ["""<!doctype html><meta charset=utf-8><title>Foundry L1 sample</title>
<style>body{font:13px/1.5 -apple-system,system-ui,sans-serif;margin:24px;color:#1a1a1a}
h2{margin:28px 0 6px}table{border-collapse:collapse;width:100%;margin:6px 0 18px}
th,td{border:1px solid #ddd;padding:5px 8px;vertical-align:top;text-align:left}
th{background:#f4f4f4}td.tags{color:#0a6}td.dark{color:#b00}.src{font-size:11px;color:#888}
.topics{color:#557;font-size:11px}tr:nth-child(even){background:#fafafa}
.legend{background:#fffbe6;border:1px solid #e8d98a;padding:10px 14px;border-radius:6px}</style>
<h1>Foundry L1 deterministic router — eyeball sample</h1>
<div class=legend><b>Judge three things:</b> (1) <b>clean</b> rows — is the single family specific & correct, or coverage theater?
(2) <b>ambiguous</b> rows — genuinely multi-family or sloppy rule? (3) <b>dark</b> rows — truly no signal (L2 deps justified) or did the rule miss obvious words?</div>"""]
    # data-verify contract block
    parts.append(f'<div data-verify-total="{total}" data-verify-clean="{bucket_tot["clean"]}" data-verify-ambiguous="{bucket_tot["ambiguous"]}" data-verify-dark="{bucket_tot["dark"]}"></div>')
    for b in ("clean","ambiguous","dark"):
        for tier in (">=10000","1000-9999","500-999","100-499"):
            rs = samples.get((b,tier),[])
            if not rs: continue
            parts.append(f"<h2>{esc(b)} — {esc(tier)} stars ({len(rs)} shown)</h2>")
            parts.append("<table><tr><th>repo</th><th>★</th><th>description</th><th>topics</th><th>family_tags</th><th>src</th></tr>")
            for x in rs:
                tagcls = "dark" if not x["tags"] else "tags"
                parts.append(
                    f"<tr><td><a href='https://github.com/{esc(x['full_name'])}'>{esc(x['full_name'])}</a></td>"
                    f"<td>{esc(x['stars'])}</td><td>{esc(x['description'][:160])}</td>"
                    f"<td class=topics>{esc(', '.join(x['topics']))}</td>"
                    f"<td class={tagcls}>{esc(' | '.join(x['tags']) or '—')}</td>"
                    f"<td class=src>{esc(x['src'])}</td></tr>")
            parts.append("</table>")
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    open(OUT_HTML,"w").write("\n".join(parts))
    print(f"\nHTML sample -> {OUT_HTML}")
    con.close()

if __name__ == "__main__":
    main()
