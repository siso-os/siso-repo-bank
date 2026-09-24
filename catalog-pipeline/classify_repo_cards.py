#!/usr/bin/env python3
"""Cheap metadata-only classification for Foundry RepoCards."""

import argparse
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "identity" / "identity.sqlite"
STAGING = BASE / "staging"
SUMMARY_JSON = STAGING / "repo-card-classification-seed.json"
ASSIGNMENTS_JSONL = STAGING / "repo-card-classification-seed.assignments.jsonl"
REPORT_MD = STAGING / "repo-card-classification-seed.md"

FAMILIES = {
    "app_builder_ai": {
        "label": "AI app builders and codegen shells",
        "terms": {
            "lovable": 10,
            "bolt": 7,
            "v0": 7,
            "webcontainer": 6,
            "sandbox": 4,
            "codegen": 5,
            "code generation": 6,
            "ai coding": 7,
            "app builder": 9,
            "website builder": 6,
            "vibe": 8,
            "agentic": 5,
            "ai agent": 4,
            "swe": 3,
            "replit": 3,
        },
    },
    "saas_starter": {
        "label": "SaaS starters, templates, and boilerplates",
        "terms": {
            "saas": 9,
            "starter": 5,
            "boilerplate": 6,
            "template": 4,
            "multitenant": 7,
            "multi tenant": 7,
            "tenant": 3,
            "nextjs starter": 7,
            "t3": 4,
            "dashboard starter": 5,
        },
    },
    "ui_component_library": {
        "label": "UI kits and component libraries",
        "terms": {
            "ui": 3,
            "component": 5,
            "components": 5,
            "design system": 8,
            "shadcn": 8,
            "radix": 6,
            "tailwind": 5,
            "material": 4,
            "chakra": 5,
            "storybook": 4,
            "headless": 4,
        },
    },
    "admin_dashboard": {
        "label": "Admin dashboards and internal tools",
        "terms": {
            "admin": 6,
            "dashboard": 6,
            "internal tool": 8,
            "backoffice": 7,
            "analytics dashboard": 7,
            "data grid": 5,
            "datatable": 5,
        },
    },
    "auth_identity": {
        "label": "Auth, identity, teams, and permissions",
        "terms": {
            "auth": 7,
            "authentication": 8,
            "authorization": 7,
            "oauth": 7,
            "oidc": 7,
            "sso": 6,
            "nextauth": 8,
            "clerk": 6,
            "passport": 5,
            "rbac": 7,
            "permission": 5,
            "identity": 5,
            "team": 3,
            "invite": 4,
        },
    },
    "billing_payments": {
        "label": "Billing, subscriptions, and payments",
        "terms": {
            "stripe": 9,
            "billing": 8,
            "subscription": 8,
            "payment": 7,
            "payments": 7,
            "checkout": 5,
            "invoice": 5,
            "paddle": 5,
            "lemonsqueezy": 5,
        },
    },
    "crm_sales": {
        "label": "CRM, sales, and customer operations",
        "terms": {
            "crm": 10,
            "sales": 5,
            "pipeline": 5,
            "lead": 4,
            "customer": 4,
            "contact": 3,
            "deal": 4,
            "hubspot": 4,
        },
    },
    "booking_scheduling": {
        "label": "Booking, scheduling, and calendars",
        "terms": {
            "booking": 9,
            "schedule": 7,
            "scheduling": 8,
            "appointment": 8,
            "calendar": 6,
            "calendly": 6,
            "reservation": 6,
            "availability": 4,
        },
    },
    "commerce_marketplace": {
        "label": "Commerce, marketplaces, and stores",
        "terms": {
            "ecommerce": 9,
            "e-commerce": 9,
            "commerce": 7,
            "shop": 5,
            "storefront": 7,
            "marketplace": 8,
            "cart": 5,
            "checkout": 4,
            "pos": 4,
        },
    },
    "workflow_automation": {
        "label": "Workflow automation and low-code engines",
        "terms": {
            "workflow": 8,
            "automation": 7,
            "low code": 8,
            "low-code": 8,
            "nocode": 8,
            "no code": 8,
            "n8n": 8,
            "temporal": 6,
            "zapier": 5,
            "orchestration": 5,
            "rule engine": 5,
        },
    },
    "data_analytics": {
        "label": "Analytics, BI, reporting, and charts",
        "terms": {
            "analytics": 8,
            "reporting": 7,
            "business intelligence": 8,
            "bi": 5,
            "chart": 5,
            "charts": 5,
            "visualization": 6,
            "metrics": 5,
            "dashboard": 3,
        },
    },
    "backend_api": {
        "label": "Backend frameworks, APIs, and services",
        "terms": {
            "api": 5,
            "backend": 7,
            "serverless": 6,
            "graphql": 6,
            "trpc": 6,
            "rest": 4,
            "express": 5,
            "fastapi": 6,
            "django": 5,
            "rails": 5,
            "nestjs": 6,
            "hono": 5,
        },
    },
    "database_orm": {
        "label": "Databases, ORMs, and persistence",
        "terms": {
            "database": 7,
            "postgres": 6,
            "postgresql": 6,
            "mysql": 5,
            "sqlite": 5,
            "orm": 7,
            "prisma": 7,
            "drizzle": 6,
            "supabase": 7,
            "redis": 4,
            "migration": 4,
        },
    },
    "testing_quality": {
        "label": "Testing, QA, and verification",
        "terms": {
            "testing": 8,
            "test": 5,
            "e2e": 7,
            "playwright": 8,
            "cypress": 7,
            "jest": 5,
            "vitest": 6,
            "storybook": 3,
            "qa": 5,
            "verification": 5,
        },
    },
    "deployment_infra": {
        "label": "Deployment, CI, and infrastructure",
        "terms": {
            "deploy": 7,
            "deployment": 7,
            "docker": 6,
            "kubernetes": 6,
            "k8s": 6,
            "terraform": 6,
            "ci": 4,
            "cd": 4,
            "github actions": 5,
            "vercel": 5,
            "cloudflare": 5,
        },
    },
    "cms_content": {
        "label": "CMS, content, docs apps, and editors",
        "terms": {
            "cms": 9,
            "content": 5,
            "blog": 5,
            "markdown": 5,
            "editor": 5,
            "wysiwyg": 6,
            "notion": 4,
            "docs": 4,
            "knowledge base": 6,
        },
    },
    "mobile_app": {
        "label": "Mobile apps and cross-platform shells",
        "terms": {
            "mobile": 7,
            "ios": 5,
            "android": 5,
            "react native": 8,
            "react-native": 8,
            "flutter": 8,
            "expo": 7,
            "capacitor": 5,
            "ionic": 5,
        },
    },
    "ai_ml_app": {
        "label": "AI, LLM, RAG, and chatbot apps",
        "terms": {
            "ai": 4,
            "llm": 8,
            "rag": 8,
            "chatbot": 8,
            "openai": 6,
            "anthropic": 5,
            "langchain": 7,
            "llama": 5,
            "vector": 4,
            "embedding": 5,
        },
    },
    "security_compliance": {
        "label": "Security, compliance, and audit",
        "terms": {
            "security": 8,
            "compliance": 7,
            "audit": 6,
            "vulnerability": 6,
            "owasp": 6,
            "secret": 4,
            "permissions": 4,
            "rbac": 4,
        },
    },
    "docs_awesome_learning": {
        "label": "Awesome lists, docs, tutorials, and examples",
        "terms": {
            "awesome": 10,
            "list": 4,
            "curated": 5,
            "tutorial": 6,
            "tutorials": 6,
            "examples": 6,
            "sample": 5,
            "learn": 4,
            "roadmap": 5,
            "guide": 4,
        },
    },
}

