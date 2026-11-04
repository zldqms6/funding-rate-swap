# FundingRateSwap

A fixed-for-floating swap on perpetual-futures funding. Two traders agree on a fixed funding rate for an asset (say BTC, 0.01% per 8 hours) over a term. After each period, GenLayer validators pull the public funding history of five venues (Binance, OKX, Gate, KuCoin, Hyperliquid), add up what each venue actually paid in code, and take the median. The difference to the fixed rate moves from one trader's margin to the other's. Margin is posted up front and covers the worst case, so nobody can default.

## Why I built this

I trade Korean stocks and crypto, and in crypto most of my positions are perps. Funding is the cost I can't plan. A long can bleed 3 bp a day for a month and then get paid for a week, and the carry on a basis trade (long spot, short perp) is only known after the fact. In rates markets this risk has a standard answer: you swap the floating rate for a fixed one. For perp funding there's no neutral place to do that.

The moment you try to write such a swap between two people, the hard part is the reference rate:
- **Which venue?** On 2026-10-14, BTC funding paid **-0.44 bp** on Binance and **+2.79 bp** on Hyperliquid for the same UTC day. Whoever picks the venue picks the result.
- **Venues line up badly.** Binance, OKX, Gate and KuCoin pay every 8 hours, Hyperliquid every hour. Binance and Hyperliquid stamp prints a few milliseconds after the hour, Gate 2 seconds after. A naive "between start and end" query silently drops the last print.
- **Venues go down or block you.** From Studionet's validators, `fapi.binance.com` answers 451 and Bybit answers 403 "Forbidden for your country".
- **Settlement needs trust.** Whoever computes the number and moves the money can lie about it.

Funding history is public and final once paid, so none of this needs a judgment call. It needs a rule both sides sign up to in advance, and several independent parties that each fetch the data and check the arithmetic. That's what this contract is.

There's no LLM in it. Every input is a published number, so the code decides everything. GenLayer's part is that each validator fetches the five venues itself and only signs off on a median it can reproduce.

## How it works

```
propose(asset, maker_pays_fixed, fixed_rate, cap, notional, start, period_s, n_periods, taker_only)
    payable: exactly the margin = notional x cap x (8h intervals in the term), rounded up
    fixed_rate, cap   decimal per 8h, e.g. "0.0001" = 0.01%; |fixed| <= 1%, 0 < cap <= 1%
    start             a future 00/08/16 UTC funding boundary, within 30 days
    period_s          a multiple of 8h, at most 7 days; n_periods 1-90
accept(swap)          the taker, before start, posts the same margin
settle(swap)          anyone, 10 min after a period ends
  ├─ fixing for (period start, period end]: cached if anyone already fixed it, else
  │    each venue: one public request, sum the prints whose hour is inside the window,
  │                only if every funding time is there exactly once (3 per day on the
  │                8h venues, 24 on Hyperliquid); otherwise the venue is left out
  │    median of the complete venues; fewer than 3 → error, nothing recorded
  ├─ diff = floating - fixed x intervals, clamped to ±cap x intervals
  └─ notional x |diff| moves from the losing margin to the winning one
     after the last period both margins are paid out
fix(asset, from, to)  anyone: the same fixing for any closed past window, stored and reused
request_close(swap)   both sides: the swap ends after the running period (before start: refund now)
cancel(swap)          the maker, before anyone accepts
void(swap)            anyone, if a period still has no fixing 30 days after it ended: skipped, no payment
```

**Who pays whom.** If floating is above fixed, the fixed payer receives. A long who wants a known funding cost pays fixed (`maker_pays_fixed = true`); a basis trader who wants to lock what the shorts earn receives fixed.

**Margin and default.** Each side posts `notional x cap x intervals`. A period can never move more than `notional x cap x its intervals`, so the margin covers every period of the term in full. Moves past the cap are not paid; that's the price of having no liquidations and no margin calls. With a 0.01% cap, a 30-day swap on 10,000 GEN needs 90 GEN (0.9%) per side.

