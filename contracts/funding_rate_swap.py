# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
FundingRateSwap: two traders swap a fixed perpetual-futures funding rate for the
funding that is actually paid, settled on a median across five venues.

Anyone holding a perp pays or receives funding every few hours, and nobody knows
the rate in advance. A long who wants a known carrying cost pays fixed and
receives floating; a basis trader who wants to lock in what the shorts earn does
the opposite. Here:

  propose    the maker locks margin and fixes the asset, which side of the fixed
             rate it is on, the fixed rate per 8 hours, a cap on how far the
             floating leg can move away from it per 8 hours, the notional, the
             start (a funding boundary: 00/08/16 UTC), the period length and the
             number of periods. Optionally names the only address that may accept.
  accept     the taker locks the same margin before the start.
  settle     anyone, once a period has ended. Validators fetch the public funding
             history of Binance, OKX, Gate, KuCoin and Hyperliquid, sum each
             venue's prints inside the period in code, and take the median of
             the venues with complete data (at least three). The difference to
             the fixed leg, clamped to the cap, moves from one margin to the other.
             After the last period both margins are paid out.
  fix        anyone: the same median for any closed past window. Fixings are
             cached, so swaps and readers share one number per window.
  request_close  both sides together end the swap after the running period.
  void       a period nobody could fix for 30 days is skipped without payment.

Margin per side is notional x cap x (number of 8h intervals), so every payment is
covered in full and neither side can default.

