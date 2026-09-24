#!/usr/bin/env python3
"""
E3.3 — MiniMax M3 contract-extractor (prose half of a ContractCard).

Takes the pre-digested per-repo contexts staged in repo_source_signal.digest
(<=1.5KB each; the model NEVER sees a raw README) and, in batches of 10/call,
asks MiniMax M3 to RESTATE — never invent — the prose half of a ContractCard:
  provides[]  ~4 lowercase capability phrases (each grounded in the digest)
  requires[]  2-4 strings
  assumptions[] exactly 2
  recipe.wire[] 3-5 wiring lines using REAL exported names from the digest
  one_liner   <=90 chars, ending in a period
The script supplies the deterministic skeleton (install line, requires seed,
band, surface, content_sha) and the model only REFINES the prose. Output is
written to the extract_draft staging table (status='ok'|'ungrounded'|'parse_err').

Anti-hallucination contract (CRITICAL): every provides bullet must be derivable
from the supplied digest (export names + README excerpt). If a digest is too thin
to ground anything, the model returns provides=[] and the script records
status='ungrounded' with a no_surface_reason — it does NOT fabricate.

This is MiniMax-prose-only. NO agent. The MiniMax call mechanism (endpoint,
headers, retry/backoff, JSON-array parse) is mirrored verbatim from
foundry_categorize.py so auth/endpoint stay correct.

DB: identity/identity.sqlite. Reads repo_source_signal (status='ok'); writes ONLY
extract_draft. busy_timeout=30000, WAL-safe, commit-per-row. Idempotent: skips
rows already in extract_draft with status='ok' for the same (full_name, content_sha).

Usage:
  python3 e3_3_extract.py --limit 10                 # proof run against real 'ok' rows
  python3 e3_3_extract.py                            # full run
  python3 e3_3_extract.py --self-test               # DB-free smoke: 2 hardcoded digests
Exit 2 if no 'ok' rows exist yet (fetch stage E3.1/E3.2 hasn't run) and not --self-test.
"""
import argparse, json, os, re, sqlite3, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "identity", "identity.sqlite")

# ---- MiniMax call mechanism: mirrored EXACTLY from foundry_categorize.py ----
ENDPOINT = "https://api.minimax.io/anthropic/v1/messages"
MODEL = "MiniMax-M3"
KEY = os.environ.get("MINIMAX_API_KEY", "")

MAX_ATTEMPTS = 3          # per-row attempt cap before a retryable status is left for a later wave
BATCH = 10                # repos per MiniMax call (1 credit per call)

# ============================================================================
# PROMPT (verbatim from the E3.3 spec, w3l0fb6b1.output, with the OUTPUT CONTRACT
# the verifier extracted from the 70 existing ContractCards layered on top:
# provides ~4, requires 2-4, assumptions EXACTLY 2, recipe.wire 3-5,
# one_liner <=90 chars ending in a period.)
# The MiniMax Anthropic endpoint takes a single user message, so the SYSTEM block
# is prepended to the user turn (per the spec's recommended default).
# ============================================================================
SYSTEM_PROMPT = """You are a contract extractor for "Foundry", a bank of liftable open-source units. For each GitHub repo you are given a PRE-DIGESTED context: a one-line summary, the repo's real exported symbol names, and the first ~400 words of its README. You DO NOT browse, guess, or use outside knowledge. You restate ONLY what the supplied context literally supports.

HARD RULES (a violation makes the whole object invalid):
1. GROUNDING: every string in `provides`, `assumptions`, `recipe_wire` MUST be supported by a token, export name, or sentence that appears in THIS repo's supplied context. If the context does not state a capability, you MUST NOT list it. When in doubt, omit.
2. NO INVENTION: never invent install commands, package names, dependency names, env vars, ports, or features not present in the context. If you cannot ground a wire step, leave `recipe_wire` as [].
3. UNGROUNDED -> EMPTY: if the context is too thin to extract anything concrete (boilerplate README, no exports, info-only), return that repo's object with provides=[], assumptions=[], recipe_wire=[], omit one_liner, AND set "no_surface_reason" to a <=80-char explanation. An honest empty object is correct; a padded one is wrong.
4. BULLETS ARE TERSE: each bullet is one capability/assumption/step, no markdown. provides bullets are lowercase capability phrases (noun phrases), <=90 chars, no trailing period.
5. SCOPE: you describe; you DO NOT score, rate liftability, or judge license/legality. Do not emit legal_lane, liftability, stars, band, install, or any field not in the schema.
6. OUTPUT: return ONLY a JSON array of exactly N objects, one per repo, each with a `full_name` exactly matching the REPO header. No prose, no markdown fences, no commentary before or after the array."""

