#!/usr/bin/env python3
"""Pilot RepoCard enrichment without cloning source.

This fetches a small, ranked sample of RepoCards through the cheap ladder:
GraphQL structured fields, README text from raw GitHub, selected manifests,
and homepage reachability hints. It writes staging artifacts only; it does not
mutate raw catalog rounds or the canonical repo_card table.
"""

import argparse
import json
import math
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "identity" / "identity.sqlite"
STAGING = BASE / "staging"
README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md", "docs/README.md")
README_MAX_BYTES = 64 * 1024
HTTP_TIMEOUT = 12

MANIFESTS_BY_LANGUAGE = {
    "JavaScript": ("package.json", "pnpm-workspace.yaml", "yarn.lock"),
    "TypeScript": ("package.json", "pnpm-workspace.yaml", "yarn.lock"),
    "Python": ("pyproject.toml", "setup.cfg", "requirements.txt", "setup.py"),
    "Go": ("go.mod",),
    "Rust": ("Cargo.toml",),
    "Ruby": ("Gemfile",),
    "PHP": ("composer.json",),
    "Java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "Kotlin": ("build.gradle.kts", "build.gradle", "settings.gradle.kts"),
    "C#": ("*.csproj",),
}

PERMISSIVE_LICENSES = {
    "MIT",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "ISC",
    "0BSD",
    "UNLICENSE",
    "MPL-2.0",
}

