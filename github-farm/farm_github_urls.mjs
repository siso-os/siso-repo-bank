#!/usr/bin/env node
import { execFile } from "node:child_process";
import { appendFile, mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { createReadStream } from "node:fs";
import { createInterface } from "node:readline";
import path from "node:path";

const OUT_DIR = "/Users/shaansisodia/SISO_Workspace/.agents/scratch/github-farm";
const PROGRESS_FILE = path.join(OUT_DIR, "progress.json");
const INDEX_FILE = path.join(OUT_DIR, "INDEX.md");
const CSV_FILE = path.join(OUT_DIR, "all_urls.csv");
const CREATED_START = "2007-01-01";
const CREATED_END = "2026-06-07";
const PAGE_SIZE = 100;
const BUCKET_LIMIT = 1000;
const DEFAULT_MAX_STARS = 999999999;
const LOW_BUDGET_STOP = 120;

const TIERS = [
  { id: "100k_plus", file: "urls_100k_plus.jsonl", low: 100001, high: null, expected: 112, label: ">100000" },
  { id: "50k_100k", file: "urls_50k_100k.jsonl", low: 50001, high: 100000, expected: 323, label: "50001..100000" },
  { id: "10k_50k", file: "urls_10k_50k.jsonl", low: 10001, high: 50000, expected: 4837, label: "10001..50000" },
  { id: "5k_10k", file: "urls_5k_10k.jsonl", low: 5001, high: 10000, expected: 6679, label: "5001..10000" },
  { id: "1k_5k", file: "urls_1k_5k.jsonl", low: 1001, high: 5000, expected: 50400, label: "1001..5000" },
  { id: "500_1k", file: "urls_500_1k.jsonl", low: 501, high: 1000, expected: 56062, label: "501..1000" },
  { id: "100_500", file: "urls_100_500.jsonl", low: 101, high: 500, expected: 339763, label: "101..500" },
];

const COLLECT_QUERY = `
query($q: String!, $first: Int!, $endCursor: String) {
  search(query: $q, type: REPOSITORY, first: $first, after: $endCursor) {
    nodes {
      ... on Repository {
        nameWithOwner
        url
        stargazerCount
        primaryLanguage {
          name
        }
        pushedAt
        createdAt
        forkCount
        shortDescriptionHTML
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
  rateLimit {
    remaining
    resetAt
  }
}`;

const RATE_LIMIT_QUERY = `{ rateLimit { remaining resetAt } }`;

function runGh(args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile("gh", args, { maxBuffer: options.maxBuffer ?? 1024 * 1024 * 128 }, (err, stdout, stderr) => {
      if (err) {
        err.stdout = stdout;
        err.stderr = stderr;
        reject(err);
        return;
      }
      resolve(stdout);
    });
  });
}

async function loadJson(file, fallback) {
  try {
    return JSON.parse(await readFile(file, "utf8"));
  } catch {
    return fallback;
  }
}

async function atomicWriteJson(file, value) {
  const tmp = `${file}.tmp`;
  await writeFile(tmp, JSON.stringify(value, null, 2) + "\n", "utf8");
  await rename(tmp, file);
}

function todayIso() {
  return new Date().toISOString();
}

function starQuery(bucket, sort = "stars-asc") {
  const high = bucket.high ?? DEFAULT_MAX_STARS;
  const parts = [`stars:${bucket.low}..${high}`];
  if (bucket.createdStart && bucket.createdEnd) {
    parts.push(`created:${bucket.createdStart}..${bucket.createdEnd}`);
  }
  parts.push(`sort:${sort}`);
  return parts.join(" ");
}

async function getRateLimit() {
  const stdout = await runGh(["api", "graphql", "-f", `query=${RATE_LIMIT_QUERY}`]);
  return JSON.parse(stdout).data.rateLimit;
}

async function saveProgress(progress) {
  progress.updatedAt = todayIso();
  await atomicWriteJson(PROGRESS_FILE, progress);
}

async function loadExistingNames(file) {
  const names = new Set();
  try {
    const rl = createInterface({ input: createReadStream(file), crlfDelay: Infinity });
    for await (const line of rl) {
      if (!line.trim()) continue;
      try {
        names.add(JSON.parse(line).full_name);
      } catch {
        // Preserve progress even if a previous interrupted write left a bad line.
      }
    }
  } catch {
    // Missing shard is a normal first-run state.
  }
  return names;
}

function repoRecord(node) {
  return {
    full_name: node.nameWithOwner,
    url: node.url,
    stars: node.stargazerCount,
    language: node.primaryLanguage?.name ?? "",
    forks: node.forkCount,
    pushed_at: node.pushedAt,
    description: node.shortDescriptionHTML ?? "",
  };
}