PERMISSIVE_LICENSES = {
    "0bsd",
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "bsd-4-clause",
    "cc0-1.0",
    "isc",
    "mit",
    "unlicense",
    "zlib",
}

REFERENCE_LICENSE_PREFIXES = ("agpl", "gpl", "lgpl", "epl", "mpl", "cddl", "sspl")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_topics(raw):
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def normalize_text(*parts):
    joined = " ".join(str(part or "").lower() for part in parts)
    compact = re.sub(r"[^a-z0-9+#.]+", " ", joined)
    return f"{joined} {compact}"


def has_term(haystack, term):
    needle = term.lower()
    if " " in needle or "-" in needle or "." in needle or "+" in needle or "#" in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def license_lane(license_id):
    value = str(license_id or "").strip().lower()
    if not value or value == "noassertion":
        return "blocked_unknown"
    if value in PERMISSIVE_LICENSES:
        return "shippable"
    if any(value.startswith(prefix) for prefix in REFERENCE_LICENSE_PREFIXES):
        return "reference_only"
    return "manual_review"


def recency_score(pushed_at):
    if not pushed_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    age_days = max((datetime.now(timezone.utc) - dt).days, 0)
    if age_days <= 90:
        return 8.0
    if age_days <= 365:
        return 5.0
    if age_days <= 1095:
        return 2.0
    return -4.0