No LLM is involved: funding history is published numbers. Settled funding prints
never change, so every validator computes the same sum for a venue and compares
it exactly. What can differ between nodes is which venues answer at all, so a
validator accepts the leader only if every venue they both saw matches exactly,
it checked at least three venues itself, and the median over everything it can
see (plus what only the leader saw) is the leader's median.
"""
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

VENUES = ("binance", "okx", "gate", "kucoin", "hyperliquid")
INTERVAL_S = {"binance": 28_800, "okx": 28_800, "gate": 28_800, "kucoin": 28_800, "hyperliquid": 3_600}
MIN_VENUES = 3
BASE_S = 28_800                    # periods start and end on 8h funding boundaries
MAX_PERIOD_S = 7 * 86_400          # keeps every venue inside one page of history
MAX_PERIODS = 90
MAX_START_DELAY_S = 30 * 86_400
SETTLE_DELAY_S = 600               # venues publish a print within seconds; leave room
VOID_AFTER_S = 30 * 86_400
WAD = 10**18
MAX_RATE = 10**16                  # 1% per 8h, fixed rate and cap alike

ASSET_RE = re.compile(r"^[A-Z0-9]{2,10}$")
DEC_RE = re.compile(r"^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d{1,3}))?$")
ZERO = "0x" + "00" * 20

ERR_EXPECTED = "[EXPECTED]"
ERR_EXTERNAL = "[EXTERNAL]"
ERR_TRANSIENT = "[TRANSIENT]"


@gl.evm.contract_interface
class _Recipient:
    class View:
        pass

    class Write:
        pass


@allow_storage
@dataclass
class Swap:
    maker: Address
    taker: Address           # zero until accepted
    taker_only: str          # "" = anyone may accept
    maker_pays_fixed: bool
    asset: str
    fixed: str               # wad per 8h, signed
    cap: str                 # wad per 8h
    notional: u256
    start: u256
    period_s: u256
    n_periods: u256
    end_period: u256         # n_periods, or earlier after a mutual close
    margin: u256             # per side
    bal_maker: u256
    bal_taker: u256
    settled: u256            # periods settled or voided
    status: str              # open | active | closed | cancelled
    close_votes: str         # "", "maker", "taker", "maker,taker"


# ---------- deterministic helpers ----------

def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def to_wad(s: str) -> int:
    """'0.0000954849645099' / '-2.2E-5' -> integer in 1e-18 units, exact up to 18
    decimals, truncated toward zero beyond that."""
    m = DEC_RE.match(str(s).strip())
    if not m:
        raise ValueError(f"not a decimal: {s!r}")
    sign, whole, frac, exp = m.group(1), m.group(2), m.group(3) or "", int(m.group(4) or 0)
    digits, shift = int(whole + frac), 18 + exp - len(frac)
    v = digits * 10**shift if shift >= 0 else digits // 10**(-shift)
    return -v if sign == "-" else v


def median(vals: list) -> int:
    xs = sorted(vals)
    k = len(xs)
    return xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) // 2


def _minute(ms) -> int:
    """Venues stamp prints a few ms (Binance, Hyperliquid) or 2 s (Gate) after the hour."""
    return (int(ms) + 30_000) // 60_000 * 60


def venue_request(venue: str, asset: str, ps: int, pe: int):
    """(method, url, body). The window is shifted by a minute so a print stamped
    just after pe is inside and one stamped just after ps is outside."""
    a, b = (ps + 60) * 1000, (pe + 60) * 1000
    if venue == "binance":
        # fapi.binance.com answers 451 from Studionet; www.binance.com serves the same API
        return "GET", f"https://www.binance.com/fapi/v1/fundingRate?symbol={asset}USDT&startTime={a}&endTime={b}&limit=1000", None
    if venue == "okx":
        return "GET", f"https://www.okx.com/api/v5/public/funding-rate-history?instId={asset}-USDT-SWAP&before={a}&after={b}&limit=100", None
    if venue == "gate":
        return "GET", f"https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract={asset}_USDT&from={ps + 60}&to={pe + 60}&limit=1000", None
    if venue == "kucoin":
        kc = "XBT" if asset == "BTC" else asset
        return "GET", f"https://api-futures.kucoin.com/api/v1/contract/funding-rates?symbol={kc}USDTM&from={a}&to={b}", None
    return "POST", "https://api.hyperliquid.xyz/info", json.dumps(
        {"type": "fundingHistory", "coin": asset, "startTime": a, "endTime": b})


class _Miss(Exception):
    def __init__(self, reason: str, transient: bool):
        super().__init__(reason)
        self.reason, self.transient = reason, transient


def parse_prints(venue: str, status: int, text: str) -> list:
    """One venue's raw answer -> [(minute_ts, wad)]. Pure."""
    try:
        data = json.loads(text, parse_float=str)      # keep KuCoin's 1.0E-4 as text
    except Exception:
        raise _Miss(f"HTTP {status}, not JSON", True)
    if venue == "gate" and status == 400 and isinstance(data, dict) and data.get("label") == "CONTRACT_NOT_FOUND":
        raise _Miss("not listed", False)
    if venue == "hyperliquid" and status == 500 and data is None:
        raise _Miss("not listed", False)               # Hyperliquid's answer to an unknown coin
    if status != 200:
        raise _Miss(f"HTTP {status}", True)
    try:
        if venue == "binance":
            if isinstance(data, dict):
                raise _Miss(f"error {data.get('code')}", False)
            rows = [(r["fundingTime"], r["fundingRate"]) for r in data]
        elif venue == "okx":
            if data.get("code") == "51001":
                raise _Miss("not listed", False)
            if data.get("code") != "0":
                raise _Miss(f"error {data.get('code')}", True)
            rows = [(r["fundingTime"], r["realizedRate"]) for r in data["data"]]
        elif venue == "gate":
            rows = [(int(r["t"]) * 1000, r["r"]) for r in data]
        elif venue == "kucoin":
            if data.get("code") == "404000":
                raise _Miss("not listed", False)
            if data.get("code") != "200000":
                raise _Miss(f"error {data.get('code')}", True)
            rows = [(r["timepoint"], r["fundingRate"]) for r in data["data"]]
        else:
            rows = [(r["time"], r["fundingRate"]) for r in data]
        return [(_minute(t), to_wad(r)) for t, r in rows]
    except _Miss:
        raise
    except Exception:
        raise _Miss("unexpected format", False)


