#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日抓取黃豆儀表板即時資料,改寫 soybean/index.html 內的 JSON island。

資料源(免金鑰;FAS 可用 secrets.FAS_API_KEY 換正式金鑰):
  - Yahoo Finance v8 chart  : ZS=F 價格、DX-Y.NYB 美元趨勢、ZL=F/ZM=F 壓榨比
  - NOAA CPC                : ENSO Alert System Status + ONI
  - USDA esmis (Cornell 後繼): 最新 WASDE txt → 美豆庫銷比
  - Open-Meteo              : 南美三產區 30 日雨量 vs 10 年常年 + 14 日預報
  - USDA FAS ESR API        : 對中國大豆週淨銷售與年度累計承諾(DEMO_KEY)

任一來源失敗時保留 island 內既有值(idempotent,同 cb-history 模式)。
"""
import json
import os
import re
import sys
import html as htmllib
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
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


SA_SITES = [
    ("阿根廷核心", -33.90, -60.57),   # Pergamino
    ("巴西南", -23.31, -51.16),       # Londrina, Paraná
    ("馬托", -12.54, -55.71),         # Sorriso, Mato Grosso
]


def get_sa(today):
    """南美三產區:過去30日雨量、同窗口10年常年值、未來14日預報(mm)。"""
    regions = []
    win_start = today - timedelta(days=30)
    win = {((win_start + timedelta(days=i)).month, (win_start + timedelta(days=i)).day)
           for i in range(30)}
    for name, lat, lon in SA_SITES:
        f = json.loads(fetch(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}&daily=precipitation_sum"
            "&past_days=31&forecast_days=14&timezone=UTC"
        ))["daily"]
        t = today.isoformat()
        past = [v for d, v in zip(f["time"], f["precipitation_sum"]) if d < t and v is not None][-30:]
        fut = [v for d, v in zip(f["time"], f["precipitation_sum"]) if d >= t and v is not None][:14]
        a = json.loads(fetch(
            "https://archive-api.open-meteo.com/v1/archive"
            f"?latitude={lat}&longitude={lon}"
            f"&start_date={today.year - 10}-01-01&end_date={today.year - 1}-12-31"
            "&daily=precipitation_sum&timezone=UTC"
        ))["daily"]
        norm10 = sum(v for d, v in zip(a["time"], a["precipitation_sum"])
                     if v is not None and (int(d[5:7]), int(d[8:10])) in win)
        regions.append({"n": name, "p30": round(sum(past)),
                        "norm": round(norm10 / 10), "fc14": round(sum(fut))})
    return {"window": today.month in (12, 1, 2, 3), "regions": regions}


def esr_my(today):
    """ESR marketYear 編號:MY 2025/26(2025-09 起算)= 2026。"""
    return today.year + 1 if today.month >= 9 else today.year


def get_china(today):
    """對中國大豆:最新週淨銷售、4週合計、年度累計承諾與去年同期比。"""
    key = os.environ.get("FAS_API_KEY", "DEMO_KEY")
    url = ("https://api.fas.usda.gov/api/esr/exports/commodityCode/801"
           "/allCountries/marketYear/{}?api_key=" + key)
    my = esr_my(today)
    cur = sorted((r for r in json.loads(fetch(url.format(my)))
                  if r["countryCode"] == 5700), key=lambda r: r["weekEndingDate"])
    if not cur:
        raise ValueError("no China rows in current MY")
    last = cur[-1]
    wk = last["weekEndingDate"][:10]
    net = int(last["currentMYNetSales"])
    sum4 = int(sum(r["currentMYNetSales"] for r in cur[-4:]))
    commit = int(last["currentMYTotalCommitment"])
    pace = None
    prev_commit = None
    try:
        prev = sorted((r for r in json.loads(fetch(url.format(my - 1)))
                       if r["countryCode"] == 5700), key=lambda r: r["weekEndingDate"])
        target = (date.fromisoformat(wk) - timedelta(days=364)).isoformat()
        cand = [r for r in prev if r["weekEndingDate"][:10] <= target]
        if cand:
            prev_commit = int(cand[-1]["currentMYTotalCommitment"])
            if prev_commit:
                pace = round(commit / prev_commit, 2)
    except Exception as e:  # noqa: BLE001
        print(f"  china prev-MY pace failed (non-fatal): {e}")
    if pace is not None and pace >= 1.15:
        hint = 0          # Strong
    elif (pace is not None and pace <= 0.85) or sum4 < 0:
        hint = 2          # Weak
    else:
        hint = 1          # Normal
    return {"wk": wk, "net": net, "sum4": sum4, "commit": commit,
            "prevCommit": prev_commit, "pace": pace, "hint": hint}


def get_oil():
    """生柴/植物油客觀指標:ZL 60交易日漲跌 + 豆油占壓榨產值比。"""
    zl_res = yahoo_chart("ZL=F", "6mo")
    zl = float(zl_res["meta"]["regularMarketPrice"])
    closes = [c for c in zl_res["indicators"]["quote"][0]["close"] if c is not None]
    base = closes[-61] if len(closes) >= 61 else closes[0]
    zl60 = round(zl / base - 1, 2)
    zm = float(yahoo_chart("ZM=F", "5d")["meta"]["regularMarketPrice"])
    # 1 蒲式耳 → 約 11 磅油 + 0.0238 短噸粕;share = 油值/(油值+粕值)
    share = round(11 * zl / (11 * zl + 2.38 * zm), 2)
    opt = 0 if (zl60 >= 0.08 or share >= 0.45) else (2 if zl60 <= -0.08 else 1)
    return {"zl": round(zl, 2), "zm": round(zm, 1), "zl60": zl60,
            "share": share, "opt": opt}


def eval_alerts(data):
    """供需緊俏警示:任一成立即列入 island,新出現的另推 Telegram。"""
    alerts = []
    for my, pct in (data.get("stu") or {}).get("all") or []:
        if pct < 6:
            alerts.append({"code": f"STU_{my}",
                           "msg": f"庫銷比緊俏:{my} 僅 {pct}%,跌破 6% 易噴區"})
    enso = data.get("enso") or {}
    if enso.get("opt") == 0:
        alerts.append({"code": "LA_NINA",
                       "msg": f"La Nina Advisory 確立,南美乾旱風險升溫(ONI {enso.get('oni')})"})
    sa = data.get("sa") or {}
    if sa.get("window"):
        for r in sa.get("regions") or []:
            if r["n"] in ("阿根廷核心", "巴西南") and r.get("norm"):
                ratio = r["p30"] / r["norm"]
                fc_norm14 = r["norm"] * 14 / 30
                if ratio < 0.6 and r["fc14"] < fc_norm14 * 0.6:
                    alerts.append({"code": f"SA_DRY_{r['n']}",
                                   "msg": (f"南美乾旱警示:{r['n']} 30日雨量僅常年 "
                                           f"{round(ratio * 100)}%,14日預報 {r['fc14']}mm 持續偏乾")})
    china = data.get("china") or {}
    if china.get("pace") is not None and china["pace"] >= 1.15:
        alerts.append({"code": "CHINA_HOT",
                       "msg": f"中國採購超速:年度承諾達去年同期 {round(china['pace'] * 100)}%"})
    return alerts


def tg_push(text):
    tok = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        print("  TG not configured, skip push")
        return
    body = urllib.parse.urlencode(
        {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers=UA
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print("  TG alert pushed")


def main():
    if "--test-alert" in sys.argv:
        tg_push("黃豆儀表板測試:警示通道已接通\nhttps://ianlife4.github.io/soybean/")
        return 0
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
        ("sa", lambda: get_sa(now.date())),
        ("china", lambda: get_china(now.date())),
        ("oil", get_oil),
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

    data["alerts"] = eval_alerts(data)

    # 真正的資料(排除時間戳)沒變就不碰檔案,避免無意義 commit 與 Pages rebuild
    # 經 JSON 正規化比對,否則新資料的 tuple 與讀回的 list 永不相等
    canon = lambda d: json.loads(json.dumps({k: v for k, v in d.items() if k != "updated"}))  # noqa: E731
    if canon(old) == canon(data):
        print(f"no data change (ok={ok}); leaving file untouched")
        return 0

    # 只推播「新出現」的警示,既有警示持續顯示於頁面但不重複轟炸
    old_codes = {a.get("code") for a in old.get("alerts") or []}
    fresh = [a for a in data["alerts"] if a["code"] not in old_codes]
    if fresh:
        try:
            tg_push("黃豆儀表板警示\n"
                    + "\n".join("· " + a["msg"] for a in fresh)
                    + "\nhttps://ianlife4.github.io/soybean/")
        except Exception as e:  # noqa: BLE001
            print(f"  TG push failed (non-fatal): {e}")

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