### The reference rate

| Venue | Endpoint | Interval |
|---|---|---|
| Binance | `www.binance.com/fapi/v1/fundingRate` | 8h |
| OKX | `www.okx.com/api/v5/public/funding-rate-history` (`realizedRate`) | 8h |
| Gate | `api.gateio.ws/api/v4/futures/usdt/funding_rate` | 8h |
| KuCoin | `api-futures.kucoin.com/api/v1/contract/funding-rates` | 8h |
| Hyperliquid | `api.hyperliquid.xyz/info` `fundingHistory` | 1h |

I checked reachability from Studionet's validators with probe contracts before picking these. `fapi.binance.com` returns 451 there, but the same API under `www.binance.com` answers normally. Bybit returned 403 on both `api.bybit.com` and `api.bybitglobal.com` ("Forbidden for your country"), so it's out. With Bybit in, every node would skip it the same way, but it would never count.

Details that matter:
- **Time alignment.** A print at time T pays for the interval ending at T, so a period (ps, pe] takes the prints stamped after ps up to and including pe. Timestamps are rounded to the minute, and each request window is shifted by 60 seconds so a print stamped `pe + 3 ms` is in and one stamped `ps + 2 s` is out.
- **Complete or nothing.** A venue counts only if every expected funding time is present once. A missing Hyperliquid hour doesn't count as zero; the venue is left out and the median comes from the other four.
- **Exact arithmetic.** Rates are parsed from the response text into integers of 1e-18 (KuCoin sends `1.0E-4`, OKX up to 16 decimals). No floats anywhere, so every node gets the same integer.
- **Median.** With 5 venues it's the middle one, with 4 the floor of the mean of the middle two. At least 3 venues are required.

### What validators check

Settled funding prints don't change, so for any venue that both the leader and a validator see, the sums must be **exactly** equal. What can differ between nodes is which venues answer. A validator accepts the leader's fixing only if:
1. The leader's own numbers add up: its median is the median of the venues it lists, there are at least 3, and each has the full number of prints for the window.
2. Every venue both of them saw matches exactly.
3. The validator itself checked at least 3 venues. It never vouches for a median it mostly couldn't check.
4. The median over the union (the validator's venues, plus any the validator couldn't reach as the leader reported them) equals the leader's median. So a leader can't drop an inconvenient venue: if the validator has it, it's back in and the median moves.

Errors are tagged. `[EXTERNAL]` (no venue lists the asset, or too few have complete data) must match the validator's own error word for word. `[TRANSIENT]` (venues not answering) only needs the validator to also hit a transient failure. `[EXPECTED]` covers input errors. Nothing is recorded on an error, and the call can be repeated.

## Live run on Studionet (2026-11-04)

