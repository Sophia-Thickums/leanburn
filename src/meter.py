#!/usr/bin/env python3
"""meter.py — price a local agent ledger at the correct rate for the hour the work happened.

Reads the session/token counters your agent framework already records, applies published
per-million rates, and reports the number that actually drives decisions: the cache-hit
ratio, the cold-cache equivalent, and the effective saving.

Read-only. No network. Anything it cannot price is reported as unpriced, never guessed.

  python3 meter.py --db ~/.hermes/state.db --rates rates.json --days 1
  python3 meter.py --rates rates.json --sessions 10
  python3 meter.py --rates rates.json --session <id>
  python3 meter.py --rates rates.json --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

M = Decimal("1000000")

WINDOW_SQL = """SELECT id, model, last_activity_at AS ts, started_at,
                       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                       reasoning_tokens, api_call_count, billing_provider
                FROM sessions"""


def load_rates(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    for name, p in cfg.get("providers", {}).items():
        for regime in ("offpeak", "peak"):
            if regime in p:
                p[regime] = {k: Decimal(str(v)) for k, v in p[regime].items()}
        p.setdefault("peak", p.get("offpeak"))
    return cfg


def is_peak(cfg: dict, ts: float) -> bool:
    if not ts:
        return False
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if cfg.get("peak_weekdays_only", True) and dt.weekday() >= 5:
        return False
    return any(lo <= dt.hour < hi for lo, hi in cfg.get("peak_windows_utc", []))


def match_provider(cfg: dict, model: str | None):
    if not model:
        return None, None
    m = model.lower()
    for name, p in cfg.get("providers", {}).items():
        for needle in p.get("match", []):
            if needle.lower() in m:
                return name, p
    return None, None


def price_row(cfg: dict, r: dict):
    """-> (usd or None, note, cold_usd or None)"""
    name, p = match_provider(cfg, r.get("model"))
    if p is None:
        return None, f"no rate table matching {r.get('model')!r}", None
    regime = "peak" if is_peak(cfg, r.get("ts") or 0) else "offpeak"
    t = p.get(regime) or p.get("offpeak")
    if not t:
        return None, f"provider {name!r} has no {regime} row", None
    inp = Decimal(r.get("input_tokens") or 0)
    out = Decimal(r.get("output_tokens") or 0)
    cr = Decimal(r.get("cache_read_tokens") or 0)
    cw = Decimal(r.get("cache_write_tokens") or 0)
    # no published cache-write rate is assumed; cache writes bill as uncached input
    if cw:
        inp += cw
    usd = (inp * t.get("miss", Decimal(0))
           + out * t.get("out", Decimal(0))
           + cr * t.get("cache", Decimal(0))) / M
    cold = ((inp + cr) * t.get("miss", Decimal(0)) + out * t.get("out", Decimal(0))) / M
    return usd, f"{name} {regime}", cold


def fmt_usd(d: Decimal) -> str:
    return f"${d.quantize(Decimal('0.0001'))}" if d < Decimal("0.01") else f"${d.quantize(Decimal('0.01'))}"


def local(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%m-%d %H:%M") if ts else "?"


def selftest():
    """Prove the arithmetic on a hand-computed case before trusting it on real data.

    Round-trips through load_rates via a temp file so this exercises the SAME code
    path a real run uses (a selftest that bypasses the loader can pass while the
    loader is broken).
    """
    import tempfile
    raw = {"providers": {"p": {"match": ["x"], "offpeak": {"miss": 0.15, "out": 0.60, "cache": 0.003},
                               "peak": {"miss": 0.30, "out": 1.20, "cache": 0.006}}},
           "peak_windows_utc": [[1, 4], [6, 10]], "peak_weekdays_only": True}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = f.name
    try:
        cfg = load_rates(path)
    finally:
        os.unlink(path)
    # 1,000,000 cached + 100,000 uncached + 50,000 out, off-peak:
    #   1.0*0.003 + 0.1*0.15 + 0.05*0.60 = 0.003 + 0.015 + 0.03 = 0.048
    row = {"model": "x-1", "ts": 0, "input_tokens": 100_000, "output_tokens": 50_000,
           "cache_read_tokens": 1_000_000, "cache_write_tokens": 0}
    usd, note, cold = price_row(cfg, row)
    want = Decimal("0.048000")
    ok1 = usd == want
    # cold = (1.1M * 0.15 + 50k * 0.60)/1M = 0.165 + 0.03 = 0.195
    ok2 = cold == Decimal("0.195000")
    # peak detection: 2026-09-15 is a Tuesday; 02:00 UTC is inside [1,4)
    ts_peak = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc).timestamp()
    ts_off = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp()
    ts_weekend = datetime(2026, 9, 19, 2, 0, tzinfo=timezone.utc).timestamp()  # Saturday
    ok3 = is_peak(cfg, ts_peak) and not is_peak(cfg, ts_off) and not is_peak(cfg, ts_weekend)
    print(f"  off-peak price == {want}      : {'PASS' if ok1 else 'FAIL'}  (got {usd})")
    print(f"  cold-cache    == {Decimal('0.195')}   : {'PASS' if ok2 else 'FAIL'}  (got {cold})")
    print(f"  peak-window detection        : {'PASS' if ok3 else 'FAIL'}")
    return 0 if (ok1 and ok2 and ok3) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/.hermes/state.db"))
    ap.add_argument("--rates", required=False, help="rate table json")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--sessions", type=int, default=0)
    ap.add_argument("--session", default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        print("meter.py selftest")
        sys.exit(selftest())
    if not a.rates:
        sys.exit("--rates is required (see README for the format)")
    cfg = load_rates(a.rates)

    if not os.path.exists(a.db):
        sys.exit(f"ledger not found: {a.db} (pass --db)")
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sql, params = WINDOW_SQL, []
    if a.session:
        sql += " WHERE id = ?"
        params.append(a.session)
    elif a.days:
        sql += " WHERE last_activity_at >= ?"
        params.append((datetime.now(timezone.utc) - timedelta(days=a.days)).timestamp())
    sql += " ORDER BY last_activity_at DESC"
    rows = [dict(r) for r in con.execute(sql, params)]
    con.close()
    if not rows:
        print("no sessions in range")
        return

    if a.session:
        r = rows[0]
        usd, note, cold = price_row(cfg, r)
        tin = (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0) + (r["cache_write_tokens"] or 0)
        hit = (r["cache_read_tokens"] or 0) / tin * 100 if tin else 0
        print(f"{r['id']}  {local(r['ts'])}  {r['model']}")
        print(f"  calls {r['api_call_count'] or 0:,}  in {r['input_tokens'] or 0:,}"
              f" + cache-read {r['cache_read_tokens'] or 0:,} = {tin:,}  out {r['output_tokens'] or 0:,}")
        print(f"  cache-hit {hit:.1f}%   " + (f"cost {fmt_usd(usd)} ({note})" if usd is not None
                                             else f"UNAVAILABLE ({note})"))
        if cold:
            print(f"  cold-cache equivalent {fmt_usd(cold)}  -> saved {((cold-usd)/cold*100):.0f}%")
        return

    if a.sessions:
        print(f"=== last {a.sessions} sessions ===\n")
        for r in rows[: a.sessions]:
            usd, note, cold = price_row(cfg, r)
            tin = sum(r[k] or 0 for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
            hit = (r["cache_read_tokens"] or 0) / tin * 100 if tin else 0
            cost = f"{fmt_usd(usd)} ({note})" if usd is not None else f"UNAVAILABLE ({note})"
            print(f"  {r['id']}  {local(r['ts'])}  {r['model']}")
            print(f"    calls {r['api_call_count'] or 0:>5,}  in {tin:>12,}  out {r['output_tokens'] or 0:>9,}"
                  f"  hit {hit:5.1f}%  {cost}")
        print()

    tin = tout = tcr = tcall = 0
    tot = coldtot = Decimal(0)
    unpriced, peaks = [], 0
    for r in rows:
        tin += r["input_tokens"] or 0
        tcr += r["cache_read_tokens"] or 0
        tout += r["output_tokens"] or 0
        tcall += r["api_call_count"] or 0
        u, note, cold = price_row(cfg, r)
        if u is None:
            unpriced.append((r["id"], r["model"]))
        else:
            tot += u
            coldtot += cold or Decimal(0)
        peaks += 1 if is_peak(cfg, r["ts"] or 0) else 0

    total_in = tin + tcr
    print("=" * 62)
    print(f"WINDOW last {a.days}d   sessions {len(rows)}   API calls {tcall:,}")
    print(f"  uncached input {tin:>12,}")
    print(f"  cached input   {tcr:>12,}")
    print(f"  output         {tout:>12,}")
    if total_in:
        print(f"  cache-hit ratio {tcr/total_in*100:.1f}% of all input tokens")
    if tot:
        print(f"  COST {fmt_usd(tot)}")
        if coldtot:
            print(f"  cold-cache equivalent {fmt_usd(coldtot)}  -> saved {((coldtot-tot)/coldtot*100):.0f}%")
    if unpriced:
        print(f"  NOT PRICED: {len(unpriced)} session(s): "
              f"{', '.join(sorted({m for _, m in unpriced}))}")
    print(f"  sessions in a peak window: {peaks} / {len(rows)}")
    print("=" * 62)
    print("Session counters are priced at the session timestamp's regime; per-call")
    print("timestamps are not stored, so a session straddling a boundary is approximate.")


if __name__ == "__main__":
    main()
