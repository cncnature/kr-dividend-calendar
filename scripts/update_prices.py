"""배당락 전후 주가 통계 계산 (GitHub Actions에서 실행).

데이터: 네이버 금융 일봉 (fchart.stock.naver.com, XML)
각 과거 배당락일(E)에 대해
  pre_close      : E 직전 거래일 종가(배당 권리 포함 마지막 가격)
  ex_close       : E 종가
  ex_day_pct     : (ex_close / pre_close - 1) × 100     ← 배당락일 하락률
  theory_pct     : -dps / pre_close × 100               ← 배당금만큼의 이론 배당락
  excess_pct     : ex_day_pct - theory_pct              ← 이론치 대비 추가 하락(-)/선방(+)
  runup_days     : E 이전 40거래일 구간 저점이 E로부터 며칠 전인지 (= "배당락 n일 전부터 상승")
  runup_pct      : 저점 종가 → pre_close 상승률
  pre5_pct       : E 직전 5거래일 등락률
  recover_days   : E 이후 종가가 pre_close를 회복하기까지 거래일 수 (60거래일 내 미회복이면 null)
출력: data/price_stats.json  {code: {ex_date: {...}}}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAVER = "https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
LOOKBACK = 40
RECOVER_WINDOW = 60


def fetch_daily(code: str, count: int = 800) -> list[tuple[dt.date, float]]:
    req = urllib.request.Request(NAVER.format(code=code, count=count), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
    # 네이버 응답은 <?xml ... encoding="euc-kr"?> 선언 — expat이 다바이트 인코딩을 거부하므로 먼저 문자열로 디코딩
    import re
    txt = raw.decode("euc-kr", "ignore") if b"euc-kr" in raw[:100].lower() else raw.decode("utf-8", "ignore")
    txt = re.sub(r"^<\?xml[^>]*\?>", "", txt.strip())
    root = ET.fromstring(txt)
    out = []
    for item in root.iter("item"):
        d, o, h, l, c, v = item.attrib["data"].split("|")
        out.append((dt.datetime.strptime(d, "%Y%m%d").date(), float(c)))
    return sorted(out)


def compute(series: list[tuple[dt.date, float]], ex_date: dt.date, dps: float | None) -> dict | None:
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    if ex_date not in dates:
        return None
    i = dates.index(ex_date)
    if i < 2:
        return None
    pre_close, ex_close = closes[i - 1], closes[i]
    ex_day_pct = (ex_close / pre_close - 1) * 100
    lo = max(0, i - 1 - LOOKBACK)
    window = closes[lo:i]            # 배당락 전일까지
    j = lo + window.index(min(window))
    runup_days = (i - 1) - j         # 저점으로부터 배당락 전일까지의 거래일 수
    runup_pct = (pre_close / closes[j] - 1) * 100
    pre5_pct = (pre_close / closes[i - 6] - 1) * 100 if i >= 6 else None
    recover = None
    for k in range(i + 1, min(len(closes), i + 1 + RECOVER_WINDOW)):
        if closes[k] >= pre_close:
            recover = k - i
            break
    theory = (-dps / pre_close * 100) if dps else None
    r = lambda x: None if x is None else round(x, 2)
    return {
        "pre_close": pre_close, "ex_close": ex_close,
        "ex_day_pct": r(ex_day_pct), "theory_pct": r(theory),
        "excess_pct": r(ex_day_pct - theory) if theory is not None else None,
        "runup_days": runup_days + 1, "runup_pct": r(runup_pct),
        "runup_start": dates[j].isoformat(), "pre5_pct": r(pre5_pct),
        "recover_days": recover, "low_close": closes[j],
    }


def main():
    with open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8") as f:
        stocks = json.load(f)["stocks"]
    confirmed = {}
    cp = os.path.join(ROOT, "data", "confirmed.json")
    if os.path.exists(cp):
        confirmed = json.load(open(cp, encoding="utf-8"))
    sp = os.path.join(ROOT, "data", "price_stats.json")
    stats = json.load(open(sp, encoding="utf-8")) if os.path.exists(sp) else {}

    sys.path.insert(0, os.path.dirname(__file__))
    import holidays as H
    from build import derive

    today = dt.date.today()
    for st in stocks:
        code = st["code"]
        # 배당락일 후보: 시드 history + 시드/확정 배당금의 기준일에서 역산
        ex_dates: dict[str, float | None] = {e: None for e in st.get("history_ex_dates", [])}
        for d in st.get("dividends", []) + confirmed.get(code, []):
            ex = derive(dt.date.fromisoformat(d["record_date"]))["ex_date"]
            ex_dates[ex] = d.get("dps")
        todo = [e for e in ex_dates if dt.date.fromisoformat(e) < today and e not in stats.get(code, {})]
        if not todo:
            continue
        try:
            series = fetch_daily(code)
        except Exception as exc:  # noqa: BLE001
            print(f"[{code}] fetch failed: {exc}")
            continue
        for e in todo:
            res = compute(series, dt.date.fromisoformat(e), ex_dates[e])
            if res:
                stats.setdefault(code, {})[e] = res
                print(f"[{code} {st['name']}] {e} 배당락 {res['ex_day_pct']}% / 상승시작 D-{res['runup_days']} ({res['runup_pct']}%) / 회복 {res['recover_days']}일")
        time.sleep(0.5)
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
