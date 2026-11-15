# FundingRateSwap

A fixed-for-floating swap on perpetual-futures funding. Two traders agree on a fixed funding rate for an asset (say BTC, 0.01% per 8 hours) over a term. After each period, GenLayer validators pull the public funding history of five venues (Binance, OKX, Gate, KuCoin, Hyperliquid), add up what each venue actually paid in code, and take the median. The difference to the fixed rate moves from one trader's margin to the other's. Margin is posted up front and covers the worst case, so nobody can default.

## Why I built this

Most of my crypto positions are perps. Funding is the cost I can't plan. A long can bleed 3 bp a day for a month and then get paid for a week, and the carry on a basis trade (long spot, short perp) is only known after the fact. In rates markets this risk has a standard answer: you swap the floating rate for a fixed one. For perp funding there's no neutral place to do that.

The moment you try to write such a swap between two people, the hard part is the reference rate:
- **Which venue?** On 2026-10-14, BTC funding paid **-0.44 bp** on Binance and **+2.79 bp** on Hyperliquid for the same UTC day. Whoever picks the venue picks the result.
- **Venues line up badly.** Binance, OKX, Gate and KuCoin pay every 8 hours, Hyperliquid every hour. Binance and Hyperliquid stamp prints a few milliseconds after the hour, Gate 2 seconds after. A naive "between start and end" query silently drops the last print.
- **Venues go down or block you.** From Studionet's validators, `fapi.binance.com` answers 451 and Bybit answers 403 "Forbidden for your country".
- **Settlement needs trust.** Whoever computes the number and moves the money can lie about it.

Funding history is public and final once paid, so none of this needs a judgment call. It needs a rule both sides sign up to in advance, and several independent parties that each fetch the data and check the arithmetic. That's what this contract is.

There's no LLM in it. Every input is a published number, so the code decides everything. GenLayer's part is that each validator fetches the five venues itself and only signs off on a median it computes from its own fetches.

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

Settled funding prints don't change, so a venue's sum is the same integer on every node that reaches it. What can differ between nodes is which venues answer. A fixing is stored when a **majority** of validators agree with the leader (not every validator). A validator agrees only if:
1. The leader's own numbers add up: its median is the median of the venues it lists, there are at least 3, and each has the full number of prints for the window.
2. **The validator fetched every venue the leader used, and got exactly the same sum.** It never accepts a venue number it did not see itself.
3. **The median of all the venues the validator fetched itself equals the leader's median.** So a leader can't drop an inconvenient venue either: if the validator has it, its median differs and it disagrees.

Together, every validator that agrees has computed the stored median from its own data. The margin transfer in `settle` is a pure function of that median and the swap's terms, so an accepted fixing implies the same transfer no matter which venues some other validator could not reach. A validator that can't reach a venue the leader used disagrees. If too few agree, nothing is stored and the fixing (or `settle`) can be called again.

Errors are tagged. `[EXTERNAL]` (no venue lists the asset, or too few have complete data) must match the validator's own error word for word. `[TRANSIENT]` (venues not answering) only needs the validator to also hit a transient failure. `[EXPECTED]` covers input errors. Nothing is recorded on an error, and the call can be repeated.

## Fix (2026-11-15): validators no longer trust venue numbers they didn't fetch

**What was wrong.** The first version (deployed at `0x19d9723C681C74C755bEf8413F5a9E6134731B4E`) let a validator that could not reach a venue fill that venue in with the leader's number, and then compare medians over the union. A leader could report a false sum for a venue some validators couldn't reach; if that number moved the median, those validators still agreed, and the false median would change the margin transfer. The README listed this as a known limit and a test asserted it on purpose. A reviewer rejected the contract for it, rightly: an accepted fixing should never depend on a payout-driving number a validator did not observe.

**What changed.** In `fixing_ok`, the union step and the "checked at least 3 venues" step are replaced by the two rules above: every venue the leader used must be one the validator fetched itself with the identical sum, and the validator's own median must equal the leader's median. (At least 3 venues follows, since the leader must use 3.) Nothing else in the contract changed, and the module docstring now describes the real rule, including that consensus is a majority of validators.

