#!/usr/bin/env python3
"""
foundry_usage_logger — the smallest reliable token ledger for DIRECT (non-Bifrost)
MiniMax/codex callers.

WHY: ~54B of the ~60B MiniMax tokens we actually ran never hit Bifrost — they went
straight to api.minimax.io from pi-workers, async-dispatch, codex-mini, and
foundry_categorize.py. Those callers get no Bifrost logs.db row, so the usage tab
under-counts MiniMax massively. This helper closes the gap GOING FORWARD: every direct
caller calls log_usage(...) after each response, appending one row to a tiny sqlite
ledger. The Foundry usage tab reads that ledger as an additional source.

CONTRACT (one function):
    log_usage(tool, model, input_tok, output_tok, machine=<auto>, ledger_path=<auto>)

Each MiniMax response carries usage.input_tokens / usage.output_tokens (Anthropic-compat
shape on api.minimax.io/anthropic). Pass those straight through.

DESIGN:
- Ledger = ~/foundry-data/foundry_usage_ledger.db on the MINI (where the Foundry interface
  reads it). Override with FOUNDRY_USAGE_LEDGER env or the ledger_path arg.
- Table foundry_usage_ledger(ts, tool, machine, model, input_tok, output_tok). Created on
  first write. WAL so a reader (the interface, RO) never blocks a writer.
- NEVER raises: a logging failure must not break the caller's real work. All errors are
  swallowed (best-effort) and optionally surfaced via FOUNDRY_USAGE_DEBUG=1.
- Other callers (pi-workers, async-dispatch, codex-mini) adopt the SAME one-liner:
      from foundry_usage_logger import log_usage
      log_usage("minimax", model, usage.get("input_tokens",0), usage.get("output_tokens",0))
"""
import os
import sqlite3
import socket
import datetime

_DEFAULT_LEDGER = os.path.expanduser(
    os.environ.get("FOUNDRY_USAGE_LEDGER", "~/foundry-data/foundry_usage_ledger.db")
)
_DDL = """
CREATE TABLE IF NOT EXISTS foundry_usage_ledger (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  tool        TEXT    NOT NULL,
  machine     TEXT    NOT NULL,
  model       TEXT,
  input_tok   INTEGER NOT NULL DEFAULT 0,
  output_tok  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ful_tool  ON foundry_usage_ledger(tool);
CREATE INDEX IF NOT EXISTS idx_ful_ts    ON foundry_usage_ledger(ts);
"""


def _machine() -> str:
    h = socket.gethostname().lower()
    if "mini" in h:
        return "mini"
    if "macbook" in h or "laptop" in h:
        return "laptop"
    return h or "unknown"


def log_usage(tool: str, model: str, input_tok, output_tok,
              machine: str = None, ledger_path: str = None) -> bool:
    """Append one usage row. Best-effort: returns True on success, False on any failure
    (never raises). Pass the MiniMax response's usage.input_tokens / output_tokens."""
    try:
        path = os.path.expanduser(ledger_path or _DEFAULT_LEDGER)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        con = sqlite3.connect(path, timeout=10)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=8000")
            con.executescript(_DDL)
            con.execute(
                "INSERT INTO foundry_usage_ledger (ts, tool, machine, model, input_tok, output_tok)"
                " VALUES (?,?,?,?,?,?)",
                (
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    str(tool or "minimax"),
                    str(machine or _machine()),
                    str(model or ""),
                    int(input_tok or 0),
                    int(output_tok or 0),
                ),
            )
            con.commit()
        finally:
            con.close()
        return True
    except Exception as e:
        if os.environ.get("FOUNDRY_USAGE_DEBUG"):
            import sys
            print(f"[usage-logger] log failed (non-fatal): {e}", file=sys.stderr, flush=True)
        return False


if __name__ == "__main__":
    # self-test: write a probe row, read it back
    ok = log_usage("selftest", "MiniMax-M3", 123, 45)
    path = os.path.expanduser(_DEFAULT_LEDGER)
    con = sqlite3.connect(path)
    n = con.execute("SELECT COUNT(*) FROM foundry_usage_ledger").fetchone()[0]
    last = con.execute(
        "SELECT ts,tool,machine,model,input_tok,output_tok FROM foundry_usage_ledger ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    print(f"write ok={ok} | ledger={path} | rows={n} | last={last}")
