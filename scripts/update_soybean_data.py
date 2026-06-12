#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日抓取黃豆儀表板即時資料,改寫 soybean/index.html 內的 JSON island。

資料源(全部免金鑰):
  - Yahoo Finance v8 chart  : ZS=F 價格、DX-Y.NYB 美元指數趨勢
  - NOAA CPC                : ENSO Alert System Status + ONI
  - USDA esmis (Cornell 後繼): 最新 WASDE txt → 美豆庫銷比

任一來源失敗時保留 island 內既有值(idempotent,同 cb-history 模式)。
"""
import json
import re
import sys
import html as htmllib
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "soybean" / "index.html"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ISLAND_RE = re.compile(
    r'(<script type="application/json" id="live">)(.*?)(</script>)', re.S
)


def fetch(url, timeout=30, retries=3):
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"  retry {i + 1}/{retries} {url}: {e}")
    raise last_err


def yahoo_chart(symbol, rng):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.request.quote(symbol)}?range={rng}&interval=1d"
    )
    j = json.loads(fetch(url))
    return j["chart"]["result"][0]


def get_price():
    res = yahoo_chart("ZS=F", "5d")
    meta = res["meta"]
    last = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    tz = ZoneInfo(meta.get("exchangeTimezoneName", "America/Chicago"))
    as_of = datetime.fromtimestamp(meta["regularMarketTime"], tz).date().isoformat()
    return {
        "last": round(float(last), 2),
        "chg": round(float(last) - float(prev), 2) if prev else None,
        "asOf": as_of,
        "contract": meta.get("shortName", "ZS"),
    }


def get_dxy():
    res = yahoo_chart("DX-Y.NYB", "3mo")
    closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    last = res["meta"]["regularMarketPrice"] or closes[-1]
    if len(closes) < 21:
        raise ValueError("DXY history too short")
    chg20 = last / closes[-21] - 1
    opt = 2 if chg20 >= 0.02 else (0 if chg20 <= -0.02 else 1)  # strong/weak/neutral
    # 頁面只用 opt;chg20d 僅供除錯,取 2 位避免微小跳動觸發無意義 commit
    return {"chg20d": round(chg20, 2), "opt": opt}


def get_enso():
    page = htmllib.unescape(
        fetch(
            "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
            "enso_advisory/ensodisc.shtml"
        ).decode("utf-8", "replace")
    )
    seg = page[page.find("Alert System Status"):][:600]
    m = re.search(r"(La Ni[ñn]a|El Ni[ñn]o)\s*(Advisory|Watch)", seg, re.I)
    if m:
        status = f"{m.group(1)} {m.group(2)}".title()
    elif re.search(r"Not\s+Active", seg, re.I):
        status = "Not Active"
    else:
        raise ValueError("ENSO status not found")
    low = status.lower()
    if "la ni" in low and "advisory" in low:
        opt = 0
    elif "el ni" in low and "watch" in low:
        opt = 2
    elif "el ni" in low and "advisory" in low:
        opt = 3
    else:  # La Niña Watch / Not Active → 中性
        opt = 1
    oni = None
    try:
        rows = fetch(
            "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
        ).decode().strip().splitlines()
        oni = float(rows[-1].split()[-1])
    except Exception as e:  # noqa: BLE001
        print(f"  ONI fetch failed (non-fatal): {e}")
    return {"status": status, "oni": oni, "opt": opt}


def current_my(today):
    """美豆行銷年度 9/1 起算: 2026-06 → 2025/26;2026-09 → 2026/27。"""
    y = today.year if today.month >= 9 else today.year - 1
    return f"{y}/{(y + 1) % 100:02d}"


def get_stu(today):
    api = json.loads(
        fetch("https://esmis.nal.usda.gov/api/v1/release/findByIdentifier/wasde?latest=true")
    )
    latest = api["results"][0]
    txt_url = next(u for u in latest["files"] if u.endswith(".txt"))
    release = latest["release_datetime"][:10]
    text = fetch(txt_url).decode("utf-8", "replace")

    start = text.find("U.S. Soybeans and Products Supply and Use")
    if start < 0:
        raise ValueError("soybeans table not found in WASDE txt")
    seg = text[start: text.find("SOYBEAN OIL", start)]
    years = re.findall(r"20\d{2}/\d{2}", seg[:400])

    def row(label):
        m = re.search(rf"^\s*{label}\s+(.+)$", seg, re.M)
        return [float(x.replace(",", "")) for x in re.findall(r"[\d,]+(?:\.\d+)?", m.group(1))]

    use, stocks = row("Use, Total"), row("Ending Stocks")
    if not (len(years) == len(use) == len(stocks)):
        raise ValueError(f"column mismatch: {years} use={use} stocks={stocks}")
    # 同一年度多欄(上月/本月)時 dict 依序覆寫,留最新一欄
    stu_by_my = {y: round(s / u * 100, 1) for y, u, s in zip(years, use, stocks)}

    my = current_my(today)
    pct = stu_by_my.get(my)
    if pct is None:  # 理論上不會發生:WASDE 永遠涵蓋當前年度
        my, pct = sorted(stu_by_my.items())[-1]
    opt = 0 if pct < 6 else (1 if pct < 8 else (2 if pct <= 10 else 3))
    return {
        "my": my,
        "pct": pct,
        "opt": opt,
        "all": sorted(stu_by_my.items()),
        "wasde": release,
    }


def main():
    htmltext = INDEX.read_text(encoding="utf-8")
    m = ISLAND_RE.search(htmltext)
    if not m:
        print("FATAL: live island not found in index.html")
        return 1
    try:
        old = json.loads(m.group(2))
    except Exception:  # noqa: BLE001
        old = {}

    now = datetime.now(timezone.utc)
    data = dict(old)
    ok, failed = [], []
    for key, fn in (
        ("price", get_price),
        ("dxy", get_dxy),
        ("enso", get_enso),
        ("stu", lambda: get_stu(now.date())),
    ):
        try:
            data[key] = fn()
            ok.append(key)
        except Exception as e:  # noqa: BLE001
            failed.append(key)
            print(f"  {key} failed, keeping previous value: {e}")
    if not ok:
        print("FATAL: all sources failed, leaving file untouched")
        return 1

    # 真正的資料(排除時間戳)沒變就不碰檔案,避免無意義 commit 與 Pages rebuild
    strip = lambda d: {k: v for k, v in d.items() if k != "updated"}  # noqa: E731
    if strip(old) == strip(data):
        print(f"no data change (ok={ok}); leaving file untouched")
        return 0

    data["updated"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    new_json = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    htmltext = ISLAND_RE.sub(lambda mm: mm.group(1) + new_json + mm.group(3), htmltext, count=1)
    INDEX.write_text(htmltext, encoding="utf-8", newline="\n")

    print(f"updated={data['updated']} ok={ok} failed={failed}")
    for k in ok:
        print(f"  {k}: {json.dumps(data[k], ensure_ascii=True)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