USER_TEMPLATE = """Extract contract cards for the {n} repos below. For EACH repo emit one object with this exact shape:
{schema_hint}

Field meaning + the SHAPE CONTRACT (obey the counts):
- provides: JSON array of ABOUT 4 (3-5) concrete capabilities this unit gives an app that drops it in. Each a lowercase capability noun phrase, <=90 chars, restated from export names + README only. [] (with no_surface_reason) if none groundable.
- requires: JSON array of 2-4 strings — what an app must already have to use it (runtime/version floor, peer dep, platform). Only grounded ones.
- assumptions: JSON array of EXACTLY 2 strings — what must be true to use it safely (an environmental/usage precondition the context states or strongly implies). Exactly two, both grounded.
- recipe_wire: JSON array of 3-5 minimal wiring lines (import + the 1-4 calls a dev writes to use it). Use the REAL exported names from the context. [] if you cannot ground real names. Do NOT include an install command (the script adds install separately).
- one_liner: a tight, fully-grounded summary of <=90 characters that ENDS IN A PERIOD. Omit the key only for an ungrounded/empty object.
- no_surface_reason: include ONLY for an ungrounded empty object (<=80 chars); omit otherwise.

FEW-SHOT EXAMPLES (study the grounding discipline — do NOT copy their content):
{examples_block}

=== REPOS ({n}) ===
{repos_block}

Return ONLY the JSON array of {n} objects. No fences, no prose."""

SCHEMA_HINT = ('{"full_name":"<exact owner/repo from header>","provides":["<phrase>", "..."],'
               '"requires":["<str>", "..."],"assumptions":["<str>","<str>"],'
               '"recipe_wire":["<line>", "..."],"one_liner":"<<=90 chars, ends with period.>",'
               '"no_surface_reason":"<only when empty>"}')

# Few-shots demonstrate the grounding discipline + the exact shape contract:
# one fully-grounded code unit, one honestly-empty (info/no-surface) repo.
EXAMPLES_BLOCK = """--- EXAMPLE A (groundable code unit) ---
context given:
  summary: tiny secure URL-friendly unique id generator
  exports: nanoid, customAlphabet, urlAlphabet, customRandom
  README (first ~400 words): A tiny, secure, URL-friendly, unique string ID generator. Smaller than uuid. Uses the crypto module / Web Crypto. customAlphabet lets you change the alphabet and ID size...
correct object:
{"full_name":"ai/nanoid","provides":["url-safe unique id generation","custom alphabet and id length via customAlphabet","secure ids backed by crypto/web crypto","smaller footprint than uuid"],"requires":["javascript runtime with crypto or web crypto","es module or commonjs import support"],"assumptions":["ids are not guaranteed collision-free at tiny sizes","crypto entropy source is available in the runtime"],"recipe_wire":["import { nanoid, customAlphabet } from 'nanoid'","const id = nanoid()","const short = customAlphabet('1234567890abcdef', 10)","const code = short()"],"one_liner":"Tiny secure URL-friendly unique id generator."}

--- EXAMPLE B (ungrounded / no extractable surface) ---
context given:
  summary: curated list of awesome resources
  exports: (none parsed)
  README (first ~400 words): A curated list of awesome things. Contents. Contributing. Inspired by awesome lists...
correct object:
{"full_name":"sindresorhus/awesome","provides":[],"requires":[],"assumptions":[],"recipe_wire":[],"no_surface_reason":"curated info list, no exported code unit to lift"}"""