REFERENCE_LICENSE_PREFIXES = ("GPL", "AGPL", "LGPL", "SSPL")
REFERENCE_LICENSES = {"CC-BY-SA-4.0", "EPL-2.0"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_gh_token():
    return subprocess.check_output(["gh", "auth", "token"], text=True).strip()


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def owner_repo(row):
    full_name = row["full_name"] or ""
    if "/" in full_name:
        owner, repo = full_name.split("/", 1)
        return owner, repo
    parsed = urlparse(row["normalized_url"] or "")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return "", ""


def select_candidates(limit):
    bands = [
        ("100k+", 100000, None),
        ("50k-100k", 50000, 99999),
        ("10k-50k", 10000, 49999),
        ("5k-10k", 5000, 9999),
        ("1k-5k", 1000, 4999),
        ("500-999", 500, 999),
        ("100-499", 100, 499),
    ]
    per_band = max(1, math.ceil(limit / len(bands)))
    selected = []
    seen = set()
    conn = connect()
    try:
        for band, low, high in bands:
            where = ["stars >= ?", "(archived IS NULL OR archived = 0)", "(fork IS NULL OR fork = 0)"]
            params = [low]
            if high is not None:
                where.append("stars <= ?")
                params.append(high)
            rows = conn.execute(
                f"""
                SELECT canonical_id, normalized_url, full_name, url, stars, language, license,
                       topics_json, default_branch, archived, fork, mirror, field_score
                FROM repo_card
                WHERE {' AND '.join(where)}
                ORDER BY field_score DESC, stars DESC, full_name
                LIMIT ?
                """,
                (*params, per_band),
            ).fetchall()
            for row in rows:
                key = row["canonical_id"]
                if key in seen:
                    continue
                seen.add(key)
                item = dict(row)
                item["band"] = band
                selected.append(item)
                if len(selected) >= limit:
                    return selected
    finally:
        conn.close()
    return selected


def graphql_string(value):
    return json.dumps(value)


def build_graphql_query(batch):
    parts = []
    for index, item in enumerate(batch):
        owner, repo = owner_repo(item)
        alias = f"r{index}"
        parts.append(
            f"""
            {alias}: repository(owner: {graphql_string(owner)}, name: {graphql_string(repo)}) {{
              nameWithOwner
              url
              stargazerCount
              forkCount
              pushedAt
              createdAt
              isArchived
              isFork
              isMirror
              diskUsage
              homepageUrl
              primaryLanguage {{ name }}
              licenseInfo {{ spdxId key name pseudoLicense }}
              defaultBranchRef {{ name target {{ oid }} }}
              repositoryTopics(first: 20) {{ nodes {{ topic {{ name }} }} }}
              latestRelease {{ tagName publishedAt }}
              readmeMd: object(expression: "HEAD:README.md") {{ ... on Blob {{ oid byteSize }} }}
            }}
            """
        )
    return "query {\n" + "\n".join(parts) + "\nrateLimit { limit cost remaining resetAt used }\n}"


def graphql_fetch(token, batch):
    body = json.dumps({"query": build_graphql_query(batch)}).encode("utf-8")
    req = Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "siso-foundry-enrichment-pilot",
        },
        method="POST",
    )
    with urlopen(req, timeout=HTTP_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError("; ".join(error.get("message", "GraphQL error") for error in payload["errors"]))
    return payload["data"]


def raw_url(owner, repo, branch, rel_path):
    return "https://raw.githubusercontent.com/{}/{}/{}/{}".format(
        quote(owner, safe=""),
        quote(repo, safe=""),
        quote(branch, safe=""),
        quote(rel_path, safe="/"),
    )


def fetch_text_url(url, max_bytes):
    req = Request(url, headers={"User-Agent": "siso-foundry-enrichment-pilot"})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            data = response.read(max_bytes + 1)
            return {
                "status": response.status,
                "text": data[:max_bytes].decode("utf-8", errors="replace"),
                "truncated": len(data) > max_bytes,
                "bytes": min(len(data), max_bytes),
                "url": url,
            }
    except HTTPError as err:
        return {"status": err.code, "text": "", "truncated": False, "bytes": 0, "url": url}
    except URLError as err:
        return {"status": "url_error", "error": str(err.reason), "text": "", "truncated": False, "bytes": 0, "url": url}


def fetch_readme(owner, repo, branch):
    for name in README_NAMES:
        result = fetch_text_url(raw_url(owner, repo, branch, name), README_MAX_BYTES)
        if result["status"] == 200:
            result["path"] = name
            return result
    return {"status": 404, "text": "", "truncated": False, "bytes": 0, "path": ""}


def manifest_paths(language):
    paths = list(MANIFESTS_BY_LANGUAGE.get(language or "", ()))
    if "package.json" not in paths:
        paths.append("package.json")
    return paths[:5]


def parse_package_json(text_value):
    try:
        data = json.loads(text_value)
    except json.JSONDecodeError:
        return {}
    deps = []
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        values = data.get(key)
        if isinstance(values, dict):
            deps.extend(values.keys())
    return {
        "name": data.get("name", ""),
        "license": data.get("license", ""),
        "scripts": sorted((data.get("scripts") or {}).keys()) if isinstance(data.get("scripts"), dict) else [],
        "dependencies": sorted(set(deps))[:80],
    }


def fetch_manifest(owner, repo, branch, language):
    for rel_path in manifest_paths(language):
        if "*" in rel_path:
            continue
        result = fetch_text_url(raw_url(owner, repo, branch, rel_path), 256 * 1024)
        if result["status"] != 200:
            continue
        hints = {}
        if rel_path == "package.json":
            hints = parse_package_json(result["text"])
        return {
            "path": rel_path,
            "status": 200,
            "bytes": result["bytes"],
            "truncated": result["truncated"],
            "hints": hints,
        }
    return {"path": "", "status": 404, "bytes": 0, "truncated": False, "hints": {}}


def legal_lane(spdx_id):
    value = (spdx_id or "").upper()
    if not value or value == "NOASSERTION":
        return "blocked_unknown"
    if value in PERMISSIVE_LICENSES:
        return "shippable"
    if value in REFERENCE_LICENSES or any(value.startswith(prefix) for prefix in REFERENCE_LICENSE_PREFIXES):
        return "reference_only"
    return "reference_only"


def repo_from_graphql(item, gql):
    license_info = gql.get("licenseInfo") or {}
    topics = [
        node.get("topic", {}).get("name")
        for node in (gql.get("repositoryTopics") or {}).get("nodes", [])
        if node.get("topic", {}).get("name")
    ]
    default_branch = (gql.get("defaultBranchRef") or {}).get("name") or item.get("default_branch") or "main"
    language = (gql.get("primaryLanguage") or {}).get("name") or item.get("language") or ""
    spdx_id = license_info.get("spdxId") or ""
    return {
        "canonical_id": item["canonical_id"],
        "band": item["band"],
        "full_name": gql.get("nameWithOwner") or item["full_name"],
        "url": gql.get("url") or item["url"],
        "stars": gql.get("stargazerCount"),
        "forks": gql.get("forkCount"),
        "pushed_at": gql.get("pushedAt"),
        "created_at": gql.get("createdAt"),
        "archived": gql.get("isArchived"),
        "fork": gql.get("isFork"),
        "mirror": gql.get("isMirror"),
        "disk_usage_kb": gql.get("diskUsage"),
        "homepage_url": gql.get("homepageUrl") or "",
        "language": language,
        "license": {
            "spdx_id": spdx_id,
            "key": license_info.get("key") or "",
            "name": license_info.get("name") or "",
            "pseudo": license_info.get("pseudoLicense"),
            "legal_lane": legal_lane(spdx_id),
        },
        "default_branch": default_branch,
        "topics": topics,
        "latest_release": gql.get("latestRelease") or {},
        "readme_pointer": gql.get("readmeMd") or {},
    }


def run_pilot(limit, batch_size, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    token = run_gh_token()
    candidates = select_candidates(limit)
    started_at = now_iso()
    out_jsonl = output_dir / "enriched-repos.jsonl"
    summary = {
        "started_at": started_at,
        "limit": limit,
        "batch_size": batch_size,
        "selected": len(candidates),
        "written": 0,
        "readme_found": 0,
        "manifest_found": 0,
        "legal_lane": {},
        "by_band": {},
        "errors": [],
    }
    with out_jsonl.open("w", encoding="utf-8") as out:
        for offset in range(0, len(candidates), batch_size):
            batch = candidates[offset : offset + batch_size]
            try:
                data = graphql_fetch(token, batch)
            except Exception as err:
                summary["errors"].append({"offset": offset, "error": str(err)})
                time.sleep(5)
                continue
            for index, item in enumerate(batch):
                gql = data.get(f"r{index}")
                if not gql:
                    summary["errors"].append({"repo": item.get("full_name"), "error": "missing graphql repo"})
                    continue
                repo = repo_from_graphql(item, gql)
                owner, name = owner_repo({"full_name": repo["full_name"], "normalized_url": item["normalized_url"]})
                readme = fetch_readme(owner, name, repo["default_branch"])
                manifest = fetch_manifest(owner, name, repo["default_branch"], repo["language"])
                repo["readme"] = {
                    "path": readme.get("path", ""),
                    "status": readme.get("status"),
                    "bytes": readme.get("bytes", 0),
                    "truncated": readme.get("truncated", False),
                    "text": readme.get("text", ""),
                }
                repo["manifest"] = manifest
                repo["fetched_at"] = now_iso()
                lane = repo["license"]["legal_lane"]
                band = repo["band"]
                summary["written"] += 1
                summary["readme_found"] += int(readme.get("status") == 200)
                summary["manifest_found"] += int(manifest.get("status") == 200)
                summary["legal_lane"][lane] = summary["legal_lane"].get(lane, 0) + 1
                summary["by_band"][band] = summary["by_band"].get(band, 0) + 1
                out.write(json.dumps(repo, sort_keys=True) + "\n")
            time.sleep(1)
    summary["completed_at"] = now_iso()
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "README.md").write_text(
        "\n".join(
            [
                "# RepoCard Enrichment Pilot",
                "",
                f"Started: `{summary['started_at']}`",
                f"Completed: `{summary['completed_at']}`",
                f"Selected: `{summary['selected']}`",
                f"Written: `{summary['written']}`",
                f"README found: `{summary['readme_found']}`",
                f"Manifest found: `{summary['manifest_found']}`",
                f"Output: `{out_jsonl}`",
                "",
                "## Legal Lanes",
                "",
                *[f"- `{key}`: {value}" for key, value in sorted(summary["legal_lane"].items())],
                "",
                "## Bands",
                "",
                *[f"- `{key}`: {value}" for key, value in sorted(summary["by_band"].items())],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return summary, out_jsonl


def build_parser():
    parser = argparse.ArgumentParser(description="run a rank-gated RepoCard enrichment pilot")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument(
        "--output-dir",
        default=str(STAGING / f"enrichment-pilot-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"),
    )
    return parser


def main():
    args = build_parser().parse_args()
    if not DB_PATH.exists():
        print(f"missing database: {DB_PATH}", file=sys.stderr)
        sys.exit(1)
    summary, out_jsonl = run_pilot(args.limit, args.batch_size, Path(args.output_dir))
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"jsonl={out_jsonl}")


if __name__ == "__main__":
    main()