def venue_sum(venue: str, prints: list, ps: int, pe: int) -> dict:
    """Sum of the prints inside (ps, pe], only if every funding time is there once."""
    step = INTERVAL_S[venue]
    want = set(range(ps + step, pe + 1, step))
    got = {}
    for t, r in prints:
        if ps < t <= pe:
            if t in got and got[t] != r:
                raise _Miss("two different prints for one time", False)
            got[t] = r
    if set(got) != want:
        raise _Miss(f"incomplete: {len(set(got) & want)}/{len(want)} prints", False)
    return {"sum": str(sum(got.values())), "n": len(want)}


def measure_venue(venue: str, asset: str, ps: int, pe: int) -> dict:
    method, url, body = venue_request(venue, asset, ps, pe)
    try:
        try:
            if method == "GET":
                resp = gl.nondet.web.get(url)
            else:
                resp = gl.nondet.web.post(url, body=body, headers={"content-type": "application/json"})
        except Exception as e:
            raise _Miss(f"request failed: {str(e)[:80]}", True)
        return venue_sum(venue, parse_prints(venue, resp.status, resp.body.decode("utf-8", "replace")), ps, pe)
    except _Miss as m:
        return {"miss": m.reason, "transient": m.transient}


def _values(venues: dict) -> dict:
    return {v: int(x["sum"]) for v, x in venues.items() if isinstance(x, dict) and "sum" in x}


def compute_fixing(asset: str, ps: int, pe: int) -> dict:
    """Leader: every venue's sum for the window, and their median."""
    venues = {v: measure_venue(v, asset, ps, pe) for v in VENUES}
    vals = _values(venues)
    if len(vals) < MIN_VENUES:
        used = ",".join(sorted(vals)) or "none"
        if any(x.get("transient") for x in venues.values() if "miss" in x):
            raise gl.vm.UserError(f"{ERR_TRANSIENT} only {len(vals)} venues answered for {asset} {ps}-{pe}")
        raise gl.vm.UserError(f"{ERR_EXTERNAL} only {len(vals)} venues have complete {asset} funding for {ps}-{pe} ({used})")
    return {"asset": asset, "from": ps, "to": pe, "venues": venues,
            "used": sorted(vals), "median": str(median(list(vals.values())))}


def fixing_ok(leader, mine: dict, asset: str, ps: int, pe: int) -> bool:
    """Validator: does this node's own view of the venues support the leader's median?"""
    try:
        if (leader["asset"], leader["from"], leader["to"]) != (asset, ps, pe):
            return False
        lv = leader["venues"]
        if set(lv) != set(VENUES):
            return False
        theirs = _values(lv)
        for v in theirs:
            if lv[v]["n"] != (pe - ps) // INTERVAL_S[v]:
                return False
        if leader["used"] != sorted(theirs) or len(theirs) < MIN_VENUES:
            return False
        if int(leader["median"]) != median(list(theirs.values())):
            return False                      # the leader's own numbers don't give its median
    except Exception:
        return False
    ours = _values(mine)
    both = set(ours) & set(theirs)
    if any(ours[v] != theirs[v] for v in both):
        return False                          # settled history is the same for everyone
    if len(both) < MIN_VENUES:
        return False                          # never vouch for a median we mostly couldn't check
    # a venue the leader left out but we have still counts: dropping data can't move the median
    union = dict(theirs)
    union.update(ours)
    return median(list(union.values())) == int(leader["median"])


def _errors_agree(leader_res, leader_fn) -> bool:
    leader_msg = getattr(leader_res, "message", "")
    try:
        leader_fn()
        return False
    except gl.vm.UserError as e:
        mine = getattr(e, "message", str(e))
        if mine.startswith(ERR_EXPECTED) or mine.startswith(ERR_EXTERNAL):
            return mine == leader_msg
        return mine.startswith(ERR_TRANSIENT) and leader_msg.startswith(ERR_TRANSIENT)
    except Exception:
        return False


def period_payment(floating: int, fixed: int, cap: int, k8: int, notional: int) -> tuple:
    """(diff, capped, amount). diff > 0: floating above fixed, the fixed payer receives."""
    lim = cap * k8
    diff = floating - fixed * k8
    capped = abs(diff) > lim
    diff = max(-lim, min(lim, diff))
    return diff, capped, notional * abs(diff) // WAD


