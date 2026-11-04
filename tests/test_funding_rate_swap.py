import json
from decimal import Decimal
from pathlib import Path

CONTRACT = "contracts/funding_rate_swap.py"
FIX = Path(__file__).parent / "fixtures"
GEN = 10**18
WAD = 10**18
BP = 10**14                     # one basis point in wad

# Real answers of the five venues, fetched on 2026-11-04 with the exact URLs the
# contract builds (scripts/capture_fixture.py):
#   btc_1010, btc_1011, btc_1014   one UTC day each, BTC perps
#   btc_week                       2026-10-05 00:00 to 10-12 00:00 (Hyperliquid: 168 hourly prints)
#   nope                           an asset no venue lists, as each venue really answers it
FX = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in FIX.glob("*.json")}
VENUES = ("binance", "okx", "gate", "kucoin", "hyperliquid")
HOST = {"binance": r"www\.binance\.com", "okx": r"www\.okx\.com", "gate": r"api\.gateio\.ws",
        "kucoin": r"api-futures\.kucoin\.com", "hyperliquid": r"api\.hyperliquid\.xyz"}


# ---------- an independent reading of the fixtures, to check the contract against ----------

def rows(venue, body):
    d = json.loads(body, parse_float=Decimal)
    if venue == "binance":
        return [(r["fundingTime"], Decimal(r["fundingRate"])) for r in d]
    if venue == "okx":
        return [(int(r["fundingTime"]), Decimal(r["realizedRate"])) for r in d["data"]]
    if venue == "gate":
        return [(r["t"] * 1000, Decimal(r["r"])) for r in d]
    if venue == "kucoin":
        return [(r["timepoint"], Decimal(r["fundingRate"])) for r in d["data"]]
    return [(r["time"], Decimal(r["fundingRate"])) for r in d]


def expected(name):
    """{venue: sum in wad} for prints whose hour is in (from, to]."""
    fx = FX[name]
    out = {}
    for v in VENUES:
        tot = Decimal(0)
        for t, r in rows(v, fx[v]["body"]):
            hour = round(t / 60_000) * 60
            if fx["from"] < hour <= fx["to"]:
                tot += r
        out[v] = int(tot * WAD)
    return out


def med(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) // 2


# ---------- mocks ----------

def merged(venue, names):
    """One venue's answer covering several windows. The contract filters by time
    itself, so serving more prints than asked for is fine."""
    bodies = [json.loads(FX[n][venue]["body"]) for n in names]
    if venue in ("okx", "kucoin"):
        out = dict(bodies[0])
        out["data"] = [x for b in bodies for x in b["data"]]
        return json.dumps(out)
    return json.dumps([x for b in bodies for x in b])


def serve(vm, *names, override=None):
    vm.clear_mocks()
    override = override or {}
    for v in VENUES:
        method = "POST" if v == "hyperliquid" else "GET"
        if v in override:
            status, body = override[v]
        elif names == ("nope",):
            status, body = FX["nope"][v]["status"], FX["nope"][v]["body"]
        else:
            status, body = 200, merged(v, names)
        vm.mock_web(HOST[v], {"method": method, "status": status, "body": body})


def hexaddr(a):
    return "0x" + bytes(a).hex()