def schema_score(level):
    return {
        "rich_created": 8.0,
        "rich": 5.0,
        "basic": 1.5,
        "seed": 0.0,
    }.get(level or "", 0.0)


def classify(row):
    topics = parse_topics(row["topics_json"])
    haystack = normalize_text(row["full_name"], row["description"], row["language"], " ".join(topics))
    family_scores = {}
    family_hits = {}
    for family, config in FAMILIES.items():
        score = 0
        hits = []
        for term, weight in config["terms"].items():
            if has_term(haystack, term):
                score += weight
                hits.append(term)
        if score:
            family_scores[family] = score
            family_hits[family] = hits[:8]

    ordered = sorted(family_scores.items(), key=lambda item: (-item[1], item[0]))
    assigned = [family for family, score in ordered if score >= 5]
    if not assigned and ordered:
        assigned = [ordered[0][0]]

    stars = row["stars"] or 0
    lane = license_lane(row["license"])
    priority = math.log10(max(stars, 1) + 1) * 18
    priority += schema_score(row["schema_level"])
    priority += recency_score(row["pushed_at"])
    priority += min(sum(family_scores.values()), 25)
    priority += min((row["field_score"] or 0) * 0.8, 8)

    if lane == "shippable":
        priority += 10
    elif lane == "manual_review":
        priority -= 2
    elif lane == "reference_only":
        priority -= 8
    else:
        priority -= 14

    if row["archived"]:
        priority -= 35
    if row["fork"]:
        priority -= 12
    if row["mirror"]:
        priority -= 15
    if "docs_awesome_learning" in assigned:
        priority -= 12

    return {
        "repo": row["full_name"] or row["normalized_url"],
        "url": row["url"] or row["normalized_url"],
        "stars": stars,
        "language": row["language"] or "",
        "license": row["license"] or "",
        "legal_lane": lane,
        "schema_level": row["schema_level"],
        "field_score": row["field_score"],
        "pushed_at": row["pushed_at"] or "",
        "archived": bool(row["archived"]) if row["archived"] is not None else False,
        "fork": bool(row["fork"]) if row["fork"] is not None else False,
        "mirror": bool(row["mirror"]) if row["mirror"] is not None else False,
        "description": row["description"] or "",
        "families": assigned,
        "family_scores": family_scores,
        "family_hits": family_hits,
        "sourcebank_priority": round(priority, 2),
    }


def iter_cards(conn):
    query = """
        SELECT
          full_name, normalized_url, url, stars, language, pushed_at, description,
          license, topics_json, archived, fork, mirror, schema_level, field_score
        FROM repo_card
        WHERE stars IS NOT NULL
        ORDER BY stars DESC, full_name ASC
    """
    yield from conn.execute(query)


def candidate_key(item):
    return (
        item["sourcebank_priority"],
        item["stars"],
        item["schema_level"] == "rich_created",
        item["repo"],
    )