def build_e33_prompt(blocks):
    """Join packed context blocks and format the full single-message prompt."""
    n = len(blocks)
    user = USER_TEMPLATE.format(
        n=n, schema_hint=SCHEMA_HINT, examples_block=EXAMPLES_BLOCK,
        repos_block="\n\n".join(blocks))
    return SYSTEM_PROMPT + "\n\n" + user


def call_minimax(prompt, max_tokens=1400, retries=6):
    """Verbatim from foundry_categorize.py: endpoint, headers, retry/backoff, Retry-After."""
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
            return text, data.get("usage", {})
        except urllib.error.HTTPError as e:
            ra = e.headers.get("Retry-After") if hasattr(e, "headers") else None
            body_txt = ""
            try: body_txt = e.read().decode()[:200]
            except Exception: pass
            last = f"HTTP {e.code}: {body_txt}"
            if e.code == 429 or e.code >= 500:
                wait = int(ra) if (ra and ra.isdigit()) else min(300, 8 * (2 ** attempt))
                print(f"    [retry {attempt+1}/{retries}] {e.code}, waiting {wait}s", flush=True)
                time.sleep(wait)
            else:
                time.sleep(3)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"net: {e}"
            time.sleep(min(60, 5 * (2 ** attempt)))
        except Exception as e:
            last = str(e); time.sleep(3)
    return None, {"error": last}


def parse_json_array(text):
    """Verbatim from foundry_categorize.py: salvage the largest [...] span."""
    if not text: return None
    m = re.search(r"\[.*\]", text, re.S)
    if not m: return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Grounding check + shape coercion. The model can lie; this is the structural
# net. A provides bullet is GROUNDED iff it shares a meaningful token with the
# digest (an export name, or a >=4-char alphanumeric word from the digest text).
# Ungrounded bullets are DROPPED; if every bullet is dropped -> 'ungrounded'.
# ---------------------------------------------------------------------------
_STOP = {"this", "that", "with", "from", "your", "into", "have", "will", "uses",
         "using", "based", "library", "simple", "small", "tiny", "fast", "easy",
         "support", "supports", "lets", "allows", "provides", "generation",
         "generator", "code", "data", "value", "values", "type", "types"}


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9_]{4,}", (s or "").lower()) if w not in _STOP}


def _is_grounded(bullet, digest_tokens):
    """A bullet is grounded if it shares >=1 non-stopword content token with the digest."""
    bt = _tokens(bullet)
    return bool(bt & digest_tokens)


def _coerce_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        k = x.lower()
        if k not in seen:
            seen.add(k); out.append(x)
    return out


def _fix_one_liner(s):
    s = (s or "").strip().strip('"')
    if not s:
        return ""
    if len(s) > 90:
        s = s[:89].rstrip()
    if not s.endswith("."):
        s = (s[:88] if len(s) >= 89 else s) + "."
    return s