def margin_for(notional: int, cap: int, period_s: int, n_periods: int) -> int:
    per_period = -(-notional * cap * (period_s // BASE_S) // WAD)     # rounded up
    return per_period * n_periods


def _check_window(ps: int, pe: int):
    if ps % BASE_S or pe % BASE_S:
        raise gl.vm.UserError(f"{ERR_EXPECTED} windows start and end on 00/08/16 UTC")
    if not (BASE_S <= pe - ps <= MAX_PERIOD_S):
        raise gl.vm.UserError(f"{ERR_EXPECTED} a window is 8 hours to 7 days")


def _rate(s: str, what: str) -> int:
    try:
        return to_wad(s)
    except Exception:
        raise gl.vm.UserError(f"{ERR_EXPECTED} {what} must be a decimal like 0.0001")


class FundingRateSwap(gl.Contract):
    swaps: TreeMap[u256, Swap]
    swap_count: u256
    periods: TreeMap[str, str]       # "<swap>:<k>" -> JSON of the settlement
    fixings: TreeMap[str, str]       # "<asset>:<from>:<to>" -> JSON of the fixing

    def __init__(self):
        self.swap_count = u256(0)

    # ---------- fixings ----------

    def _fixing(self, asset: str, ps: int, pe: int) -> tuple:
        key = f"{asset}:{ps}:{pe}"
        hit = self.fixings.get(key, "")
        if hit:
            return json.loads(hit), True

        def leader_fn() -> dict:
            return compute_fixing(asset, ps, pe)

        def validator_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return _errors_agree(leader_res, leader_fn)
            mine = {v: measure_venue(v, asset, ps, pe) for v in VENUES}
            return fixing_ok(leader_res.calldata, mine, asset, ps, pe)

        obs = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        self.fixings[key] = json.dumps(obs, sort_keys=True)
        return obs, False

    @gl.public.write
    def fix(self, asset: str, from_ts: int, to_ts: int) -> dict:
        if not ASSET_RE.match(asset):
            raise gl.vm.UserError(f"{ERR_EXPECTED} asset looks like BTC")
        _check_window(from_ts, to_ts)
        if _now() < to_ts + SETTLE_DELAY_S:
            raise gl.vm.UserError(f"{ERR_EXPECTED} window has not closed")
        obs, cached = self._fixing(asset, from_ts, to_ts)
        return dict(obs, cached=cached)

    # ---------- maker / taker ----------

    @gl.public.write.payable
    def propose(self, asset: str, maker_pays_fixed: bool, fixed_rate: str, cap: str, notional: int,
                start: int, period_s: int, n_periods: int, taker_only: str) -> int:
        if not ASSET_RE.match(asset):
            raise gl.vm.UserError(f"{ERR_EXPECTED} asset looks like BTC")
        fixed, cap_w = _rate(fixed_rate, "fixed_rate"), _rate(cap, "cap")
        if abs(fixed) > MAX_RATE:
            raise gl.vm.UserError(f"{ERR_EXPECTED} fixed_rate must be within +-1% per 8h")
        if not (0 < cap_w <= MAX_RATE):
            raise gl.vm.UserError(f"{ERR_EXPECTED} cap must be above 0 and at most 1% per 8h")
        now = _now()
        if start % BASE_S or not (now < start <= now + MAX_START_DELAY_S):
            raise gl.vm.UserError(f"{ERR_EXPECTED} start must be a future 00/08/16 UTC boundary within 30 days")
        if period_s % BASE_S or not (BASE_S <= period_s <= MAX_PERIOD_S):
            raise gl.vm.UserError(f"{ERR_EXPECTED} period_s must be a multiple of 8h, at most 7 days")
        if not (1 <= n_periods <= MAX_PERIODS):
            raise gl.vm.UserError(f"{ERR_EXPECTED} n_periods must be 1-{MAX_PERIODS}")
        if notional <= 0:
            raise gl.vm.UserError(f"{ERR_EXPECTED} notional must be positive")
        if taker_only and not re.match(r"^0x[0-9a-fA-F]{40}$", taker_only):
            raise gl.vm.UserError(f"{ERR_EXPECTED} taker_only must be a 0x address or empty")
        if taker_only and Address(taker_only) == gl.message.sender_address:
            raise gl.vm.UserError(f"{ERR_EXPECTED} maker and taker must differ")
        margin = margin_for(notional, cap_w, period_s, n_periods)
        if margin == 0 or int(gl.message.value) != margin:
            raise gl.vm.UserError(f"{ERR_EXPECTED} send exactly the margin: {margin}")
        sid = int(self.swap_count)
        self.swaps[u256(sid)] = Swap(
            maker=gl.message.sender_address, taker=Address(ZERO),
            taker_only=Address(taker_only).as_hex if taker_only else "",
            maker_pays_fixed=maker_pays_fixed, asset=asset, fixed=str(fixed), cap=str(cap_w),
            notional=u256(notional), start=u256(start), period_s=u256(period_s),
            n_periods=u256(n_periods), end_period=u256(n_periods), margin=u256(margin),
            bal_maker=u256(margin), bal_taker=u256(0), settled=u256(0), status="open", close_votes="",
        )
        self.swap_count = u256(sid + 1)
        return sid

    @gl.public.write.payable
    def accept(self, swap_id: int) -> int:
        s = self._swap(swap_id)
        if s.status != "open":
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is {s.status}")
        if _now() >= int(s.start):
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap has already started")
        me = gl.message.sender_address
        if me == s.maker:
            raise gl.vm.UserError(f"{ERR_EXPECTED} maker and taker must differ")
        if s.taker_only and Address(s.taker_only) != me:
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is reserved for another taker")
        if int(gl.message.value) != int(s.margin):
            raise gl.vm.UserError(f"{ERR_EXPECTED} send exactly the margin: {int(s.margin)}")
        s.taker, s.bal_taker, s.status = me, s.margin, "active"
        return int(s.margin)

    @gl.public.write
    def cancel(self, swap_id: int) -> int:
        s = self._swap(swap_id)
        if gl.message.sender_address != s.maker:
            raise gl.vm.UserError(f"{ERR_EXPECTED} only the maker can cancel")
        if s.status != "open":
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is {s.status}")
        s.status = "cancelled"
        amount = int(s.bal_maker)
        s.bal_maker = u256(0)
        _Recipient(s.maker).emit_transfer(value=u256(amount))
        return amount

    @gl.public.write
    def request_close(self, swap_id: int) -> dict:
        s = self._swap(swap_id)
        me = gl.message.sender_address
        side = "maker" if me == s.maker else "taker" if me == s.taker else ""
        if not side:
            raise gl.vm.UserError(f"{ERR_EXPECTED} only the two sides can close")
        if s.status != "active":
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is {s.status}")
        votes = set(x for x in s.close_votes.split(",") if x) | {side}
        s.close_votes = ",".join(sorted(votes))
        if votes == {"maker", "taker"}:
            now, start, p = _now(), int(s.start), int(s.period_s)
            begun = 0 if now < start else (now - start) // p + 1      # the running period still counts
            s.end_period = u256(min(int(s.end_period), max(begun, int(s.settled))))
            self._maybe_finish(s)
        return {"close_votes": s.close_votes, "end_period": int(s.end_period), "status": s.status}

    # ---------- anyone ----------

    @gl.public.write
    def settle(self, swap_id: int) -> dict:
        s = self._swap(swap_id)
        if s.status != "active":
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is {s.status}")
        k = int(s.settled)
        ps = int(s.start) + k * int(s.period_s)
        pe = ps + int(s.period_s)
        if _now() < pe + SETTLE_DELAY_S:
            raise gl.vm.UserError(f"{ERR_EXPECTED} period {k} has not closed")
        obs, cached = self._fixing(s.asset, ps, pe)
        k8 = int(s.period_s) // BASE_S
        diff, capped, amount = period_payment(int(obs["median"]), int(s.fixed), int(s.cap), k8, int(s.notional))
        payer_is_maker = (diff > 0) != s.maker_pays_fixed       # who loses this period
        if amount:
            if payer_is_maker:
                amount = min(amount, int(s.bal_maker))
                s.bal_maker, s.bal_taker = u256(int(s.bal_maker) - amount), u256(int(s.bal_taker) + amount)
            else:
                amount = min(amount, int(s.bal_taker))
                s.bal_taker, s.bal_maker = u256(int(s.bal_taker) - amount), u256(int(s.bal_maker) + amount)
        rec = {"period": k, "from": ps, "to": pe, "floating": obs["median"], "fixed": str(int(s.fixed) * k8),
               "diff": str(diff), "capped": capped, "paid": amount,
               "payer": ("maker" if payer_is_maker else "taker") if amount else "", "used": obs["used"],
               "fixing_cached": cached}
        self.periods[f"{swap_id}:{k}"] = json.dumps(rec, sort_keys=True)
        s.settled = u256(k + 1)
        self._maybe_finish(s)
        return dict(rec, status=s.status)

    @gl.public.write
    def void(self, swap_id: int) -> dict:
        s = self._swap(swap_id)
        if s.status != "active":
            raise gl.vm.UserError(f"{ERR_EXPECTED} swap is {s.status}")
        k = int(s.settled)
        pe = int(s.start) + (k + 1) * int(s.period_s)
        if _now() < pe + VOID_AFTER_S:
            raise gl.vm.UserError(f"{ERR_EXPECTED} period {k} can be voided 30 days after it ends")
        rec = {"period": k, "from": pe - int(s.period_s), "to": pe, "void": True, "paid": 0}
        self.periods[f"{swap_id}:{k}"] = json.dumps(rec, sort_keys=True)
        s.settled = u256(k + 1)
        self._maybe_finish(s)
        return dict(rec, status=s.status)

    def _maybe_finish(self, s: Swap):
        if s.status != "active" or int(s.settled) < int(s.end_period):
            return
        s.status = "closed"
        a, b = int(s.bal_maker), int(s.bal_taker)
        s.bal_maker, s.bal_taker = u256(0), u256(0)
        if a:
            _Recipient(s.maker).emit_transfer(value=u256(a))
        if b:
            _Recipient(s.taker).emit_transfer(value=u256(b))

    # ---------- views ----------

    @gl.public.view
    def get_swap(self, swap_id: int) -> dict:
        s = self._swap(swap_id)
        return {"maker": s.maker.as_hex, "taker": "" if s.taker.as_hex == Address(ZERO).as_hex else s.taker.as_hex,
                "taker_only": s.taker_only, "maker_pays_fixed": s.maker_pays_fixed, "asset": s.asset,
                "fixed": s.fixed, "cap": s.cap, "notional": int(s.notional), "start": int(s.start),
                "period_s": int(s.period_s), "n_periods": int(s.n_periods), "end_period": int(s.end_period),
                "margin": int(s.margin), "bal_maker": int(s.bal_maker), "bal_taker": int(s.bal_taker),
                "settled": int(s.settled), "status": s.status, "close_votes": s.close_votes}

    @gl.public.view
    def get_period(self, swap_id: int, k: int) -> dict:
        self._swap(swap_id)
        raw = self.periods.get(f"{swap_id}:{k}", "")
        return json.loads(raw) if raw else {}

    @gl.public.view
    def get_fixing(self, asset: str, from_ts: int, to_ts: int) -> dict:
        raw = self.fixings.get(f"{asset}:{from_ts}:{to_ts}", "")
        return json.loads(raw) if raw else {}

    @gl.public.view
    def quote_margin(self, notional: int, cap: str, period_s: int, n_periods: int) -> int:
        return margin_for(notional, _rate(cap, "cap"), period_s, n_periods)

    @gl.public.view
    def get_count(self) -> int:
        return int(self.swap_count)

    def _swap(self, sid: int) -> Swap:
        if not (0 <= sid < int(self.swap_count)):
            raise gl.vm.UserError(f"{ERR_EXPECTED} unknown swap")
        return self.swaps[u256(sid)]