async function fetchSearchPage(query, cursor = null) {
  const args = [
    "api",
    "graphql",
    "-f",
    `query=${COLLECT_QUERY}`,
    "-F",
    `q=${query}`,
    "-F",
    `first=${PAGE_SIZE}`,
  ];
  if (cursor) {
    args.push("-F", `endCursor=${cursor}`);
  }
  const stdout = await runGh(args, { maxBuffer: 1024 * 1024 * 64 });
  const data = JSON.parse(stdout).data;
  return {
    nodes: data.search.nodes ?? [],
    pageInfo: data.search.pageInfo ?? { hasNextPage: false, endCursor: null },
    rateLimit: data.rateLimit,
  };
}

async function appendUnseenRepos(file, nodes, seen) {
  const repos = [];
  for (const node of nodes) {
    const repo = repoRecord(node);
    if (!repo.full_name || seen.has(repo.full_name)) continue;
    seen.add(repo.full_name);
    repos.push(repo);
  }
  if (repos.length > 0) {
    await appendFile(file, repos.map((repo) => JSON.stringify(repo)).join("\n") + "\n", "utf8");
  }
  return repos.length;
}

function completedHighWatermark(completedBuckets) {
  let high = null;
  for (const id of completedBuckets ?? []) {
    const match = /^stars:(\d+)\.\.(\d+)/.exec(id);
    if (!match) continue;
    high = Math.max(high ?? Number(match[2]), Number(match[2]));
  }
  return high;
}

function ensureTierProgress(tier, progress, high) {
  const tierProgress = progress.tiers[tier.id] ?? {};
  progress.tiers[tier.id] = tierProgress;
  tierProgress.completedBuckets ??= [];
  tierProgress.bucketPlan ??= null;
  tierProgress.observedCount ??= null;

  if (!tierProgress.keyset || tierProgress.keyset.strategy !== "stars-keyset-v1") {
    const completedHigh = completedHighWatermark(tierProgress.completedBuckets);
    const nextLow = Math.min(Math.max(tier.low, completedHigh ?? tier.low), high + 1);
    tierProgress.keyset = {
      strategy: "stars-keyset-v1",
      nextLow,
      high,
      cursor: null,
      searchRows: 0,
      windowIndex: 0,
      tie: null,
    };
  } else {
    tierProgress.keyset.high = high;
    tierProgress.keyset.nextLow = Math.min(Math.max(tierProgress.keyset.nextLow ?? tier.low, tier.low), high + 1);
    tierProgress.keyset.cursor ??= null;
    tierProgress.keyset.searchRows ??= 0;
    tierProgress.keyset.windowIndex ??= 0;
    tierProgress.keyset.tie ??= null;
  }

  return tierProgress;
}

function keysetBucketId(low, high, index) {
  return `stars:${low}..${high}|keyset:${index}`;
}

function createdTieBucketId(star, createdStart, createdEnd, index) {
  return `stars:${star}..${star}|created:${createdStart}..${createdEnd}|keyset:${index}`;
}

function lowBudgetReason(rateLimit) {
  if (rateLimit?.remaining < LOW_BUDGET_STOP) {
    return `GraphQL budget low: remaining ${rateLimit.remaining}, reset ${rateLimit.resetAt}`;
  }
  return "";
}

async function collectExactStarByCreated(tier, star, tierProgress, shardFile, seen, progress) {
  const keyset = tierProgress.keyset;
  keyset.tie ??= {
    star,
    createdStart: CREATED_START,
    cursor: null,
    searchRows: 0,
    windowIndex: 0,
  };

  while (keyset.tie && keyset.tie.star === star) {
    const tie = keyset.tie;
    const windowStart = tie.createdStart;
    const query = starQuery(
      { low: star, high: star, createdStart: windowStart, createdEnd: CREATED_END },
      "created-asc",
    );
    const page = await fetchSearchPage(query, tie.cursor);
    const added = await appendUnseenRepos(shardFile, page.nodes, seen);
    tie.searchRows += page.nodes.length;
    tie.cursor = page.pageInfo.endCursor;
    tierProgress.rowsCollected = seen.size;
    await saveProgress(progress);

    const lastNode = page.nodes.at(-1);
    const lastCreated = lastNode?.createdAt?.slice(0, 10) ?? null;
    const reason = lowBudgetReason(page.rateLimit);
    console.error(
      `tie page ${tier.id} stars:${star} created:${windowStart}..${CREATED_END}: +${added}, rows=${seen.size}`,
    );

    if (page.nodes.length === 0 || !page.pageInfo.hasNextPage) {
      tierProgress.completedBuckets = [
        ...new Set([
          ...(tierProgress.completedBuckets ?? []),
          createdTieBucketId(star, windowStart, CREATED_END, tie.windowIndex),
        ]),
      ];
      keyset.tie = null;
      await saveProgress(progress);
      return { complete: true };
    }

    if (tie.searchRows >= BUCKET_LIMIT) {
      if (!lastCreated || lastCreated === windowStart) {
        const reason = `${tier.id}: exact star ${star} still hit the 1000-result cap inside created:${windowStart}`;
        progress.issues.push(reason);
        await saveProgress(progress);
        return { complete: false, reason };
      }
      tierProgress.completedBuckets = [
        ...new Set([
          ...(tierProgress.completedBuckets ?? []),
          createdTieBucketId(star, windowStart, lastCreated, tie.windowIndex),
        ]),
      ];
      keyset.tie = {
        star,
        createdStart: lastCreated,
        cursor: null,
        searchRows: 0,
        windowIndex: tie.windowIndex + 1,
      };
      await saveProgress(progress);
      if (reason) {
        return { complete: false, reason };
      }
      continue;
    }

    if (reason) {
      return { complete: false, reason };
    }
  }

  return { complete: true };
}

