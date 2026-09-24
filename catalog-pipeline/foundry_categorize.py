#!/usr/bin/env python3
"""
Foundry categorize+rate pipeline — runs on MiniMax (off the Claude plan).

For each repo: fetch README (free HTTP) -> one MiniMax call (locked prompt + frozen
category tree + one-pass schema) -> deterministic clamp -> write rows to the DB.

Design:
- DB is the merge point + the checkpoint. Resumable: skips repos already in repo_category.
- Anthropic-compatible MiniMax endpoint (api.minimax.io/anthropic).
- Concurrency via thread pool; bounded so we don't hammer the quota.
- One call analyzes ONE repo (full README). Batching multiple READMEs/call is a later
  optimization; start 1/call for clean dogfooding + easy grading vs Opus ground truth.

Usage:
  python3 foundry_categorize.py --source work_top10k --limit 20        # dogfood
  python3 foundry_categorize.py --source work_top10k                   # full run
  python3 foundry_categorize.py --repos owner/a,owner/b                 # specific repos
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

# Direct-API token ledger (Build B): this script calls api.minimax.io DIRECTLY (bypassing
# Bifrost), so its tokens were never logged. log_usage() appends each call's usage to the
# foundry_usage_ledger that the Foundry usage tab reads. Best-effort; never breaks the run.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from foundry_usage_logger import log_usage
except Exception:
    def log_usage(*a, **k):  # logger missing → no-op, categorizer still runs
        return False

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")
ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
MODEL = "MiniMax-M3"
KEY = os.environ.get("MINIMAX_API_KEY", "")
CRITIQUE_ON = True  # toggled off by --no-critique for bulk runs

# ---------- category tree (loaded from DB, WITH example repos per leaf) ----------
def load_tree(con):
    rows = con.execute("SELECT id,slug,name,level,definition FROM category WHERE status='frozen' ORDER BY level,name").fetchall()
    # up to 3 example repos already placed in each category (highest-star), so the model places by analogy
    examples = {}
    for cid, fn in con.execute("""SELECT rc.category_id, rc.full_name FROM repo_category rc
                                  JOIN repo_card rp ON rp.full_name=rc.full_name
                                  WHERE rc.role='primary' ORDER BY rp.stars DESC"""):
        examples.setdefault(cid, [])
        if len(examples[cid]) < 3: examples[cid].append(fn)
    lines, slugs, name_to_slug = [], set(), {}
    for cid, slug, name, lvl, defn in rows:
        slugs.add(slug)
        name_to_slug[re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")] = slug
        ex = examples.get(cid, [])
        ex_str = f"  e.g. {', '.join(ex)}" if ex else ""
        if lvl == 0:
            lines.append(f"\n# {name} [{slug}]\n  {defn}" if defn else f"\n# {name} [{slug}]")
        else:
            lines.append(f"  - {name} [{slug}]: {defn}{ex_str}")
    return "\n".join(lines), slugs, name_to_slug

def load_precedence():
    p = os.path.join(HERE, "precedence_rules.txt")
    try:
        return open(p).read().strip()
    except Exception:
        return ""

# ---------- README fetch (free, not a billed call) ----------
def fetch_readme(full_name):
    # try common raw paths first (free, fast)
    for url in (f"https://raw.githubusercontent.com/{full_name}/HEAD/README.md",
                f"https://raw.githubusercontent.com/{full_name}/main/README.md",
                f"https://raw.githubusercontent.com/{full_name}/master/README.md",
                f"https://raw.githubusercontent.com/{full_name}/HEAD/readme.md",
                f"https://raw.githubusercontent.com/{full_name}/HEAD/README.rst",
                f"https://raw.githubusercontent.com/{full_name}/HEAD/README"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "foundry-bot"})
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = r.read().decode("utf-8", "replace")
                if txt.strip():
                    return txt[:24000]
        except Exception:
            continue
    # fallback: GitHub API resolves any branch/path/casing for the README
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{full_name}/readme",
            headers={"User-Agent": "foundry-bot", "Accept": "application/vnd.github.raw"})
        gh = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if gh: req.add_header("Authorization", f"Bearer {gh}")
        with urllib.request.urlopen(req, timeout=20) as r:
            txt = r.read().decode("utf-8", "replace")
            if txt.strip():
                return txt[:24000]
    except Exception:
        pass
    return None

PROMPT = """You analyze a GitHub repo for "Foundry" — a bank of the most valuable repos (valuable as reusable CODE or as curated INFORMATION; both count fully, never zero a non-code repo).

Read the README below (the WHOLE README) plus the repo facts. Do NOT use GitHub topics. Judge from the README + facts.

