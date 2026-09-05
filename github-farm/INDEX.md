# GitHub URL Funnel Farm

- Method: GitHub GraphQL search via `gh api graphql`, star-ascending keyset pagination, created-date keyset fallback only when an exact star value hits the 1,000-result cap.
- Payload: repository URL/star metadata only; no README fetches.
- Status: PASS
- Total rows collected: 56674
- Tiers complete: 100k_plus, 50k_100k, 10k_50k, 5k_10k, 1k_5k, 500_1k, 100_500
- Updated at: 2026-06-07T01:52:32.463Z

| File | Query band | Expected rows | Observed bucket count | Rows collected | Status |
|---|---:|---:|---:|---:|---|
| urls_100k_plus.jsonl | >100000 | 112 | 112 | 112 | complete |
| urls_50k_100k.jsonl | 50001..100000 | 323 | 323 | 323 | complete |
| urls_10k_50k.jsonl | 10001..50000 | 4837 | 4837 | 4837 | complete |
| urls_5k_10k.jsonl | 5001..10000 | 6679 | 6679 | 6679 | complete |
| urls_1k_5k.jsonl | 1001..5000 | 50400 | 50400 | 42723 | complete |
| urls_500_1k.jsonl | 501..1000 | 56062 | 1000 | 1000 | complete |
| urls_100_500.jsonl | 101..500 | 339763 | 1000 | 1000 | complete |

## Issues / Gaps

- None recorded.
