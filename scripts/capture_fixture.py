"""Save the raw funding-history answers of the five venues for one window, fetched
with the same URLs the contract builds, so the tests run on real data.

    python scripts/capture_fixture.py <name> <ASSET> <from ISO> <to ISO>
    python scripts/capture_fixture.py btc_1014 BTC 2026-10-14T00:00 2026-10-15T00:00

Each venue is stored as {"status", "body"} exactly as it came back (body as text).
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def urls(asset, ps, pe):
    a, b = (ps + 60) * 1000, (pe + 60) * 1000
    kc = "XBT" if asset == "BTC" else asset
    return {
        "binance": ("GET", f"https://www.binance.com/fapi/v1/fundingRate?symbol={asset}USDT&startTime={a}&endTime={b}&limit=1000", None),
        "okx": ("GET", f"https://www.okx.com/api/v5/public/funding-rate-history?instId={asset}-USDT-SWAP&before={a}&after={b}&limit=100", None),
        "gate": ("GET", f"https://api.gateio.ws/api/v4/futures/usdt/funding_rate?contract={asset}_USDT&from={ps + 60}&to={pe + 60}&limit=1000", None),
        "kucoin": ("GET", f"https://api-futures.kucoin.com/api/v1/contract/funding-rates?symbol={kc}USDTM&from={a}&to={b}", None),
        "hyperliquid": ("POST", "https://api.hyperliquid.xyz/info",
                        json.dumps({"type": "fundingHistory", "coin": asset, "startTime": a, "endTime": b})),
    }


def fetch(method, url, body):
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method,
                                 headers={"content-type": "application/json", "user-agent": "curl/8.4.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def main(name, asset, frm, to):
    ps, pe = ts(frm), ts(to)
    out = {"asset": asset, "from": ps, "to": pe, "captured": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for venue, (method, url, body) in urls(asset, ps, pe).items():
        status, text = fetch(method, url, body)
        out[venue] = {"status": status, "body": text}
        print(venue, status, len(text))
    path = Path(__file__).parent.parent / "tests" / "fixtures" / f"{name}.json"
    path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main(*sys.argv[1:5])