async function collectTierKeyset(tier, tierProgress, shardFile, seen, progress) {
  const keyset = tierProgress.keyset;
  const completed = new Set(tierProgress.completedBuckets ?? []);

  while (keyset.nextLow <= keyset.high) {
    if (keyset.tie) {
      const tieStar = keyset.tie.star;
      const tieResult = await collectExactStarByCreated(tier, tieStar, tierProgress, shardFile, seen, progress);
      if (!tieResult.complete) return tieResult;
      keyset.nextLow = tieStar + 1;
    }

    const windowLow = keyset.nextLow;
    const query = starQuery({ low: windowLow, high: keyset.high }, "stars-asc");
    const page = await fetchSearchPage(query, keyset.cursor);
    const added = await appendUnseenRepos(shardFile, page.nodes, seen);
    keyset.searchRows += page.nodes.length;
    keyset.cursor = page.pageInfo.endCursor;
    tierProgress.rowsCollected = seen.size;
    await saveProgress(progress);

    const lastNode = page.nodes.at(-1);
    const lastStars = lastNode?.stargazerCount ?? null;
    const reason = lowBudgetReason(page.rateLimit);
    console.error(`keyset page ${tier.id} stars:${windowLow}..${keyset.high}: +${added}, rows=${seen.size}`);

    if (page.nodes.length === 0 || !page.pageInfo.hasNextPage) {
      if (lastStars !== null) {
        completed.add(keysetBucketId(windowLow, lastStars, keyset.windowIndex));
      }
      tierProgress.completedBuckets = [...completed];
      keyset.nextLow = keyset.high + 1;
      keyset.cursor = null;
      keyset.searchRows = 0;
      await saveProgress(progress);
      return { complete: true };
    }

    if (keyset.searchRows >= BUCKET_LIMIT) {
      if (lastStars === null) {
        keyset.nextLow = keyset.high + 1;
      } else {
        completed.add(keysetBucketId(windowLow, lastStars, keyset.windowIndex));
        tierProgress.completedBuckets = [...completed];
        keyset.cursor = null;
        keyset.searchRows = 0;
        keyset.windowIndex += 1;

        if (lastStars === windowLow) {
          keyset.tie = {
            star: lastStars,
            createdStart: CREATED_START,
            cursor: null,
            searchRows: 0,
            windowIndex: 0,
          };
          await saveProgress(progress);
          if (reason) {
            return { complete: false, reason };
          }
          const tieResult = await collectExactStarByCreated(tier, lastStars, tierProgress, shardFile, seen, progress);
          if (!tieResult.complete) return tieResult;
          keyset.nextLow = lastStars + 1;
        } else {
          keyset.nextLow = lastStars;
        }
      }
      await saveProgress(progress);
      if (reason) {
        return { complete: false, reason };
      }
      continue;
    }

    if (reason) {
      return { complete: false, reason };
    }
  }

  return { complete: true };
}

