"""배당 캘린더 데이터 생성기.

입력
- data/stocks.json      : 종목별 기준일 규칙·최근 배당금(수동 시드)
- data/confirmed.json   : DART 공시로 확정된 기준일/배당금 (update_dart.py 생성, 없으면 무시)
- data/price_stats.json : 배당락 전후 주가 통계 (update_prices.py 생성, 없으면 무시)
출력
- site/data.json        : 웹 캘린더가 읽는 단일 파일

날짜 계산(국내 주식 T+2 결제)
  기준일 R  →  명부 확정 거래일 S = R 이전·당일 중 마지막 거래일
  배당락일  = S 직전 거래일           (이날부터 사면 배당 없음)
  마지막 매수일 = 배당락일 직전 거래일 (이날 장 마감까지 보유해야 배당)
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import holidays as H  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YEARS = [int(y) for y in os.environ.get("CAL_YEARS", "2026,2027,2028,2029,2030").split(",")]


def load(path, default):
    p = os.path.join(ROOT, path)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def derive(record: dt.date) -> dict:
    s = H.last_trading_on_or_before(record)
    ex = H.prev_trading_day(s)
    buy = H.prev_trading_day(ex)
    return {"record_date": record.isoformat(), "ex_date": ex.isoformat(), "last_buy_date": buy.isoformat()}


def rule_dates(rule: dict, year: int) -> list[dt.date]:
    if rule["type"] == "eom":
        return [dt.date(year, m, calendar.monthrange(year, m)[1]) for m in rule["months"]]
    if rule["type"] == "fixed":
        out = []
        for m, d in rule["dates"]:
            d = min(d, calendar.monthrange(year, m)[1])
            out.append(dt.date(year, m, d))
        return out
    raise ValueError(rule)


def period_label(stock: dict, record: dt.date) -> str:
    """기준일 → 귀속 기간 라벨 (예: 2027-1Q, 2026-결산)."""
    m = record.month
    if stock["freq"] == "연":
        return f"{record.year - 1 if m <= 6 else record.year}-결산"
    rule = stock["rule"]
    months = rule.get("months") or [x[0] for x in rule["dates"]]
    idx = sorted(months).index(m)
    if rule["type"] == "eom" and 12 in months:  # 3·6·9·12 형
        return f"{record.year}-{['1Q','2Q','3Q','결산'][idx]}"
    # 2·5·8·11 형: 2월 = 전년 결산, 5월 = 1Q ...
    return f"{record.year - 1}-결산" if idx == 0 else f"{record.year}-{idx}Q"


def main():
    stocks = load("data/stocks.json", {})["stocks"]
    confirmed = load("data/confirmed.json", {})   # {code: [{record_date, dps, period, source, pay_date}]}
    pstats = load("data/price_stats.json", {})    # {code: {ex_date: {...}}}

    events = []
    for st in stocks:
        code = st["code"]
        conf_map = {c["record_date"]: c for c in confirmed.get(code, [])}
        # 참고 배당액: 공시 확정분 + 시드 합쳐 최신순
        divs = sorted({d["record_date"]: d for d in st.get("dividends", []) + confirmed.get(code, [])}.values(),
                      key=lambda x: x["record_date"], reverse=True)
        # 과거 배당락 통계 요약(최근 4건 평균)
        hist = [v | {"ex_date": k} for k, v in pstats.get(code, {}).items()]
        hist.sort(key=lambda x: x["ex_date"], reverse=True)
        recent = hist[:4]
        summary = None
        if recent:
            avg = lambda key: round(sum(r[key] for r in recent if r.get(key) is not None) / max(1, len([r for r in recent if r.get(key) is not None])), 2)
            # 배당 플레이 승률: '마지막 매수일 종가에 사서 배당을 받은 뒤' 배당락 전 종가를 20거래일 안에 회복했거나,
            # 배당락일 하락이 배당금(이론 배당락)보다 작았던 경우를 '승'으로 본다 (= 배당을 손실 없이 챙긴 경우). 전체 과거 이력 기준.
            wins = [r for r in hist if (r.get("recover_days") is not None and r["recover_days"] <= 20) or (r.get("excess_pct") is not None and r["excess_pct"] >= 0)]
            summary = {
                "n": len(recent),
                "avg_ex_day_pct": avg("ex_day_pct"),
                "avg_runup_pct": avg("runup_pct"),
                "avg_runup_days": avg("runup_days"),
                "avg_recover_days": avg("recover_days"),
                "win_n": len(hist), "win_k": len(wins),
                "win_rate": round(len(wins) / len(hist) * 100) if hist else None,
                "high_win": len(hist) >= 4 and len(wins) / len(hist) >= 0.75,
            }
        for year in YEARS:
            # 규칙으로 산출한 기준일을 귀속기간(period)별로 모으고, 같은 기간에 DART 확정 공시가 있으면 그 날짜로 교체
            by_period = {}
            for rec in rule_dates(st["rule"], year):
                by_period[period_label(st, rec)] = rec
            for c in confirmed.get(code, []):
                if c["record_date"].startswith(str(year)):
                    by_period[c["period"]] = dt.date.fromisoformat(c["record_date"])
            for rec in sorted(by_period.values()):
                d = derive(rec)
                c = conf_map.get(d["record_date"])
                status = "확정" if c else ("예상" if st["confidence"] in ("A", "B") else "예상(불확실)")
                ev = {
                    "code": code, "name": st["name"], "market": st["market"], "freq": st["freq"],
                    "period": c["period"] if c else period_label(st, rec),
                    **d,
                    "status": status,
                    "dps": c["dps"] if c else None,
                    "pay_date": c.get("pay_date") if c else None,
                    "source": c.get("source") if c else None,
                    "holiday_official": H.is_official(rec.year),
                    "confidence": st["confidence"],
                    "note": st.get("note", ""),
                    "ref_dividends": divs[:4],
                    "ex_stats": summary,
                    "ex_history": recent,
                }
                events.append(ev)
    events.sort(key=lambda e: (e["ex_date"], e["code"]))

    out = {
        "generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "years": YEARS,
        "holidays": {str(y): H.holidays(y) for y in YEARS},
        "holiday_official_years": [y for y in YEARS if H.is_official(y)],
        "events": events,
        "stocks": [{"code": s["code"], "name": s["name"], "freq": s["freq"], "confidence": s["confidence"],
                    "win": next((e["ex_stats"] for e in events if e["code"] == s["code"] and e["ex_stats"]), None)} for s in stocks],
    }
    os.makedirs(os.path.join(ROOT, "site"), exist_ok=True)
    with open(os.path.join(ROOT, "site", "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"events: {len(events)}  years: {YEARS}")


if __name__ == "__main__":
    main()