**Why this rule and not a "worst case" check.** I also considered letting a validator agree when the leader's unobserved venue can't move the median whatever its value. With five venues that almost never holds (the median would have to be pinned by the validator's own values), so it would add code without adding agreements. Requiring the validator to have seen every venue the leader used is simpler and gives the guarantee directly.

**Cost.** Liveness, not safety. If a venue is down for some validators, those validators disagree; the fixing is stored only if a majority saw the same venues as the leader. Otherwise the transaction records nothing and can be repeated (or the leader rotates). A venue down for everyone still drops out the same way as before and the other venues are used.

**Tests that show it** (all three fail on the old source and pass on the new; I ran both):
- `test_validator_that_cannot_reach_a_venue`: the reviewer's case. A validator gets no KuCoin answer and the leader reports an invented KuCoin sum that moves the 2026-10-14 median: the old rule agreed, the new one refuses. It also refuses an invented KuCoin number that lands exactly on the validator's own four-venue median (same median, but the stored KuCoin sum would be unchecked), and a validator behind Binance's real 451 refuses the honest five-venue leader.
- `test_no_unobserved_venue_can_move_an_accepted_fixing`: for each of the five venues missing on the validator, a leader number for it moved down 50 bp, up 50 bp or left honest: always refused. With the venue missing for the leader too, the four-venue median is accepted and a median off by 1e-18 is refused.
- `test_settlement_does_not_depend_on_which_venues_a_validator_reached`: a swap settles on 2026-10-14 with Hyperliquid down for the leader. A validator with the same four venues agrees, one that also reaches Hyperliquid disagrees, and one that additionally misses Binance disagrees (the old rule agreed there).

The validator logic was exercised only in direct-mode tests with saved real venue answers. On Studionet I can't make a validator lose a venue on purpose, so the live run below shows the fixed contract working with normal reachability, not the attack.

## Live run on Studionet (2026-11-15, fixed contract)

Contract [`0x89267EDBBB5d942eF450896BE938F337045003d7`](https://explorer-studio.genlayer.com/address/0x89267EDBBB5d942eF450896BE938F337045003d7), deployed from the current `contracts/funding_rate_swap.py` (deploy tx `0xe1fb994564b066bc87b3dab437571e0024894e9ac2562a0387f43ce4d24779f8`). `deployments.json` lists only this address; `glcheck manifest deployments.json` reports the deployed source matches this file. `scripts/live_demo.mjs` wrote everything to `demo_result.json`.

Every transaction is `ACCEPTED` with leader `SUCCESS`; validators that voted all voted AGREE (three AGREE and two IDLE on most transactions, five AGREE on one accept). The NOPE row is the expected leader error, and the validators agreed with the error.

| Window | Binance | OKX | Gate | KuCoin | Hyperliquid | Median | fix tx |
|---|---|---|---|---|---|---|---|
| BTC 2026-10-14 | -0.4395 | **1.7045** | 1.35 | 2.05 | 2.788 | **1.7045** | `0x22f8da6f...` |
| BTC 2026-10-11 | 0.091 | -0.2599 | **-0.21** | 0.71 | -0.4047 | **-0.21** | `0x3876b38c...` |
| BTC 2026-10-05 to 10-12 | 1.0492 | **4.5836** | 1.78 | 10.18 | 13.0003 | **4.5836** | `0x76ef27a5...` |
| ETH 2026-11-03 | 2.8833 | **2.7996** | 2.49 | 2.26 | 3.00 | **2.7996** | `0x697b6d22...` |
| NOPE | | | | | | leader `ERROR` `[EXTERNAL] only 0 venues have complete NOPE funding ... (none)`, validators AGREE | `0x0d94f6ae...` |

(bp of notional for the whole window.) All five venues answered the leader and the agreeing validators in every fixing, so each agreeing validator had fetched all five itself. The medians are the same integers as in the first run on 2026-11-04, as they should be: settled funding history doesn't change.

**Swaps** (BTC, 10,000 GEN notional, 0.01% cap, one 8h period from 2026-11-15 08:00 UTC, 1 GEN margin per side):

| Swap | Steps | Result |
|---|---|---|
| #0 maker pays 0.01% fixed | propose `0xed504145...`, accept `0xcbc976ea...`, settle `0xdbd08b77...` after the period (2026-11-15 16:11 UTC) | floating **0.04 bp** vs fixed 1 bp: maker paid taker **0.96 GEN**, `closed`, margins paid out (maker 0.04, taker 1.96 GEN) |
| #1 maker receives 0.008% fixed | propose `0xc9d5c6a5...`, cancel `0xa5d8a2f4...` | `cancelled`, maker refunded |
| #2 maker receives 0.008% fixed | propose `0xb760a829...`, accept `0x437d3dba...`, request_close by both (`0x0f0782e2...`, `0xf7349655...`) | `closed`, `end_period` 0, both refunded |

The settlement fixing for 2026-11-15 08:00 to 16:00 UTC (BTC, bp): Binance -0.0247, OKX 0.1178, Gate -0.06, KuCoin **0.04**, Hyperliquid 0.9839, median 0.04 (KuCoin). The settle transaction was ACCEPTED, leader SUCCESS, votes AGREE x3 / IDLE x2, all five venues complete. Nobody had fixed that window before (`fixing_cached` false), so the validators fetched it live inside `settle`, and under the new rule each agreeing validator had fetched all five venues itself.

## Earlier live run (2026-11-04, first version, replaced)

This run used the first version of the contract, with the validator rule that was later rejected (see the fix above). It is kept as history; the address is no longer listed in `deployments.json`.

Contract [`0x19d9723C681C74C755bEf8413F5a9E6134731B4E`](https://explorer-studio.genlayer.com/address/0x19d9723C681C74C755bEf8413F5a9E6134731B4E), deployed with my own key through `genlayer-js` (`scripts/deploy.mjs`). `scripts/live_demo.mjs` wrote everything to what is now `demo_result_2026-11-05_0x19d9.json`. Every transaction below is `ACCEPTED` with leader `SUCCESS` and every validator that voted AGREE (the rest IDLE), except the NOPE row, where the leader correctly errored and the validators agreed with the error.

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
| #0 maker pays 0.01% fixed | propose, accept, settle after the period (2026-11-05 00:11 UTC) | floating **0.2405 bp** vs fixed 1 bp: maker paid taker **0.7595 GEN**, `closed`, margins paid out (maker 0.2405, taker 1.7595 GEN) |
| #1 maker receives 0.008% fixed | propose, cancel | `cancelled`, maker refunded |
| #2 maker receives 0.008% fixed | propose, accept, request_close by both before start | `closed`, `end_period` 0, both refunded |

The settlement fixing for 2026-11-04 16:00 to 11-05 00:00 UTC (BTC, bp): Binance **0.24**, OKX 0.75, Gate 0.80, KuCoin 0.18, Hyperliquid -0.25, median 0.2405 (Binance). The settle transaction (`0xf088396d...`) was ACCEPTED, leader SUCCESS, votes AGREE x3 / IDLE x2, all five venues complete. Nobody had fixed that window before, so the validators fetched it live inside `settle`.

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

20 direct-mode tests on **real venue answers** saved in `tests/fixtures/` (fetched 2026-11-04 with the contract's exact URLs: BTC on 10-10, 10-11, 10-14, the week of 10-05, and each venue's real answer for an unlisted symbol). Expected values come from a separate parser in the test file (Python `Decimal`), not from the contract.
- **Fixings:** the 10-14 median is OKX's number with Binance negative and Hyperliquid at 2.79 bp. A negative day. The 7-day window (21 and 168 prints). Fixings are stored and reused without new requests. Window rules.
- **False leaders:** a different median; OKX moved by 1e-18 with the median moved to match; Binance dropped (moves the median 0.17 bp, rejected); a Hyperliquid count of 23; another day's window; malformed. All rejected.
- **Partial views (2026-11-15 fix):** a validator that can't reach a venue the leader used refuses, whether the leader's number for it is invented (moving the median or not) or honest; this includes Binance's real 451 answer. A validator that sees every venue accepts the honest leader and rejects the invented one. A validator that reaches only 2 venues never agrees. For each of the five venues missing on the validator, no leader number for it is accepted. A settlement fixed with Hyperliquid down is accepted only by validators that saw the same four venues.
- **Venue trouble:** Binance down for everyone → median of the other four. A Hyperliquid answer missing one hour → that venue is left out. Three venues down → `[TRANSIENT]`, nothing stored. Unlisted asset → `[EXTERNAL]`. Error agreement: same external error agrees, a leader error when the validator gets data disagrees, transient matches transient.
- **Swaps:** input rules, reserved taker, exact margin, no accept after start. Settlement on 10-14 (maker pays about 1.30 GEN). Cap reached (the whole 3 GEN margin moves, no more). Two periods with the payer flipping as funding turns negative. Cancel, mutual close mid-term and before start, void only after 30 days. Rate parser on every venue's number format.

Weakening either validator rule makes a test fail; I checked both: accepting a leader venue the validator didn't fetch (checking only venues both saw), and replacing the own-median comparison with `return True`.

On Windows, `tests/conftest.py` patches a temp-file unlink in gltest's loader.

## Limits

- **Partial outages cost liveness.** Since 2026-11-15 a validator never takes a venue number on trust (see the fix above). The price: if a venue answers some validators and not others, the fixing needs a majority that saw the same venues as the leader, and otherwise records nothing until a later call succeeds. If a venue is down for the leader and a majority, the median of the remaining venues is stored, as it would be if the venue were down for everyone.
- **The cap is a collar.** Funding moves past the cap are not paid. That's what makes default impossible without liquidations, but in a funding spike the hedge stops at the cap.
- **8h boundaries only.** Periods start and end on 00/08/16 UTC so all five venues line up. If a venue switches an asset to a 4h interval, its print count no longer matches and it drops out of the median until the contract is updated.
- **History windows.** OKX serves about 3 months of funding history. A period settled later loses OKX and uses the other four. Periods are at most 7 days so each venue answers in one page.
- **USDT perps plus Hyperliquid.** The symbol mapping is `<ASSET>USDT`, `<ASSET>-USDT-SWAP`, `<ASSET>_USDT`, KuCoin's `XBT` for BTC, and Hyperliquid's coin name. Assets with other names on some venues will be missing there.
- **Settlement waits for someone to call it.** Either side (or a keeper) has to call `settle`. The side that's owed money has the reason to. A period nobody can fix for 30 days can be voided by anyone, and then nobody is paid for it.
- **Notional is in GEN.** The swap pays in the chain's native token. There is no price conversion; the notional is whatever amount of GEN the two sides agree represents their position.
