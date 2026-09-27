# Operation: Lean Burn

**A cost doctrine for running long agent sessions on metered inference budgets.**

Lean burn is the engine principle of running on excess air — less fuel for the same power.
The engine runs *leaner*, not idler. Same idea, applied to token spend: same work, same model,
a fraction of the bill, achieved by changing *how the session is shaped* rather than by shrinking
what the session does.

---

## ⚠️ BEFORE YOU TRUST A NUMBER FROM THIS: the rate table is an INPUT, not the truth

**This tool shipped carrying the wrong peak-price window — a different vendor's schedule — and
reported peak/off-peak BACKWARDS the entire time.** The table had a date on it, said "verified", and
described another company. A fixed table that *looks* measured is still a claim.

Before reading anything this prints:

1. **Open your provider's pricing page and edit `rates.json` yourself.** Prices and windows change.
2. **`peak_windows_utc` is in UTC.** Leave it `[]` if your provider is flat-rate.
3. **A session's regime comes from its timestamp.** Per-call timestamps are not stored, so a session
   straddling a boundary is approximate — the tool says so, and it means it.

Anything it cannot price is reported as **unpriced, never guessed**. If most sessions come back
unpriced, that is the tool telling you the rate table is incomplete — not failing.

---

## Point it at YOUR ledger

```bash
python3 src/meter.py --selftest                              # proves the pricer can be wrong correctly
python3 src/meter.py --db ~/your-agent/state.db --days 1
python3 src/meter.py --db ~/your-agent/state.db --sessions 10
python3 src/meter.py --db ~/your-agent/state.db --session <id>
```

It reads `sessions` with: `id, model, last_activity_at, started_at, input_tokens, output_tokens,
cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count, billing_provider`.
**Different column names? Edit `WINDOW_SQL` at the top of `src/meter.py`.** Read-only, no network.

---

## The finding that makes this possible

Modern API pricing has two levers, and they are not equal.

**Lever 1 — cached input.** A provider-side context cache re-reads a long, stable prefix at a
small fraction of the full input rate. On DeepSeek V4.1-Flash the off-peak spread is
**$0.003 / 1M cached input vs $0.15 / 1M uncached** — a **50x** difference on the same tokens.

**Lever 2 — time of day.** Some providers bill peak hours at exactly double. DeepSeek's peak is
**Mon–Fri 01:00–04:00 and 06:00–10:00 UTC**; everything else, including all weekend, is off-peak.

Most of an agent session's input *is* cache-eligible — you re-send the same long prefix every
turn. So the dominant term in your bill is not how much you think, it is **whether that prefix
stays cacheable and when you run.**

---

## The laws

### 1. Keep the prefix stable — the cache is the whole game
Cache hits depend on the leading tokens of the request being byte-identical between calls.
Anything that rewrites the front of your context (reordering system files, churning a big
memory block, restarting the session constantly) invalidates the cache and silently moves you
from the cheap column to the expensive one. **Stability is worth more than cleverness.**

### 2. Read by address, never by inhaling
Loading a whole 26k-token file to answer one question re-bills the whole file on every turn
that carries it. An indexed lookup that returns only the ~250-token chunk you needed costs
roughly 100x less for the same fact. Build the index once; use it every session.

### 3. One decisive spend per 24h
Set a daily cap on the weekly budget and spend it in **one high-value block** rather than
dribbling across the day. Dribbling means cold caches, repeated context rebuilds, and paying
the expensive column over and over for the same prefix.

### 4. Batch tool calls into one round trip
Ten tool calls as ten round trips re-send the full context ten times. Ten calls composed into
one script send it once and return ten results. The saving scales with how long your context is
— which is exactly when you need it.

### 5. Run heavy work off-peak
The clock is published; shift the big blocks. A 2x rate difference on a long session is a free
50% discount for nothing but scheduling.

### 6. Auxiliary routes belong on the same model as the main route
A "cheap" model with an expensive cache rate or a high cached-input price can cost *more* than
the good model once the cache is counted. Compare candidates on **cached** rates, not headline
rates.

### 7. Never trim what the agent *is* — trim the plumbing
Budget discipline applies to duplicated reads, stale files, and bloated scaffolding. It never
applies to identity, memory, or capability. An agent that saves money by forgetting who it is
has not saved anything.

### 8. A cap is a guard rail, not a muzzle
The cap exists to make the spend *deliberate*, not to make it small for its own sake. Guard the
levers; do not treat cost as a reason to skip verification.

### 9. Measure with a meter, never with a feeling
You cannot tell a 50x cache win from a 2x one by intuition. Read the counters. Anything you
cannot price, label as unpriced rather than guessing a number.

---

## The meter

Providers show a coarse total. The per-session truth usually already exists in a local ledger —
agent frameworks record token counters (input, output, cache-read, cache-write) on every API
call. `src/meter.py` reads that ledger and prices it at the **correct rate for the hour the
work happened**, plus the number that actually drives decisions:

```
cache-hit ratio          how much of your input rode the cheap column
cold-cache equivalent    what the same work would have cost with no cache
effective saving         the difference, as a percentage
```

Usage:

```bash
# point at your ledger and a rate table
python3 src/meter.py --db ~/.hermes/state.db --rates rates.json --days 1

# per-session detail
python3 src/meter.py --rates rates.json --sessions 10
```

It reads SQLite read-only, makes no network calls, and prints `not priced` for anything it has
no published rate for. Rates live in `rates.json` — add your provider there.

### Rate table format

```json
{
  "providers": {
    "deepseek-flash": {
      "match": ["deepseek"],
      "offpeak": { "miss": 0.15, "out": 0.60, "cache": 0.003 },
      "peak":    { "miss": 0.30, "out": 1.20, "cache": 0.006 }
    }
  },
  "peak_windows_utc": [[1, 4], [6, 10]],
  "peak_weekdays_only": true
}
```

`miss` = uncached input per 1M, `out` = output per 1M, `cache` = cached input per 1M.

---

## What this is not

- Not a claim that cheaper models are equal. The doctrine changes the **bill**, not the model.
- Not a proxy for account-level spend. It prices a local ledger against published rates.
- Not a substitute for the provider's own meter. Use both; they answer different questions.

## Prior art and scope

The peak/off-peak structure and cache pricing cited here are DeepSeek's published rates,
verified 2026-09-15 from provider docs and independently corroborated. Other providers publish
their own windows; encode yours in `rates.json`.

The doctrine is provider-agnostic. The numbers in `rates.json` are one snapshot and will age —
re-check them rather than trusting a file.

## License

MIT. See `LICENSE`.
