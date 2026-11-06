"""Context beyond the chart: related markets, macro data and positioning.

* FRED (St. Louis Fed, free, no key): US 2y/10y yields, broad dollar index, VIX, WTI oil, S&P 500 (daily).
* Broker symbols (MT5): live prices of related CFDs if your broker offers them (configure names).
* CFTC Commitments of Traders (free, weekly): leveraged funds / asset managers in currency futures,
  managed money in gold.
* OANDA position book (needs an OANDA account API token): % of OANDA clients long/short.
Everything is cached on disk to respect the providers and keep tool calls fast."""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "trading-agent/0.3"}

FRED_SERIES = {
    "US2Y": ("DGS2", "US 2-year Treasury yield (%)"),
    "US10Y": ("DGS10", "US 10-year Treasury yield (%)"),
    "USD_BROAD": ("DTWEXBGS", "Broad US dollar index (Fed)"),
    "VIX": ("VIXCLS", "CBOE VIX"),
    "WTI": ("DCOILWTICO", "WTI crude oil ($)"),
    "SPX": ("SP500", "S&P 500"),
}

# CFTC: currency futures in the Traders in Financial Futures report, gold in the Disaggregated report.
COT_MARKETS = {
    "EUR": ("gpe5-46if", "EURO FX - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "GBP": ("gpe5-46if", "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "JPY": ("gpe5-46if", "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "CHF": ("gpe5-46if", "SWISS FRANC - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "CAD": ("gpe5-46if", "CANADIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "AUD": ("gpe5-46if", "AUSTRALIAN DOLLAR - CHICAGO MERCANTILE EXCHANGE", "lev_money"),
    "USD": ("gpe5-46if", "USD INDEX - ICE FUTURES U.S.", "lev_money"),
    "XAU": ("72hh-3qpy", "GOLD - COMMODITY EXCHANGE INC.", "m_money"),
}


def _cached_get(url: str, cache: Path, max_age_s: int, headers: dict | None = None) -> bytes:
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_s:
        return cache.read_bytes()
    try:
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
        return data
    except Exception:
        if cache.exists():  # stale beats nothing; caller sees the age
            return cache.read_bytes()
        raise


def fred_series(series_id: str, cache_dir: str = "cache", points: int = 30) -> list[tuple[str, float]]:
    """Official FRED API when FRED_API_KEY is set (free key: fredaccount.stlouisfed.org/apikeys),
    otherwise the public CSV download."""
    key = os.environ.get("FRED_API_KEY")
    if key:
        q = urllib.parse.urlencode({"series_id": series_id, "api_key": key, "file_type": "json",
                                    "sort_order": "desc", "limit": points + 10})
        raw = _cached_get(f"https://api.stlouisfed.org/fred/series/observations?{q}",
                          Path(cache_dir) / f"fred_{series_id}.json", 6 * 3600)
        obs = json.loads(raw).get("observations", [])
        rows = [(o["date"], float(o["value"])) for o in reversed(obs) if o.get("value") not in (None, "", ".")]
        return rows[-points:]
    raw = _cached_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                      Path(cache_dir) / f"fred_{series_id}.csv", 6 * 3600)
    rows = []
    for r in csv.reader(io.StringIO(raw.decode("utf-8"))):
        if len(r) == 2 and r[1] not in ("", ".") and r[0][:1].isdigit():
            try:
                rows.append((r[0], float(r[1])))
            except ValueError:
                pass
    return rows[-points:]


def macro_snapshot(cache_dir: str = "cache") -> dict:
    out = {}
    down = None
    for name, (sid, desc) in FRED_SERIES.items():
        if down:   # FRED unreachable: do not wait on every series
            out[name] = {"error": down}
            continue
        try:
            s = fred_series(sid, cache_dir, 30)
        except Exception as e:
            down = f"FRED unavailable ({e}). Set FRED_API_KEY to use the official API."
            out[name] = {"error": down}
            continue
        if not s:
            continue
        last_d, last = s[-1]
        def chg(n):
            return round(last - s[-1 - n][1], 4) if len(s) > n else None
        out[name] = {"description": desc, "date": last_d, "value": last, "chg_1d": chg(1), "chg_5d": chg(5),
                     "chg_20d": chg(20)}
    if "value" in out.get("US10Y", {}) and "value" in out.get("US2Y", {}):
        out["US_CURVE_10Y_2Y"] = round(out["US10Y"]["value"] - out["US2Y"]["value"], 3)
    out["note"] = "FRED daily closes (previous US session); use for regime/context, not intraday timing."
    return out


def broker_intermarket(broker, symbols: dict[str, str]) -> dict:
    """Live prices + daily change of related instruments on the broker (names differ per broker)."""
    from .market import bars_with_indicators
    out = {}
    for label, sym in (symbols or {}).items():
        try:
            d1 = bars_with_indicators(broker, sym, "D1", 25)
            bid, _ = broker.quote(sym)
            prev = float(d1["close"].iloc[-1])
            out[label] = {"symbol": sym, "price": bid, "chg_vs_prev_close_pct": round(100 * (bid / prev - 1), 3),
                          "chg_5d_pct": round(100 * (bid / float(d1["close"].iloc[-5]) - 1), 3),
                          "d1_trend": ("up" if d1["ema20"].iloc[-1] > d1["ema50"].iloc[-1] else "down")
                          if "ema50" in d1 and d1["ema50"].notna().iloc[-1] else "n/a"}
        except Exception as e:
            out[label] = {"symbol": sym, "error": str(e)}
    return out


def cot_positioning(code: str, cache_dir: str = "cache", weeks: int = 12) -> dict:
    if code not in COT_MARKETS:
        return {"error": f"no COT market configured for {code}"}
    dataset, market, group = COT_MARKETS[code]
    q = urllib.parse.urlencode({"$limit": weeks, "$order": "report_date_as_yyyy_mm_dd DESC",
                                "$where": f"market_and_exchange_names='{market}'"})
    raw = _cached_get(f"https://publicreporting.cftc.gov/resource/{dataset}.json?{q}",
                      Path(cache_dir) / f"cot_{code}.json", 12 * 3600)
    rows = json.loads(raw)
    if not rows:
        return {"error": "no COT rows"}
    lk, sk = (f"{group}_positions_long", f"{group}_positions_short") if group == "lev_money" else \
        ("m_money_positions_long_all", "m_money_positions_short_all")
    hist = []
    for r in rows:
        try:
            lng, sht = float(r[lk]), float(r[sk])
        except (KeyError, ValueError):
            continue
        hist.append({"date": r["report_date_as_yyyy_mm_dd"][:10], "net": lng - sht, "long": lng, "short": sht,
                     "open_interest": float(r.get("open_interest_all", 0) or 0)})
    if not hist:
        return {"error": "unexpected COT format"}
    nets = [h["net"] for h in hist]
    lo, hi = min(nets), max(nets)
    latest = hist[0]
    return {"market": market, "trader_group": "leveraged funds" if group == "lev_money" else "managed money",
            "report_date": latest["date"], "net_contracts": latest["net"],
            "net_change_1w": latest["net"] - hist[1]["net"] if len(hist) > 1 else None,
            "net_position_in_12w_range_pct": round(100 * (latest["net"] - lo) / (hi - lo), 1) if hi > lo else None,
            "note": "Weekly, released Fridays for Tuesday's positions. Extremes can mean crowded trades."}


def oanda_position_book(symbol: str, token: str | None = None, practice: bool = True,
                        cache_dir: str = "cache") -> dict:
    token = token or os.environ.get("OANDA_API_TOKEN")
    if not token:
        return {"error": "set OANDA_API_TOKEN (OANDA account > Manage API Access) to enable retail positioning"}
    instrument = f"{symbol[:3]}_{symbol[3:6]}" if len(symbol) >= 6 else symbol
    host = "api-fxpractice.oanda.com" if practice else "api-fxtrade.oanda.com"
    raw = _cached_get(f"https://{host}/v3/instruments/{instrument}/positionBook",
                      Path(cache_dir) / f"oanda_pb_{instrument}.json", 20 * 60,
                      headers={"Authorization": f"Bearer {token}"})
    book = json.loads(raw).get("positionBook", {})
    buckets = book.get("buckets", [])
    if not buckets:
        return {"error": "empty position book"}
    lng = sum(float(b["longCountPercent"]) for b in buckets)
    sht = sum(float(b["shortCountPercent"]) for b in buckets)
    price = float(book.get("price", 0))
    above = sum(float(b["longCountPercent"]) + float(b["shortCountPercent"])
                for b in buckets if float(b["price"]) > price)
    return {"instrument": instrument, "time": book.get("time"),
            "clients_long_pct": round(100 * lng / (lng + sht), 1) if lng + sht else None,
            "clients_short_pct": round(100 * sht / (lng + sht), 1) if lng + sht else None,
            "share_of_positions_opened_above_price_pct": round(above, 1),
            "note": "Retail positioning is usually read contrarian when extreme (>70% one side)."}


def positioning(symbol: str, cache_dir: str = "cache", oanda_practice: bool = True) -> dict:
    base, quote = symbol[:3].upper(), symbol[3:6].upper()
    out = {"symbol": symbol, "cot": {}}
    for code in (base, quote):
        if code in COT_MARKETS:
            try:
                out["cot"][code] = cot_positioning(code, cache_dir)
            except Exception as e:
                out["cot"][code] = {"error": str(e)}
    try:
        out["oanda_retail"] = oanda_position_book(symbol, practice=oanda_practice, cache_dir=cache_dir)
    except Exception as e:
        out["oanda_retail"] = {"error": str(e)}
    return out