Contract [`0x19d9723C681C74C755bEf8413F5a9E6134731B4E`](https://explorer-studio.genlayer.com/address/0x19d9723C681C74C755bEf8413F5a9E6134731B4E), deployed with my own key through `genlayer-js` (`scripts/deploy.mjs`). `scripts/live_demo.mjs` wrote everything to `demo_result.json`. Every transaction below is `ACCEPTED` with leader `SUCCESS` and every validator that voted AGREE (the rest IDLE), except the NOPE row, where the leader correctly errored and the validators agreed with the error.

**Fixings on real past windows** (bp of notional for the whole window; median in bold):

| Window | Binance | OKX | Gate | KuCoin | Hyperliquid | Median |
|---|---|---|---|---|---|---|
| BTC 2026-10-14 (1 day) | -0.44 | **1.70** | 1.35 | 2.05 | 2.79 | **1.7045** |
| BTC 2026-10-11 (1 day) | 0.09 | -0.26 | **-0.21** | 0.71 | -0.40 | **-0.21** |
| BTC 2026-10-05 to 10-12 (7 days, 21 / 168 prints) | 1.05 | **4.58** | 1.78 | 10.18 | 13.00 | **4.5836** |
| ETH 2026-11-03 (1 day) | 2.88 | **2.80** | 2.49 | 2.26 | 3.00 | **2.7996** |
| NOPE (no venue lists it) | `[]` | 51001 | 400 | 404000 | 500 `null` | leader `ERROR` `[EXTERNAL] only 0 venues have complete NOPE funding ...`, validators AGREE |

The on-chain medians are identical to the integers my test helper computes from the saved fixtures, which were fetched separately from my machine.

What I take from it:
- **The venue choice is bigger than the trade.** Over the week of 10-05, the same BTC position paid 1.05 bp on Binance and 13.00 bp on Hyperliquid. A swap settled on either one would really be a bet on that venue. The median landed on OKX's 4.58.
- **Funding goes negative.** On 10-11 the median was -0.21 bp, so a fixed receiver at 0 would have paid that day.
- **Hourly and 8-hourly venues line up.** The 7-day window has exactly 168 Hyperliquid prints and 21 on each other venue, and every validator reproduced the same integers.

**Swaps** (BTC, 10,000 GEN notional, 0.01% cap, one 8h period from 2026-11-04 16:00 UTC, so 1 GEN margin per side):

| Swap | Steps | Result |
|---|---|---|
| #0 maker pays 0.01% fixed | propose, accept | `active`, 1 GEN on each side |
| #1 maker receives 0.008% fixed | propose, cancel | `cancelled`, maker refunded |
| #2 maker receives 0.008% fixed | propose, accept, request_close by both before start | `closed`, `end_period` 0, both refunded |

**Not done yet:** swap #0's period ends 2026-11-05 00:00 UTC. `live_demo.mjs` is waiting to call `settle` at 00:11 UTC. If that process is gone, `node scripts/live_demo.mjs 0x19d9723C681C74C755bEf8413F5a9E6134731B4E resume` does the settlement and records it. The settlement code is the same `_fixing` path as the fixings above, plus the arithmetic covered by the tests, but a live settlement on Studionet is not recorded here yet.

## API

| Method | Who | What |
|---|---|---|
| `propose(asset, maker_pays_fixed, fixed_rate, cap, notional, start, period_s, n_periods, taker_only)` payable | maker | locks the margin, returns the swap id; `taker_only` "" or an address |
| `accept(swap_id)` payable | taker | locks the same margin, before start |
| `cancel(swap_id)` | maker | refund while nobody has accepted |
| `settle(swap_id)` | anyone | settles the next closed period, pays out after the last one |
| `request_close(swap_id)` | either side | when both have asked, the swap ends after the running period |
| `void(swap_id)` | anyone | skips a period that still has no fixing 30 days after it ended |
| `fix(asset, from_ts, to_ts)` | anyone | records the fixing for a closed window (8h boundaries, 8h to 7 days) |
| `get_swap(swap_id)` / `get_period(swap_id, k)` / `get_fixing(asset, from, to)` | view | state, one settled period, one stored fixing |
| `quote_margin(notional, cap, period_s, n_periods)` | view | the margin each side must send |
| `get_count()` | view | number of swaps |

A fixing (`fix`'s return value, and what `get_fixing` stores):

```json
{"asset": "BTC", "from": 1791936000, "to": 1792022400, "median": "170451515011700",
 "used": ["binance", "gate", "hyperliquid", "kucoin", "okx"],
 "venues": {"binance": {"n": 3, "sum": "-43950000000000"}, "okx": {"n": 3, "sum": "170451515011700"},
            "gate": {"n": 3, "sum": "135000000000000"}, "kucoin": {"n": 3, "sum": "205000000000000"},
            "hyperliquid": {"n": 24, "sum": "278800500000000"}}}
```

Sums and the median are in 1e-18 of notional (1 bp = 1e14). A venue left out shows `{"miss": "<reason>", "transient": true|false}`.

Deploy and run:

```bash
npm install
node scripts/deploy.mjs contracts/funding_rate_swap.py      # DEPLOYER_PK in .env, created and funded if missing
node scripts/live_demo.mjs <contract address>
python scripts/capture_fixture.py btc_1014 BTC 2026-10-14T00:00 2026-10-15T00:00
py -3.12 glcheck.py manifest deployments.json               # the deployed source is this file
```

## Tests

```bash
py -3.12 -m venv .venv && .venv/Scripts/pip install genlayer-test
.venv/Scripts/python -X utf8 -m pytest tests -q
```

18 direct-mode tests on **real venue answers** saved in `tests/fixtures/` (fetched 2026-11-04 with the contract's exact URLs: BTC on 10-10, 10-11, 10-14, the week of 10-05, and each venue's real answer for an unlisted symbol). Expected values come from a separate parser in the test file (Python `Decimal`), not from the contract.
- **Fixings:** the 10-14 median is OKX's number with Binance negative and Hyperliquid at 2.79 bp. A negative day. The 7-day window (21 and 168 prints). Fixings are stored and reused without new requests. Window rules.
- **False leaders:** a different median; OKX moved by 1e-18 with the median moved to match; Binance dropped (moves the median 0.17 bp, rejected); a Hyperliquid count of 23; another day's window; malformed. All rejected.
- **Partial views:** a validator that gets Binance's real 451 still agrees. A validator that can't see KuCoin can be fooled by a leader inventing a KuCoin number, while one that sees KuCoin rejects it (this is a known limit, tested on purpose). A validator that reaches only 2 venues never agrees.
- **Venue trouble:** Binance down for everyone → median of the other four. A Hyperliquid answer missing one hour → that venue is left out. Three venues down → `[TRANSIENT]`, nothing stored. Unlisted asset → `[EXTERNAL]`. Error agreement: same external error agrees, a leader error when the validator gets data disagrees, transient matches transient.
- **Swaps:** input rules, reserved taker, exact margin, no accept after start. Settlement on 10-14 (maker pays about 1.30 GEN). Cap reached (the whole 3 GEN margin moves, no more). Two periods with the payer flipping as funding turns negative. Cancel, mutual close mid-term and before start, void only after 30 days. Rate parser on every venue's number format.

Replacing the union-median check or the "checked at least 3 venues" check with `return True` makes a test fail; I checked both.

On Windows, `tests/conftest.py` patches a temp-file unlink in gltest's loader.

## Limits

- **Venues a validator can't see are taken on trust.** If a validator can't reach a venue, it uses the leader's number for it. A leader could invent that number, and the validators that do reach the venue reject it. If most validators lose the same venue at once, a false number for it could pass. Requiring each validator to check at least 3 venues limits this but doesn't remove it.
- **The cap is a collar.** Funding moves past the cap are not paid. That's what makes default impossible without liquidations, but in a funding spike the hedge stops at the cap.
- **8h boundaries only.** Periods start and end on 00/08/16 UTC so all five venues line up. If a venue switches an asset to a 4h interval, its print count no longer matches and it drops out of the median until the contract is updated.
- **History windows.** OKX serves about 3 months of funding history. A period settled later loses OKX and uses the other four. Periods are at most 7 days so each venue answers in one page.
- **USDT perps plus Hyperliquid.** The symbol mapping is `<ASSET>USDT`, `<ASSET>-USDT-SWAP`, `<ASSET>_USDT`, KuCoin's `XBT` for BTC, and Hyperliquid's coin name. Assets with other names on some venues will be missing there.
- **Settlement waits for someone to call it.** Either side (or a keeper) has to call `settle`. The side that's owed money has the reason to. A period nobody can fix for 30 days can be voided by anyone, and then nobody is paid for it.
- **Notional is in GEN.** The swap pays in the chain's native token. There is no price conversion; the notional is whatever amount of GEN the two sides agree represents their position.