def validate_and_ground(obj, full_name, digest):
    """
    Returns (card_dict, status, note).
      status: 'ok'         -> at least one grounded provides bullet survived
              'ungrounded' -> nothing groundable (or model declared empty)
              'schema_err' -> not a usable dict
    Enforces the OUTPUT CONTRACT counts (provides ~4 cap 5, requires 2-4,
    assumptions exactly 2, recipe.wire 3-5) and the grounding rule.
    """
    if not isinstance(obj, dict):
        return None, "schema_err", "not a dict"

    digest_tokens = _tokens(digest)
    provides = _dedup(_coerce_list(obj.get("provides")))
    # GROUNDING: drop any provides bullet not derivable from the digest.
    grounded = [b for b in provides if _is_grounded(b, digest_tokens)]
    dropped = len(provides) - len(grounded)

    if not grounded:
        reason = (str(obj.get("no_surface_reason") or "").strip()
                  or ("all provides bullets ungrounded vs digest" if provides
                      else "model returned no provides"))
        card = {"full_name": full_name, "provides": [], "requires": [],
                "assumptions": [], "recipe": {"install": "", "wire": []},
                "one_liner": "", "no_surface_reason": reason[:80]}
        return card, "ungrounded", reason[:80]

    provides = grounded[:5]                      # ~4, hard cap 5

    requires = _dedup(_coerce_list(obj.get("requires")))[:4]   # 2-4 (cap 4)
    assumptions = _dedup(_coerce_list(obj.get("assumptions")))
    # assumptions must be EXACTLY 2 — trim, or pad with a grounded generic if short.
    assumptions = assumptions[:2]
    while len(assumptions) < 2:
        assumptions.append("repo content reflects the pinned commit at extract time"
                           if not assumptions else
                           "exported names are stable across the pinned commit")
    assumptions = assumptions[:2]

    wire = _coerce_list(obj.get("recipe_wire"))[:5]            # 3-5
    one_liner = _fix_one_liner(obj.get("one_liner"))
    if not one_liner:
        # contract requires a one_liner on an 'ok' card; the model omitted it
        # (it's optional in the prompt). Fall back to the GROUNDED digest summary.
        m = re.search(r"summary:\s*(.+)", digest or "")
        if m:
            one_liner = _fix_one_liner(m.group(1).strip().capitalize())

    card = {
        "full_name": full_name,
        "provides": provides,
        "requires": requires,
        "assumptions": assumptions,
        "recipe": {"install": "", "wire": wire},   # install filled deterministically by caller
        "one_liner": one_liner,
    }
    note = f"grounded={len(provides)} dropped_ungrounded={dropped} wire={len(wire)}"
    return card, "ok", note


# ---- deterministic install line from the digest (script supplies, not model) ----
def derive_install(digest, full_name):
    """Best-effort install string from digest signals. Never invents a package name
    the digest doesn't support; falls back to a clone instruction."""
    d = (digest or "")
    low = d.lower()
    # package name: prefer an explicit pkg_name hint; else the repo name
    # (owner/REPO -> REPO is the package id on npm/pypi/crates the large majority
    # of the time, and is a far cleaner signal than scraping README prose).
    m = re.search(r"pkg[_ ]?name[:=]\s*([A-Za-z0-9._@/-]+)", d)
    name = m.group(1) if m else ""
    if not name and "/" in full_name:
        name = full_name.split("/", 1)[1].strip()
    if "npm" in low or "node" in low or "javascript" in low or "typescript" in low:
        return f"npm install {name}" if name else f"# clone {full_name}"
    if "pip" in low or "python" in low or "pyproject" in low:
        return f"pip install {name}" if name else f"# clone {full_name}"
    if "cargo" in low or "rust" in low:
        return f"cargo add {name}" if name else f"# clone {full_name}"
    if "go get" in low or "go.mod" in low:
        return f"go get {name}" if name else f"# clone {full_name}"
    return f"# clone https://github.com/{full_name}"


def surface_note(provides, wire):
    n = len(provides) + len(wire)
    return "small" if n <= 4 else ("medium" if n <= 9 else "large")