def render_report(summary, top_by_family):
    lines = []
    lines.append("# RepoCard Classification Seed")
    lines.append("")
    lines.append(f"Generated: `{summary['generated_at']}`")
    lines.append(f"RepoCards scanned: `{summary['repo_cards_scanned']:,}`")
    lines.append(f"Repos with at least one family: `{summary['repos_with_family']:,}`")
    lines.append("")
    lines.append("This is a metadata-only seed: no source clones, no app assembly, no proof slice.")
    lines.append("")
    lines.append("## Family Counts")
    lines.append("")
    lines.append("| Family | Count | Label |")
    lines.append("| --- | ---: | --- |")
    for family, count in summary["family_counts"]:
        lines.append(f"| `{family}` | `{count:,}` | {FAMILIES[family]['label']} |")
    lines.append("")
    lines.append("## Legal Lanes")
    lines.append("")
    lines.append("| Lane | Count |")
    lines.append("| --- | ---: |")
    for lane, count in summary["legal_lanes"]:
        lines.append(f"| `{lane}` | `{count:,}` |")
    lines.append("")
    lines.append("## Top Candidates By Family")
    for family, items in top_by_family.items():
        lines.append("")
        lines.append(f"### {FAMILIES[family]['label']} (`{family}`)")
        if not items:
            lines.append("")
            lines.append("No candidates.")
            continue
        lines.append("")
        lines.append("| Priority | Stars | Lane | Repo | Language | Notes |")
        lines.append("| ---: | ---: | --- | --- | --- | --- |")
        for item in items:
            desc = item["description"].replace("|", " ").strip()
            if len(desc) > 110:
                desc = f"{desc[:107]}..."
            repo = item["repo"].replace("|", " ")
            url = item["url"]
            repo_link = f"[{repo}]({url})" if url else repo
            lines.append(
                f"| `{item['sourcebank_priority']}` | `{item['stars']:,}` | `{item['legal_lane']}` | "
                f"{repo_link} | `{item['language']}` | {desc} |"
            )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- Treat these as retrieval buckets, not final truth.")
    lines.append("- `docs_awesome_learning` is useful for reference discovery, but usually not direct SourceBank reuse.")
    lines.append("- High-priority shippable candidates should be the first repos to receive README/license/manifest enrichment and later contract distillation.")
    lines.append("- `reference_only` and `blocked_unknown` can still teach patterns, but should not feed copyable code lanes without review.")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--summary-json", type=Path, default=SUMMARY_JSON)
    parser.add_argument("--assignments-jsonl", type=Path, default=ASSIGNMENTS_JSONL)
    parser.add_argument("--report-md", type=Path, default=REPORT_MD)
    parser.add_argument("--limit-per-family", type=int, default=25)
    args = parser.parse_args()

    args.summary_json.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    total = 0
    with_family = 0
    family_counts = Counter()
    legal_lanes = Counter()
    languages = Counter()
    top_by_family = defaultdict(list)

    with args.assignments_jsonl.open("w", encoding="utf-8") as out:
        for row in iter_cards(conn):
            total += 1
            item = classify(row)
            legal_lanes[item["legal_lane"]] += 1
            if item["language"]:
                languages[item["language"]] += 1
            if item["families"]:
                with_family += 1
            for family in item["families"]:
                family_counts[family] += 1
                bucket = top_by_family[family]
                bucket.append(item)
                bucket.sort(key=candidate_key, reverse=True)
                del bucket[args.limit_per_family :]
            out.write(json.dumps(item, sort_keys=True) + "\n")

    ordered_top = {
        family: sorted(items, key=candidate_key, reverse=True)
        for family, items in sorted(top_by_family.items())
    }
    summary = {
        "generated_at": now_iso(),
        "repo_cards_scanned": total,
        "repos_with_family": with_family,
        "family_counts": family_counts.most_common(),
        "legal_lanes": legal_lanes.most_common(),
        "top_languages": languages.most_common(30),
        "artifacts": {
            "assignments_jsonl": str(args.assignments_jsonl),
            "report_md": str(args.report_md),
        },
        "top_by_family": ordered_top,
    }

    args.summary_json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report_md.write_text(render_report(summary, ordered_top), encoding="utf-8")

    print(json.dumps({
        "repo_cards_scanned": total,
        "repos_with_family": with_family,
        "families": len(ordered_top),
        "summary_json": str(args.summary_json),
        "assignments_jsonl": str(args.assignments_jsonl),
        "report_md": str(args.report_md),
    }, indent=2))


if __name__ == "__main__":
    main()