REPO FACTS:
- url: {url}
- stars: {stars}   language: {language}   forks: {forks}
- created: {created}   last pushed: {pushed}   archived: {archived}
- homepage: {homepage}
- description: {description}
- license (badge, may be wrong): {license}
(Use stars/age/last-push to judge maturity & liveness when scoring value: high-star + recently-pushed = mature/alive; old + long-stale or archived = dying. Don't let stars alone inflate value if the README shows it's thin.)

First THINK in a "reasoning" field (2-4 sentences): what KIND of thing is this repo really? what is its primary value (code to lift, or curated info)? does an EXISTING leaf genuinely describe it, or would you be stretching an unrelated bucket? Look at the example repos listed under each leaf — place by analogy to them. Then emit the rest.

Emit ONE JSON object (no prose, no markdown fences):
- reasoning: your 2-4 sentence think-through (above)
- confidence: 0-100, how sure you are of the category placement (be honest; low if the README was thin or no leaf fit well)
- category_slug: the SINGLE best-fit LEAF category from the frozen tree (the [slug] in brackets, the indented sub-buckets). ALWAYS prefer a specific leaf over a top-level bucket. Only use a top-level bucket slug if truly no leaf under it fits.
- propose_new: if NO existing leaf fits this repo well (it's a genuinely distinct KIND not covered), set this to an object {{"name":"<New Category Name>","parent_slug":"<best top-level bucket slug>","definition":"<1-line, generic enough 3+ repos would match>"}}; otherwise set propose_new to null. When you propose_new, still set category_slug to the closest existing leaf as a fallback.
- reuse_value 0-100: can you lift code/components/patterns from it?
- info_value 0-100: is it curated knowledge worth having?
- overall_value: = max(reuse_value, info_value)
- value_type: one of CODE | INFO | BOTH | NEITHER
- license_reality: the ACTUAL reusability license (flag copyleft/no-license/separate-content-terms), not just a badge
- one_line: what it is good for / why bank it (<=160 chars)
- saucy: 1 if exceptionally valuable/rare/load-bearing else 0

CRITICAL on placement: only place into an existing leaf if that leaf's DEFINITION genuinely describes this repo's kind. Do NOT stretch an unrelated bucket to force a fit. If the repo's true kind has no matching leaf, you MUST set propose_new (and pick the closest existing leaf only as a weak fallback). The frozen tree was built from a small sample and is KNOWN to be missing whole kinds — e.g. databases (postgres/redis are NOT "backend-data-platform"), security/pentest tools, blockchain/crypto nodes (bitcoin is NOT an "os-kernel"), datasets, data-science libraries, monitoring/observability, message queues, mobile SDKs. When you see one of these or any other kind the tree lacks, propose_new rather than mis-filing. Never flag a repo as fake from any prior assumption — judge only by the README.

OVERLAP PRECEDENCE RULES (use these to break ties when a repo could fit two kinds):
{precedence}

FROZEN CATEGORY TREE (top kinds have POSITIVE/NEGATIVE tests; each leaf shows example repos — match by analogy and obey the tests):
{tree}

REPO: {full_name}

README (the whole thing):
{readme}

Return ONLY the JSON object."""

# ---- batched prompt: analyze N repos in ONE call (1 credit instead of N) ----
BATCH_HEADER = """You analyze MULTIPLE GitHub repos for "Foundry" — a bank of the most valuable repos (valuable as reusable CODE or curated INFORMATION; both count, never zero a non-code repo). Do NOT use GitHub topics; judge each from its README + facts.

For EACH repo below, emit one analysis object. Same rules for every repo:
- reasoning: 1-3 sentences — what KIND is it, primary value, which leaf fits (or none).
- confidence 0-100 on the category placement.
- category_slug: SINGLE best-fit LEAF from the tree (prefer a specific leaf/sub-bucket over a top-level bucket). Place by analogy to each leaf's example repos and OBEY the POSITIVE/NEGATIVE tests.
- propose_new: {{"name":..,"parent_slug":..,"definition":..}} ONLY if no existing leaf genuinely fits (a distinct missing KIND); else null. Still set category_slug to closest leaf as fallback.
- reuse_value, info_value 0-100; overall_value = max of the two.
- value_type: CODE | INFO | BOTH | NEITHER
- license_reality: ACTUAL reusability license (flag copyleft/no-license), not just the badge.
- one_line: <=160 chars.
- saucy: 1 if exceptionally valuable/rare else 0.
Do NOT stretch an unrelated bucket to force a fit — propose_new instead. Never flag a repo fake from a prior assumption; judge only by its README.

OVERLAP PRECEDENCE RULES:
{precedence}

FROZEN CATEGORY TREE (top kinds have POSITIVE/NEGATIVE tests; leaves show example repos):
{tree}

Below are {n} repos. Return ONLY a JSON ARRAY of {n} objects, each with a "full_name" field matching the repo plus all fields above. No prose, no fences.

=== REPOS ===
{repos_block}
"""

def call_minimax(prompt, max_tokens=1400, retries=6):
    body = json.dumps({
        "model": MODEL, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            usage = data.get("usage", {}) or {}
            # log direct-API token usage to the Foundry ledger (input_tokens/output_tokens
            # are the Anthropic-compat fields MiniMax returns). Best-effort; never raises.
            log_usage("minimax", MODEL, usage.get("input_tokens", 0), usage.get("output_tokens", 0))
            return text, usage
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
            body_txt = ""
            try: body_txt = e.read().decode()[:200]
            except Exception: pass
            last = f"HTTP {e.code}: {body_txt}"
            if e.code == 429 or e.code >= 500:  # rate limit / quota window / transient server error
                # honor Retry-After if given, else exponential backoff capped at 5min (quota windows can be long)
                wait = int(ra) if (ra and ra.isdigit()) else min(300, 8 * (2 ** attempt))
                print(f"    [retry {attempt+1}/{retries}] {e.code}, waiting {wait}s", flush=True)
                time.sleep(wait)
            else:  # 4xx other than 429 = bad request, don't hammer
                time.sleep(3)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"net: {e}"
            time.sleep(min(60, 5 * (2 ** attempt)))  # network blip -> exponential
        except Exception as e:
            last = str(e); time.sleep(3)
    return None, {"error": last}

def parse_json(text):
    if not text: return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m: return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None

def clamp(a):
    """Fix the M3/Haiku-class overall_value inflation found in the bake-off."""
    r = a.get("reuse_value", 0) or 0
    i = a.get("info_value", 0) or 0
    a["overall_value"] = max(r, i)  # enforce the rule
    lic = (a.get("license_reality") or "").lower()
    copyleft_or_none = any(k in lic for k in ("agpl", "gpl", "no license", "no-license", "noassertion", "all rights reserved", "separate"))
    if copyleft_or_none:
        a["overall_value"] = min(a["overall_value"], r + 25)
    if a.get("value_type") == "CODE":
        a["overall_value"] = min(a["overall_value"], r + 10)
    a["overall_value"] = max(0, min(100, int(a["overall_value"])))
    return a

def slug_to_id(con, slug, slugs, name_to_slug):
    slug = (slug or "").strip().lower()
    if slug not in slugs:
        # fuzzy: the model sometimes slugifies the NAME instead of using the real slug.
        if slug in name_to_slug:
            slug = name_to_slug[slug]
        else:
            # substring / suffix match against known slugs (e.g. 'standalone-agent-platform' -> 'agent-platform')
            cand = [s for s in slugs if s in slug or slug.endswith(s) or s.endswith(slug)]
            if len(cand) == 1:
                slug = cand[0]
            else:
                return None
    row = con.execute("SELECT id FROM category WHERE slug=? AND status='frozen'", (slug,)).fetchone()
    return row[0] if row else None

CRITIQUE = """You previously analyzed this repo but were not fully confident, or landed on a coarse top-level bucket, or proposed a new category. Re-examine ONLY the category placement — keep everything else.

Your previous FULL answer (JSON): {prev}

The frozen category tree (leaves show example repos — place by analogy):
{tree}

REPO: {full_name}

Re-decide ONLY these:
- category_slug: is there a SPECIFIC LEAF that fits better? Never settle for a top-level bucket if a leaf fits. Place by analogy to the example repos.
- propose_new: were you right to propose a new category, or does an existing leaf actually cover it? Only keep propose_new if truly no leaf fits.

Return the COMPLETE JSON object with the SAME fields and SAME values as your previous answer, changing ONLY category_slug and/or propose_new (and reasoning to explain the change). Do NOT change reuse_value, info_value, overall_value, or value_type — copy them exactly from your previous answer. value_type MUST stay one of: CODE, INFO, BOTH, NEITHER. Return ONLY the JSON object."""

def process(facts, tree, slugs, top_slugs, precedence):
    full_name = facts["full_name"]
    readme = fetch_readme(full_name)
    if not readme:
        return full_name, None, "README fetch failed"
    prompt = PROMPT.format(
        tree=tree, precedence=precedence, full_name=full_name, url=facts.get("url") or f"https://github.com/{full_name}",
        stars=facts.get("stars"), language=facts.get("language") or "?", forks=facts.get("forks"),
        created=(facts.get("created_at") or "?")[:10], pushed=(facts.get("pushed_at") or "?")[:10],
        archived=("yes" if facts.get("archived") else "no"), homepage=facts.get("homepage") or "none",
        description=facts.get("description") or "", license=facts.get("license") or "none", readme=readme)
    text, usage = call_minimax(prompt)
    a = parse_json(text)
    if not a:
        return full_name, None, f"parse failed (usage={usage})"
    # CONDITIONAL CRITIQUE: only re-run the shaky ones (cheap on the easy 80%). Off for bulk runs.
    shaky = CRITIQUE_ON and ((a.get("confidence", 100) < 70) or (a.get("category_slug","") in top_slugs) or bool(a.get("propose_new")))
    if shaky:
        c_prompt = CRITIQUE.format(prev=json.dumps(a), tree=tree, full_name=full_name)
        c_text, _ = call_minimax(c_prompt)
        c = parse_json(c_text)
        if c:
            # critique only owns category/propose_new; preserve the original scores defensively
            for k in ("reuse_value","info_value","value_type","license_reality","one_line","saucy"):
                if k not in c or c.get(k) in (None, ""): c[k] = a.get(k)
            if c.get("value_type") not in ("CODE","INFO","BOTH","NEITHER"): c["value_type"] = a.get("value_type")
            a = c; a["_critiqued"] = True
    a = clamp(a)
    a["full_name"] = full_name
    return full_name, a, None

def parse_json_array(text):
    if not text: return None
    m = re.search(r"\[.*\]", text, re.S)
    if not m: return None
    try: return json.loads(m.group(0))
    except Exception: return None

def process_batch(facts_list, tree, slugs, top_slugs, precedence):
    """Analyze a batch of repos in ONE API call (1 credit for N repos). Returns list of (full_name, analysis|None, err)."""
    blocks, valid = [], []
    for facts in facts_list:
        fn = facts["full_name"]
        readme = fetch_readme(fn)
        if not readme:
            continue  # handled below as a fail
        valid.append(fn)
        blocks.append(
            f"--- REPO: {fn} ---\n"
            f"url: {facts.get('url') or 'https://github.com/'+fn} | stars: {facts.get('stars')} | lang: {facts.get('language') or '?'} | "
            f"created: {(facts.get('created_at') or '?')[:10]} | pushed: {(facts.get('pushed_at') or '?')[:10]} | "
            f"archived: {'yes' if facts.get('archived') else 'no'} | license-badge: {facts.get('license') or 'none'}\n"
            f"description: {facts.get('description') or ''}\n"
            f"README:\n{readme[:14000]}\n")
    fetched = set(valid)
    fails = [(f["full_name"], None, "README fetch failed") for f in facts_list if f["full_name"] not in fetched]
    if not blocks:
        return fails
    prompt = BATCH_HEADER.format(precedence=precedence, tree=tree, n=len(blocks), repos_block="\n".join(blocks))
    # scale output tokens with batch size
    text, usage = call_minimax(prompt, max_tokens=400 + 320 * len(blocks))
    arr = parse_json_array(text)
    if not arr:
        return fails + [(fn, None, f"batch parse failed (usage={usage})") for fn in valid]
    by_name = {}
    for obj in arr:
        if isinstance(obj, dict) and obj.get("full_name"):
            by_name[obj["full_name"]] = obj
    out = list(fails)
    for fn in valid:
        a = by_name.get(fn)
        if not a:
            out.append((fn, None, "missing from batch response"))
            continue
        a = clamp(a); a["full_name"] = fn
        out.append((fn, a, None))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="work_top10k")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--repos", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--agent-id", default="minimax-m27")
    ap.add_argument("--no-critique", action="store_true", help="disable per-repo self-critique (faster bulk)")
    ap.add_argument("--rank-start", type=int, default=0)
    ap.add_argument("--rank-end", type=int, default=0)
    ap.add_argument("--batch", type=int, default=1, help="repos per API call (1 credit per call). 10 = 10x credit efficiency")
    args = ap.parse_args()
    global CRITIQUE_ON
    if args.no_critique: CRITIQUE_ON = False

    if not KEY:
        sys.exit("MINIMAX_API_KEY not set. Run: source ~/.config/siso-secrets/minimax.env")

    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    tree, slugs, name_to_slug = load_tree(con)
    precedence = load_precedence()
    top_slugs = {r[0] for r in con.execute("SELECT slug FROM category WHERE level=0 AND status='frozen'").fetchall()}

    con.row_factory = sqlite3.Row
    FIELDS = "full_name,url,stars,language,forks,created_at,pushed_at,archived,homepage,description,license"
    if args.repos:
        names = [r.strip() for r in args.repos.split(",") if r.strip()]
        q = f"SELECT {FIELDS} FROM repo_card WHERE full_name IN ({','.join('?'*len(names))})"
        rows = con.execute(q, names).fetchall()
    else:
        done = {r[0] for r in con.execute("SELECT DISTINCT full_name FROM repo_category WHERE agent_id=?", (args.agent_id,)).fetchall()}
        # slice the source by rank window (for waves); work_top tables have a 'rank' column
        rank_filter = ""
        if args.rank_start or args.rank_end:
            lo = args.rank_start or 1
            hi = args.rank_end or 10**9
            rank_filter = f" AND s.rank BETWEEN {lo} AND {hi}"
        q = (f"SELECT {','.join('rp.'+c for c in FIELDS.split(','))} "
             f"FROM repo_card rp JOIN {args.source} s ON s.full_name=rp.full_name "
             f"WHERE 1=1{rank_filter} ORDER BY s.rank")
        rows = [r for r in con.execute(q).fetchall() if r["full_name"] not in done]
        if args.limit:
            rows = rows[:args.limit]
    facts_list = [dict(r) for r in rows]
    con.row_factory = None

    bs = max(1, args.batch)
    print(f"[foundry] {len(facts_list)} repos | model={MODEL} | workers={args.workers} | batch={bs} (~{(len(facts_list)+bs-1)//bs} API calls)", flush=True)
    ok = fail = proposed = coarse = 0
    t0 = time.time()

    def result_stream(ex):
        if bs == 1:
            futs = {ex.submit(process, f, tree, slugs, top_slugs, precedence): f["full_name"] for f in facts_list}
            for fut in as_completed(futs):
                yield fut.result()
        else:
            batches = [facts_list[i:i+bs] for i in range(0, len(facts_list), bs)]
            futs = [ex.submit(process_batch, b, tree, slugs, top_slugs, precedence) for b in batches]
            for fut in as_completed(futs):
                for r in fut.result():
                    yield r

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for full_name, a, err in result_stream(ex):
            if err or not a:
                fail += 1
                print(f"  FAIL {full_name}: {err}", flush=True)
                continue
            # New-category proposal path: M3 flagged this repo doesn't fit any existing leaf.
            pn = a.get("propose_new")
            if isinstance(pn, dict) and pn.get("name"):
                con.execute("""INSERT INTO category_proposal
                    (full_name,proposed_name,suggested_parent,rationale,status,agent_id,created_at)
                    VALUES (?,?,?,?,'pending',?,datetime('now'))""",
                    (full_name, pn.get("name")[:80], pn.get("parent_slug"),
                     (pn.get("definition") or a.get("one_line",""))[:300], args.agent_id))
                proposed += 1
                print(f"  PROPOSE {full_name}: new cat '{pn.get('name')}' under {pn.get('parent_slug')}", flush=True)
            cid = slug_to_id(con, a.get("category_slug", ""), slugs, name_to_slug)
            if cid is None:
                fail += 1
                print(f"  BADSLUG {full_name}: '{a.get('category_slug')}'", flush=True)
                continue
            # Altitude guard: flag top-level-bucket placements (a leaf likely exists) for review.
            alt = a.get("category_slug","") in top_slugs
            if alt: coarse += 1
            lic = (a.get("license_reality") or "")[:40]
            vt = a.get("value_type") or "?"
            ol = a.get("one_line") or ""
            why = (("[COARSE] " if alt else "") + f"[{vt}|{lic}] {ol}")[:300]
            try:
                con.execute("""INSERT OR REPLACE INTO repo_category
                    (full_name,category_id,role,confidence,reuse_value,info_value,overall_value,saucy,why,agent_id,built_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
                    (full_name, cid, "primary", (60 if alt else 85), a.get("reuse_value"), a.get("info_value"),
                     a.get("overall_value"), a.get("saucy", 0), why, args.agent_id))
                con.commit()
                ok += 1
                print(f"  OK   {full_name:42s} -> {a['category_slug']:26s} reuse={a.get('reuse_value')} info={a.get('info_value')} ov={a.get('overall_value')} {vt}", flush=True)
            except Exception as e:
                fail += 1
                print(f"  WRITEFAIL {full_name}: {e}", flush=True)
    dt = time.time() - t0
    print(f"[foundry] done: {ok} ok, {fail} fail, {proposed} new-cat proposals, {coarse} coarse(top-level), {dt:.0f}s ({dt/max(1,ok+fail):.1f}s/repo)", flush=True)
    con.close()

if __name__ == "__main__":
    main()