async function writeCombinedCsvAndIndex(progress, finalStatus, finalReason = "") {
  const rows = [["full_name", "stars", "url"]];
  const tierRows = {};
  let total = 0;

  for (const tier of TIERS) {
    const file = path.join(OUT_DIR, tier.file);
    let count = 0;
    try {
      const rl = createInterface({ input: createReadStream(file), crlfDelay: Infinity });
      for await (const line of rl) {
        if (!line.trim()) continue;
        const repo = JSON.parse(line);
        rows.push([repo.full_name, repo.stars, repo.url]);
        count += 1;
      }
    } catch {
      // Missing shard means zero collected.
    }
    tierRows[tier.id] = count;
    total += count;
  }

  rows.sort((a, b) => {
    if (a[0] === "full_name") return -1;
    if (b[0] === "full_name") return 1;
    return Number(b[1]) - Number(a[1]);
  });
  await writeFile(CSV_FILE, rows.map((row) => row.map(csvEscape).join(",")).join("\n") + "\n", "utf8");

  const completeTierIds = TIERS.filter((tier) => progress.tiers[tier.id]?.status === "complete").map((tier) => tier.id);
  const tierLines = TIERS.map((tier) => {
    const tierProgress = progress.tiers[tier.id] ?? {};
    const observed = tierProgress.observedCount ?? "not planned";
    const status = tierProgress.status ?? "pending";
    return `| ${tier.file} | ${tier.label} | ${tier.expected} | ${observed} | ${tierRows[tier.id]} | ${status} |`;
  });
  const summary = [
    "# GitHub URL Funnel Farm",
    "",
    "- Method: GitHub GraphQL search via `gh api graphql`, star-ascending keyset pagination, created-date keyset fallback only when an exact star value hits the 1,000-result cap.",
    "- Payload: repository URL/star metadata only; no README fetches.",
    `- Status: ${finalStatus}${finalReason ? ` (${finalReason})` : ""}`,
    `- Total rows collected: ${total}`,
    `- Tiers complete: ${completeTierIds.length ? completeTierIds.join(", ") : "none"}`,
    `- Updated at: ${todayIso()}`,
    "",
    "| File | Query band | Expected rows | Observed bucket count | Rows collected | Status |",
    "|---|---:|---:|---:|---:|---|",
    ...tierLines,
    "",
    "## Issues / Gaps",
    "",
    progress.issues.length ? progress.issues.map((issue) => `- ${issue}`).join("\n") : "- None recorded.",
    "",
  ].join("\n");
  await writeFile(INDEX_FILE, summary, "utf8");

  return { total, completeTierIds, tierRows };
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""').replaceAll(/\r?\n/g, " ")}"` : text;
}

async function main() {
  await mkdir(OUT_DIR, { recursive: true });
  const progress = await loadJson(PROGRESS_FILE, {
    version: 1,
    createdAt: todayIso(),
    updatedAt: todayIso(),
    tiers: {},
    issues: [],
  });

  const initialLimit = await getRateLimit();
  console.error(`GraphQL budget start: ${JSON.stringify(initialLimit)}`);

  let finalStatus = "PASS";
  let finalReason = "";

  for (const tier of TIERS) {
    if (progress.tiers[tier.id]?.status === "complete") {
      console.error(`skip complete tier ${tier.id}`);
      continue;
    }
    const high = tier.high ?? DEFAULT_MAX_STARS;
    const tierProgress = ensureTierProgress(tier, progress, high);

    const beforeTierLimit = await getRateLimit();
    if (beforeTierLimit.remaining < LOW_BUDGET_STOP) {
      finalStatus = "PARTIAL";
      finalReason = `GraphQL budget low before ${tier.id}: remaining ${beforeTierLimit.remaining}, reset ${beforeTierLimit.resetAt}`;
      progress.issues.push(finalReason);
      await saveProgress(progress);
      break;
    }

    const shardFile = path.join(OUT_DIR, tier.file);
    const seen = await loadExistingNames(shardFile);
    tierProgress.status = "collecting";
    tierProgress.rowsCollected = seen.size;
    await saveProgress(progress);

    try {
      console.error(`collecting tier ${tier.id} ${tier.label} via keyset stars:${tierProgress.keyset.nextLow}..${high}`);
      const result = await collectTierKeyset(tier, tierProgress, shardFile, seen, progress);
      if (!result.complete) {
        finalStatus = "PARTIAL";
        finalReason = result.reason;
        progress.issues.push(`${tier.id}: ${result.reason}`);
      }
    } catch (err) {
      finalStatus = "PARTIAL";
      finalReason = `failed ${tier.id}: ${(err.stderr || err.message || "").toString().slice(0, 500)}`;
      progress.issues.push(finalReason);
      tierProgress.rowsCollected = seen.size;
      await saveProgress(progress);
    }

    if (tierProgress.keyset.nextLow > tierProgress.keyset.high && finalStatus !== "PARTIAL") {
      tierProgress.status = "complete";
      tierProgress.rowsCollected = seen.size;
      tierProgress.observedCount ??= seen.size;
      await saveProgress(progress);
      console.error(`tier complete ${tier.id}: rows=${seen.size}`);
    } else {
      tierProgress.status = "partial";
      tierProgress.rowsCollected = seen.size;
      await saveProgress(progress);
      break;
    }
  }

  const finalLimit = await getRateLimit();
  progress.finalRateLimit = finalLimit;
  await saveProgress(progress);
  const index = await writeCombinedCsvAndIndex(progress, finalStatus, finalReason);
  console.error(`farm ${finalStatus}: total=${index.total}, complete=${index.completeTierIds.join(",") || "none"}`);
}

main().catch(async (err) => {
  console.error(err);
  process.exit(1);
});