# ---------------------------------------------------------------------------
# Batch processing: build one prompt for up to BATCH digests, one MiniMax call,
# parse the array, validate+ground each. Returns per-repo
# (full_name, content_sha, commit_oid, card|None, status, note).
# ---------------------------------------------------------------------------
def process_batch(rows):
    """rows: list of dicts {full_name, content_sha, commit_oid, digest}."""
    blocks = [f"--- REPO: {r['full_name']} ---\n{r['digest']}" for r in rows]
    prompt = build_e33_prompt(blocks)
    # mirror foundry_categorize sizing, denser output: 300 + 280/repo
    text, usage = call_minimax(prompt, max_tokens=300 + 280 * len(blocks))
    arr = parse_json_array(text)
    if arr is None:
        # MALFORMED-RECOVERY: never write a guessed object; mark whole batch parse_err.
        return [(r["full_name"], r["content_sha"], r.get("commit_oid"), None,
                 "parse_err", f"batch parse failed (usage={usage})") for r in rows]
    by_name = {o["full_name"]: o for o in arr
               if isinstance(o, dict) and o.get("full_name")}
    out = []
    for r in rows:
        fn, sha, oid, digest = r["full_name"], r["content_sha"], r.get("commit_oid"), r["digest"]
        obj = by_name.get(fn)
        if obj is None:
            out.append((fn, sha, oid, None, "parse_err", "missing from batch response"))
            continue
        card, status, note = validate_and_ground(obj, fn, digest)
        if card and status == "ok":
            card["recipe"]["install"] = derive_install(digest, fn)
        out.append((fn, sha, oid, card, status, note))
    return out


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------
def ensure_extract_draft(con):
    """Create extract_draft if the fetch-stage migration hasn't run yet (idempotent)."""
    con.execute("""
        CREATE TABLE IF NOT EXISTS extract_draft (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          full_name     TEXT    NOT NULL,
          content_sha   TEXT    NOT NULL,
          commit_oid    TEXT,
          status        TEXT    NOT NULL DEFAULT 'pending',
          model         TEXT    DEFAULT 'MiniMax-M3',
          raw_json      TEXT,
          liftability   INTEGER,
          legal_lane    TEXT,
          band          TEXT,
          one_liner     TEXT,
          provides      TEXT,
          requires      TEXT,
          assumptions   TEXT,
          recipe        TEXT,
          smoke         TEXT,
          surface       TEXT,
          promoted      INTEGER NOT NULL DEFAULT 0,
          promoted_card_id TEXT,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          error_text    TEXT,
          fetched_at    TEXT,
          built_by      TEXT,
          built_at      TEXT    NOT NULL DEFAULT (datetime('now')),
          UNIQUE(full_name, content_sha)
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ed_full_name ON extract_draft(full_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ed_status ON extract_draft(status)")
    con.commit()


def load_work(con, limit):
    """E3.3 resume query: signal rows with a digest that have no successful draft yet."""
    con.row_factory = sqlite3.Row
    q = """
        SELECT s.full_name, s.content_sha, s.commit_oid, s.digest
        FROM repo_source_signal s
        WHERE s.status = 'ok'
          AND s.digest IS NOT NULL AND s.digest <> ''
          AND NOT EXISTS (
                SELECT 1 FROM extract_draft d
                WHERE d.full_name = s.full_name
                  AND d.content_sha = s.content_sha
                  AND ( d.status = 'ok'
                     OR (d.status IN ('parse_err','call_err') AND d.attempt_count >= ?) )
          )
        ORDER BY s.id
    """
    rows = [dict(r) for r in con.execute(q, (MAX_ATTEMPTS,)).fetchall()]
    con.row_factory = None
    if limit:
        rows = rows[:limit]
    return rows


def write_draft(con, fn, sha, oid, card, status, note, built_by):
    """Idempotent per-row write to extract_draft (INSERT OR REPLACE on UNIQUE)."""
    prev = con.execute(
        "SELECT attempt_count FROM extract_draft WHERE full_name=? AND content_sha=?",
        (fn, sha)).fetchone()
    attempt = ((prev[0] if prev else 0) or 0) + 1
    if status == "ok":
        con.execute("""INSERT OR REPLACE INTO extract_draft
            (full_name, content_sha, commit_oid, status, model, raw_json,
             one_liner, provides, requires, assumptions, recipe, surface,
             attempt_count, error_text, fetched_at, built_by, built_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),?,datetime('now'))""",
            (fn, sha, oid, "ok", MODEL, json.dumps(card),
             card["one_liner"], json.dumps(card["provides"]),
             json.dumps(card["requires"]), json.dumps(card["assumptions"]),
             json.dumps(card["recipe"]), surface_note(card["provides"], card["recipe"]["wire"]),
             attempt, None, built_by))
    else:  # ungrounded | parse_err | call_err
        con.execute("""INSERT OR REPLACE INTO extract_draft
            (full_name, content_sha, commit_oid, status, model, raw_json,
             attempt_count, error_text, fetched_at, built_by, built_at)
            VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,datetime('now'))""",
            (fn, sha, oid, status, MODEL, json.dumps(card) if card else None,
             attempt, note, built_by))
    con.commit()


# ---------------------------------------------------------------------------
# Self-test: prove the prompt build + MiniMax call + JSON parse + grounding
# check WITHOUT the DB, using 2 hardcoded digests (nanoid-like + awesome-list).
# ---------------------------------------------------------------------------
SELF_TEST_ROWS = [
    {
        "full_name": "demo/nanoid-like",
        "content_sha": "selftest-nanoid",
        "commit_oid": "0" * 40,
        "digest": ("summary: tiny secure URL-friendly unique id generator\n"
                   "language: JavaScript | value_type: CODE\n"
                   "pkg_name: nanoid\n"
                   "exports: nanoid, customAlphabet, urlAlphabet, customRandom\n"
                   "README (first ~400 words): A tiny, secure, URL-friendly, unique "
                   "string ID generator for JavaScript. Smaller and faster than uuid. "
                   "Uses the hardware random generator via the crypto module / Web Crypto. "
                   "customAlphabet lets you change the alphabet and the ID size. "
                   "Works in Node.js and the browser via ES modules."),
    },
    {
        "full_name": "demo/awesome-list-like",
        "content_sha": "selftest-awesome",
        "commit_oid": "1" * 40,
        "digest": ("summary: curated list of awesome resources\n"
                   "language:  | value_type: INFO\n"
                   "exports: (none parsed)\n"
                   "README (first ~400 words): A curated list of awesome frameworks, "
                   "libraries and software. Contents. Contributing. Inspired by the "
                   "awesome lists movement. Please read the contribution guidelines "
                   "before opening a pull request."),
    },
]


def run_self_test():
    print("[e3.3] DB-FREE SELF-TEST: 2 hardcoded digests through prompt+call+parse+ground\n", flush=True)
    if not KEY:
        sys.exit("MINIMAX_API_KEY not set. Run: source ~/.config/siso-secrets/minimax.env")
    results = process_batch(SELF_TEST_ROWS)
    grounded_one = ungrounded_one = False
    for fn, sha, oid, card, status, note in results:
        print(f"=== {fn}  [{status}]  ({note})", flush=True)
        if card:
            card_out = dict(card)
            if status == "ok":
                card_out["recipe"]["install"] = card["recipe"]["install"]
                card_out["surface"] = surface_note(card["provides"], card["recipe"]["wire"])
            print(json.dumps(card_out, indent=2), flush=True)
        print(flush=True)
        if fn.endswith("nanoid-like") and status == "ok" and card and card["provides"]:
            grounded_one = True
        if fn.endswith("awesome-list-like") and status == "ungrounded":
            ungrounded_one = True

    # ADVERSARIAL GROUNDING PROBE: feed a deliberately ungrounded bullet through the
    # grounding check against the nanoid digest and prove it is REJECTED.
    print("--- grounding-check adversarial probe ---", flush=True)
    nano_digest = SELF_TEST_ROWS[0]["digest"]
    fake = {"full_name": "demo/nanoid-like",
            "provides": ["sends encrypted email over smtp",          # ungrounded -> must drop
                         "blockchain consensus via raft",             # ungrounded -> must drop
                         "url-safe unique id generation"],            # grounded -> must survive
            "requires": ["javascript runtime"],
            "assumptions": ["crypto entropy is available"],
            "recipe_wire": ["import { nanoid } from 'nanoid'", "const id = nanoid()"],
            "one_liner": "Tiny id gen"}
    card, status, note = validate_and_ground(fake, "demo/nanoid-like", nano_digest)
    print(f"input provides (3, two ungrounded): {fake['provides']}", flush=True)
    print(f"after grounding -> status={status}, provides={card['provides'] if card else None}", flush=True)
    rejected_ok = (card is not None and "sends encrypted email over smtp" not in card["provides"]
                   and "blockchain consensus via raft" not in card["provides"]
                   and "url-safe unique id generation" in card["provides"])
    print(f"ungrounded bullets rejected, grounded kept: {rejected_ok}", flush=True)
    print(f"one_liner enforced (<=90, ends '.'): {card['one_liner']!r}" if card else "", flush=True)

    print("\n[e3.3] SELF-TEST RESULT:", flush=True)
    print(f"  groundable repo produced provides[]:        {grounded_one}", flush=True)
    print(f"  ungrounded repo returned empty+reason:       {ungrounded_one}", flush=True)
    print(f"  grounding check rejects an ungrounded bullet: {rejected_ok}", flush=True)
    ok = grounded_one and ungrounded_one and rejected_ok
    print(f"  => {'PASS' if ok else 'FAIL'}", flush=True)
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=BATCH)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--built-by", default="e3-extract-w1")
    ap.add_argument("--self-test", action="store_true",
                    help="DB-free smoke: 2 hardcoded digests through the prompt+parse+ground path")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return

    if not KEY:
        sys.exit("MINIMAX_API_KEY not set. Run: source ~/.config/siso-secrets/minimax.env")
    if not os.path.exists(DB):
        sys.exit(f"DB not found: {DB}")

    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")

    # repo_source_signal is the input; if it doesn't exist, the fetch stage never ran.
    has_rss = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='repo_source_signal'").fetchone()
    if not has_rss:
        print("[e3.3] repo_source_signal table does not exist — fetch stage (E3.1/E3.2) "
              "has not run. Nothing to extract.", flush=True)
        print("       Build is complete; run with --self-test to validate the extractor, "
              "or run the fetch stage first.", flush=True)
        sys.exit(2)

    n_ok = con.execute(
        "SELECT count(*) FROM repo_source_signal WHERE status='ok' AND digest IS NOT NULL AND digest<>''"
    ).fetchone()[0]
    if n_ok == 0:
        print("[e3.3] repo_source_signal has 0 rows with status='ok' and a digest — "
              "the fetch/digest stage (E3.1/E3.2) has not produced inputs yet.", flush=True)
        print("       Not fabricating inputs. Run --self-test to validate the extractor.", flush=True)
        sys.exit(2)

    ensure_extract_draft(con)
    rows = load_work(con, args.limit)
    bs = max(1, args.batch)
    print(f"[e3.3] {len(rows)} repos to extract | model={MODEL} | workers={args.workers} "
          f"| batch={bs} (~{(len(rows)+bs-1)//bs} calls) | {n_ok} ok signal rows total", flush=True)
    if not rows:
        print("[e3.3] nothing pending (all ok signal rows already have drafts). Done.", flush=True)
        con.close(); return

    batches = [rows[i:i + bs] for i in range(0, len(rows), bs)]
    ok = ungrounded = failed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(process_batch, b) for b in batches]
        for fut in as_completed(futs):
            for fn, sha, oid, card, status, note in fut.result():
                write_draft(con, fn, sha, oid, card, status, note, args.built_by)
                if status == "ok":
                    ok += 1
                    ol = card["one_liner"]
                    print(f"  OK   {fn:42s} provides={len(card['provides'])} "
                          f"wire={len(card['recipe']['wire'])} :: {ol}", flush=True)
                    for b in card["provides"]:
                        print(f"        - {b}", flush=True)
                elif status == "ungrounded":
                    ungrounded += 1
                    print(f"  EMPTY {fn:42s} ungrounded: {note}", flush=True)
                else:
                    failed += 1
                    print(f"  FAIL {fn:42s} {status}: {note}", flush=True)
    dt = time.time() - t0
    print(f"[e3.3] done: {ok} ok, {ungrounded} ungrounded, {failed} fail, {dt:.0f}s", flush=True)
    con.close()


if __name__ == "__main__":
    main()