def at(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def fix(vm, c, name, sender, *names, override=None):
    fx = FX[name]
    vm.warp(at(fx["to"] + 3600))
    serve(vm, *(names or (name,)), override=override)
    vm.sender = sender
    return c.fix("NOPE" if name == "nope" else "BTC", fx["from"], fx["to"])


def propose(vm, c, maker, start, pays_fixed=True, fixed="0.0001", cap="0.0001", notional=10_000 * GEN,
            period_s=86_400, n=1, taker_only=""):
    vm.warp(at(start - 12 * 3600))
    vm.sender = maker
    vm.value = c.quote_margin(notional, cap, period_s, n)
    sid = c.propose("BTC", pays_fixed, fixed, cap, notional, start, period_s, n, taker_only)
    vm.value = 0
    return sid


def accept(vm, c, taker, sid):
    vm.sender = taker
    vm.value = c.get_swap(sid)["margin"]
    c.accept(sid)
    vm.value = 0


# ---------- fixings ----------

def test_fixing_takes_the_median_so_one_venue_cant_move_it(direct_vm, direct_deploy, direct_alice):
    # 2026-10-14: Binance paid -0.44 bp for the day, Hyperliquid +2.79 bp
    c = direct_deploy(CONTRACT)
    out = fix(direct_vm, c, "btc_1014", direct_alice)
    exp = expected("btc_1014")
    assert {v: int(out["venues"][v]["sum"]) for v in VENUES} == exp
    assert exp["binance"] < 0 < 2 * BP < exp["hyperliquid"]
    assert int(out["median"]) == med(exp.values()) == exp["okx"]
    assert out["used"] == sorted(VENUES) and out["cached"] is False
    assert [out["venues"][v]["n"] for v in VENUES] == [3, 3, 3, 3, 24]
    assert direct_vm.run_validator() is True
    # stored, and served from storage the second time without touching the venues
    direct_vm.clear_mocks()
    again = c.fix("BTC", FX["btc_1014"]["from"], FX["btc_1014"]["to"])
    assert again["cached"] is True and again["median"] == out["median"]
    assert c.get_fixing("BTC", FX["btc_1014"]["from"], FX["btc_1014"]["to"])["median"] == out["median"]


def test_negative_funding_day(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    out = fix(direct_vm, c, "btc_1011", direct_alice)
    exp = expected("btc_1011")
    assert int(out["median"]) == med(exp.values()) < 0
    assert direct_vm.run_validator() is True


def test_week_window_lines_up_hourly_and_8h_venues(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    out = fix(direct_vm, c, "btc_week", direct_alice)
    assert [out["venues"][v]["n"] for v in VENUES] == [21, 21, 21, 21, 168]
    assert int(out["median"]) == med(expected("btc_week").values())
    assert direct_vm.run_validator() is True


def test_fix_input_rules(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    f, t = FX["btc_1014"]["from"], FX["btc_1014"]["to"]
    direct_vm.warp(at(t + 300))
    for args, msg in [
        (("btc", f, t), "asset looks like BTC"),
        (("BTC", f + 3600, t), "00/08/16 UTC"),
        (("BTC", f, f + 8 * 86_400), "8 hours to 7 days"),
        (("BTC", f, t), "has not closed"),          # only 5 minutes after the window
    ]:
        with direct_vm.expect_revert(msg):
            c.fix(*args)


# ---------- validators ----------

def test_validator_rejects_a_false_leader(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    honest = fix(direct_vm, c, "btc_1014", direct_alice)
    honest.pop("cached")
    exp = expected("btc_1014")
    assert direct_vm.run_validator(leader_result=honest) is True
    # a different median over the same venue numbers
    assert direct_vm.run_validator(leader_result=dict(honest, median=str(int(honest["median"]) + 1))) is False
    # OKX moved by 1e-18 and the median moved with it: internally consistent, but not what OKX published
    v = json.loads(json.dumps(honest["venues"]))
    v["okx"]["sum"] = str(exp["okx"] + 1)
    assert direct_vm.run_validator(leader_result=dict(honest, venues=v, median=str(exp["okx"] + 1))) is False
    # leaves out Binance, the negative outlier: the median of the other four is 0.17 bp higher
    v = json.loads(json.dumps(honest["venues"]))
    v["binance"] = {"miss": "HTTP 451", "transient": True}
    four = [exp[x] for x in VENUES if x != "binance"]
    dropped = dict(honest, venues=v, used=sorted(set(VENUES) - {"binance"}), median=str(med(four)))
    assert med(four) - exp["okx"] > BP // 10
    assert direct_vm.run_validator(leader_result=dropped) is False
    # claims a short window (Hyperliquid with 23 prints)
    v = json.loads(json.dumps(honest["venues"]))
    v["hyperliquid"]["n"] = 23
    assert direct_vm.run_validator(leader_result=dict(honest, venues=v)) is False
    # a fixing for another day
    assert direct_vm.run_validator(leader_result=dict(honest, **{"from": honest["from"] - 86_400})) is False
    assert direct_vm.run_validator(leader_result={"median": "1"}) is False


def test_validator_that_cannot_reach_a_venue(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    honest = fix(direct_vm, c, "btc_1014", direct_alice)
    honest.pop("cached")
    exp = expected("btc_1014")
    # this validator gets Binance's real geo-block answer: it checks the other four and agrees
    serve(direct_vm, "btc_1014", override={"binance": (451, '{"code":0,"msg":"Service unavailable from a restricted location"}')})
    assert direct_vm.run_validator() is True
    # the known weakness: a leader inventing a KuCoin number fools a validator that can't see KuCoin...
    v = json.loads(json.dumps(honest["venues"]))
    v["kucoin"]["sum"] = str(exp["gate"] - BP)
    fake = dict(honest, venues=v, median=str(med([exp["binance"], exp["okx"], exp["gate"], exp["gate"] - BP, exp["hyperliquid"]])))
    assert int(fake["median"]) != int(honest["median"])
    serve(direct_vm, "btc_1014", override={"kucoin": (503, "")})
    assert direct_vm.run_validator(leader_result=fake) is True
    # ...but every validator that does see KuCoin rejects it
    serve(direct_vm, "btc_1014")
    assert direct_vm.run_validator(leader_result=fake) is False
    # a validator that only reaches two venues never vouches for a median
    serve(direct_vm, "btc_1014", override={"binance": (503, ""), "okx": (503, ""), "gate": (503, "")})
    assert direct_vm.run_validator(leader_result=honest) is False


def test_one_venue_down_for_everyone_falls_back_to_the_other_four(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    out = fix(direct_vm, c, "btc_1014", direct_alice, override={"binance": (451, '{"code":0,"msg":"restricted"}')})
    exp = expected("btc_1014")
    assert out["used"] == ["gate", "hyperliquid", "kucoin", "okx"]
    assert out["venues"]["binance"] == {"miss": "HTTP 451", "transient": True}
    assert int(out["median"]) == med([exp[v] for v in VENUES if v != "binance"])
    assert direct_vm.run_validator() is True


def test_missing_print_excludes_that_venue(direct_vm, direct_deploy, direct_alice):
    # Hyperliquid's answer without its 13:00 print: the venue is incomplete, not "zero for that hour"
    c = direct_deploy(CONTRACT)
    hl = [r for r in json.loads(FX["btc_1014"]["hyperliquid"]["body"])
          if round(r["time"] / 60_000) * 60 != FX["btc_1014"]["from"] + 13 * 3600]
    out = fix(direct_vm, c, "btc_1014", direct_alice, override={"hyperliquid": (200, json.dumps(hl))})
    assert out["venues"]["hyperliquid"] == {"miss": "incomplete: 23/24 prints", "transient": False}
    assert "hyperliquid" not in out["used"]
    assert direct_vm.run_validator() is True


def test_too_few_venues_is_transient_and_records_nothing(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy(CONTRACT)
    down = {"binance": (502, "<html>bad gateway</html>"), "okx": (503, ""), "gate": (429, "{}")}
    with direct_vm.expect_revert("[TRANSIENT] only 2 venues answered"):
        fix(direct_vm, c, "btc_1014", direct_alice, override=down)
    assert c.get_fixing("BTC", FX["btc_1014"]["from"], FX["btc_1014"]["to"]) == {}


def test_unlisted_asset_is_external_and_validators_agree_on_errors(direct_vm, direct_deploy, direct_alice):
    # each venue's real answer for an unknown symbol: Binance [] , OKX 51001, Gate 400,
    # KuCoin 404000, Hyperliquid HTTP 500 "null"
    c = direct_deploy(CONTRACT)
    msg = f"[EXTERNAL] only 0 venues have complete NOPE funding for {FX['nope']['from']}-{FX['nope']['to']} (none)"
    with direct_vm.expect_revert(msg):
        fix(direct_vm, c, "nope", direct_alice)
    # capture a validator on a successful call, then hand it leader errors
    fix(direct_vm, c, "btc_1014", direct_alice)
    serve(direct_vm, "btc_1014")
    assert direct_vm.run_validator(leader_error=Exception("[EXTERNAL] only 0 venues have complete BTC funding")) is False
    assert direct_vm.run_validator(leader_error=Exception("[TRANSIENT] only 2 venues answered")) is False
    serve(direct_vm, "btc_1014", override={v: (502, "") for v in VENUES})
    assert direct_vm.run_validator(leader_error=Exception("[TRANSIENT] only 1 venues answered")) is True
    assert direct_vm.run_validator(leader_error=Exception("[EXPECTED] window has not closed")) is False


# ---------- swaps ----------

def test_propose_and_accept_rules(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    start = FX["btc_1014"]["from"]
    direct_vm.warp(at(start - 12 * 3600))
    direct_vm.sender = direct_alice
    N = 10_000 * GEN
    m = c.quote_margin(N, "0.0001", 86_400, 1)
    assert m == 3 * GEN                                   # 10,000 x 0.01% x 3 intervals
    direct_vm.value = m
    for args, msg in [
        (("btc", True, "0.0001", "0.0001", N, start, 86_400, 1, ""), "asset looks like BTC"),
        (("BTC", True, "1bp", "0.0001", N, start, 86_400, 1, ""), "fixed_rate must be a decimal"),
        (("BTC", True, "0.02", "0.0001", N, start, 86_400, 1, ""), "within +-1%"),
        (("BTC", True, "0.0001", "0", N, start, 86_400, 1, ""), "cap must be above 0"),
        (("BTC", True, "0.0001", "0.0001", N, start + 3600, 86_400, 1, ""), "future 00/08/16 UTC"),
        (("BTC", True, "0.0001", "0.0001", N, start - 86_400, 86_400, 1, ""), "future 00/08/16 UTC"),
        (("BTC", True, "0.0001", "0.0001", N, start, 3600, 1, ""), "multiple of 8h"),
        (("BTC", True, "0.0001", "0.0001", N, start, 86_400, 91, ""), "n_periods"),
        (("BTC", True, "0.0001", "0.0001", 0, start, 86_400, 1, ""), "notional must be positive"),
        (("BTC", True, "0.0001", "0.0001", N, start, 86_400, 1, "bob"), "taker_only must be"),
        (("BTC", True, "0.0001", "0.0001", N, start, 86_400, 1, hexaddr(direct_alice)), "must differ"),
        (("BTC", True, "0.0001", "0.0002", N, start, 86_400, 1, ""), "send exactly the margin: 6"),
    ]:
        with direct_vm.expect_revert(msg):
            c.propose(*args)
    sid = c.propose("BTC", True, "0.0001", "0.0001", N, start, 86_400, 1, hexaddr(direct_bob))
    direct_vm.value = 0
    s = c.get_swap(sid)
    assert s["status"] == "open" and s["bal_maker"] == m and s["taker"] == "" and s["fixed"] == str(BP)
    direct_vm.sender = direct_charlie
    direct_vm.value = m
    with direct_vm.expect_revert("reserved for another taker"):
        c.accept(sid)
    direct_vm.sender = direct_bob
    direct_vm.value = m - 1
    with direct_vm.expect_revert("send exactly the margin"):
        c.accept(sid)
    direct_vm.value = m
    direct_vm.warp(at(start))
    with direct_vm.expect_revert("already started"):
        c.accept(sid)
    direct_vm.warp(at(start - 60))
    c.accept(sid)
    direct_vm.value = 0
    assert c.get_swap(sid)["status"] == "active"
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("swap is active"):
        c.cancel(sid)


def test_swap_settles_on_the_median(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    # Alice pays 0.01% per 8h fixed (3 bp for the day) on 10,000 GEN; Bob receives it.
    # Floating on 2026-10-14 was the OKX number, about 1.70 bp: Alice pays Bob the difference.
    c = direct_deploy(CONTRACT)
    start = FX["btc_1014"]["from"]
    sid = propose(direct_vm, c, direct_alice, start)
    accept(direct_vm, c, direct_bob, sid)
    direct_vm.warp(at(start + 86_400 + 300))
    with direct_vm.expect_revert("period 0 has not closed"):
        c.settle(sid)
    direct_vm.warp(at(start + 86_400 + 900))
    serve(direct_vm, "btc_1014")
    direct_vm.sender = direct_charlie                        # anyone may settle
    out = c.settle(sid)
    floating = med(expected("btc_1014").values())
    owed = 10_000 * GEN * (3 * BP - floating) // WAD
    assert out["floating"] == str(floating) and out["fixed"] == str(3 * BP)
    assert out["payer"] == "maker" and out["paid"] == owed and out["capped"] is False
    assert 1_290_000_000_000_000_000 < owed < 1_310_000_000_000_000_000     # ~1.30 GEN
    assert out["status"] == "closed"
    assert direct_vm.run_validator() is True
    s = c.get_swap(sid)
    assert s["bal_maker"] == 0 and s["bal_taker"] == 0 and s["settled"] == 1
    assert c.get_period(sid, 0)["paid"] == owed
    with direct_vm.expect_revert("swap is closed"):
        c.settle(sid)


def test_cap_limits_the_payment_and_margin_always_covers_it(direct_vm, direct_deploy, direct_alice, direct_bob):
    # fixed at 0.05% per 8h against ~1.7 bp floating: the move is far past the 0.01% cap
    c = direct_deploy(CONTRACT)
    start = FX["btc_1014"]["from"]
    sid = propose(direct_vm, c, direct_alice, start, fixed="0.0005", cap="0.0001")
    accept(direct_vm, c, direct_bob, sid)
    direct_vm.warp(at(start + 86_400 + 900))
    serve(direct_vm, "btc_1014")
    out = c.settle(sid)
    assert out["capped"] is True and out["diff"] == str(-3 * BP) and out["paid"] == 3 * GEN
    # the whole margin moved, and no more
    assert out["paid"] == c.get_swap(sid)["margin"]


def test_fixing_is_shared_between_swaps_and_fix(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    start = FX["btc_1014"]["from"]
    s1 = propose(direct_vm, c, direct_alice, start, pays_fixed=False)
    accept(direct_vm, c, direct_bob, s1)
    fix(direct_vm, c, "btc_1014", direct_alice)
    direct_vm.clear_mocks()                                   # no venue answers any more
    direct_vm.warp(at(start + 86_400 + 900))
    out = c.settle(s1)
    assert out["fixing_cached"] is True
    # Alice receives fixed here, so Bob (the fixed payer) pays
    assert out["payer"] == "taker"


def test_two_periods_with_a_sign_flip(direct_vm, direct_deploy, direct_alice, direct_bob):
    # fixed 0: Bob pays fixed and receives floating. 10-10 floating was positive, 10-11 negative.
    c = direct_deploy(CONTRACT)
    start = FX["btc_1010"]["from"]
    assert FX["btc_1011"]["from"] == start + 86_400
    sid = propose(direct_vm, c, direct_alice, start, pays_fixed=False, fixed="0", n=2)
    accept(direct_vm, c, direct_bob, sid)
    m = c.get_swap(sid)["margin"]
    direct_vm.warp(at(start + 2 * 86_400 + 900))
    serve(direct_vm, "btc_1010", "btc_1011")
    a = c.settle(sid)
    b = c.settle(sid)
    f0, f1 = med(expected("btc_1010").values()), med(expected("btc_1011").values())
    assert f0 > 0 > f1
    assert a["payer"] == "maker" and a["paid"] == 10_000 * GEN * f0 // WAD and a["status"] == "active"
    assert b["payer"] == "taker" and b["paid"] == 10_000 * GEN * -f1 // WAD and b["status"] == "closed"
    assert direct_vm.run_validator() is True
    assert c.get_period(sid, 1)["from"] == start + 86_400


def test_mutual_close_and_cancel(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    c = direct_deploy(CONTRACT)
    start = FX["btc_1010"]["from"]
    # cancel before anyone accepts
    sid = propose(direct_vm, c, direct_alice, start)
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("only the maker"):
        c.cancel(sid)
    direct_vm.sender = direct_alice
    assert c.cancel(sid) == 3 * GEN
    assert c.get_swap(sid)["status"] == "cancelled"
    # a 2-day swap both sides close during day 1: day 1 still settles, day 2 never happens
    sid = propose(direct_vm, c, direct_alice, start, n=2)
    accept(direct_vm, c, direct_bob, sid)
    direct_vm.warp(at(start + 3600))
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("only the two sides"):
        c.request_close(sid)
    direct_vm.sender = direct_alice
    assert c.request_close(sid) == {"close_votes": "maker", "end_period": 2, "status": "active"}
    direct_vm.sender = direct_bob
    assert c.request_close(sid) == {"close_votes": "maker,taker", "end_period": 1, "status": "active"}
    direct_vm.warp(at(start + 86_400 + 900))
    serve(direct_vm, "btc_1010")
    assert c.settle(sid)["status"] == "closed"
    # closing before the start refunds both at once
    sid = propose(direct_vm, c, direct_alice, start)
    accept(direct_vm, c, direct_bob, sid)
    direct_vm.sender = direct_alice
    c.request_close(sid)
    direct_vm.sender = direct_bob
    assert c.request_close(sid)["status"] == "closed"


def test_void_only_after_thirty_days(direct_vm, direct_deploy, direct_alice, direct_bob):
    c = direct_deploy(CONTRACT)
    start = FX["btc_1010"]["from"]
    sid = propose(direct_vm, c, direct_alice, start)
    accept(direct_vm, c, direct_bob, sid)
    direct_vm.warp(at(start + 86_400 + 29 * 86_400))
    with direct_vm.expect_revert("30 days after it ends"):
        c.void(sid)
    direct_vm.warp(at(start + 86_400 + 30 * 86_400))
    out = c.void(sid)
    assert out["void"] is True and out["paid"] == 0 and out["status"] == "closed"


def test_rate_parser_handles_every_venue_format(direct_vm, direct_deploy):
    # quote_margin runs the same parser as the venue data: KuCoin sends 1.0E-4, OKX 16 decimals
    c = direct_deploy(CONTRACT)
    one = 10**18
    assert c.quote_margin(one, "1.0E-4", 28_800, 1) == BP
    assert c.quote_margin(one, "0.0000954849645099", 28_800, 1) == 95_484_964_509_900
    assert c.quote_margin(one, "0.00010000", 28_800, 1) == BP
    assert c.quote_margin(one, "1e-4", 28_800, 1) == BP
