"""DART 공시 시그널 백테스트 — 최근 N일 시장 전체 공시로 '공시 다음 거래일 종가 매수' 기준 초과수익 측정.

측정 방식(보수적·재현 가능)
  진입: 공시일(t0) 다음 거래일 종가 (일 1회 배치로 실제 체결 가능한 가장 빠른 가격)
  보유: 5거래일 / 20거래일
  초과수익(AR) = 종목 수익률 − 시장(KODEX 200, 069500) 같은 기간 수익률
  보고: 건수, 평균·중앙값 AR, 승률(AR>0 비율), 연도별, 조건별(소각목적·규모)
  주의: 거래비용(세금 0.18%+수수료·슬리피지 ≈ 0.3~0.5%) 별도. 생존편향 없음(상장폐지 종목은 가격 조회 실패 → 제외됨을 명시).

출력: data/backtest_signals.json, data/backtest_report.md, 텔레그램 요약(토큰 있을 때)
환경: DART_API_KEY, BT_DAYS(기본 730), BT_MAX_DOCS(기본 2500: 내부자 보고서 원문 파싱 상한)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import dart_signals as S  # noqa: E402
import holidays as H  # noqa: E402
from update_prices import fetch_daily  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS = int(os.environ.get("BT_DAYS", "730"))
MAX_DOCS = int(os.environ.get("BT_MAX_DOCS", "2500"))
HOLD = (5, 20)


def ret_after(series, t0: dt.date, hold: int):
    """t0 다음 거래일 종가 진입 → hold 거래일 후 종가. (진입가, 수익률%) 또는 None."""
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    entry_d = H.next_trading_day(t0)
    if entry_d not in dates:
        return None
    i = dates.index(entry_d)
    if i + hold >= len(closes):
        return None
    return closes[i], (closes[i + hold] / closes[i] - 1) * 100


def main():
    if not S.KEY:
        print("DART_API_KEY 없음")
        return
    end = dt.date.today()
    bgn = end - dt.timedelta(days=DAYS)
    b, e = bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    print(f"기간 {b}~{e}")

    events = []  # {signal, sub, code, name, date, extra}
    # 1) 주요사항보고서(B001): 자사주·유증·CB·BW·EB
    for x in S.list_all(b, e, detail="B001"):
        c = S.classify(x["report_nm"])
        if c and x.get("stock_code"):
            events.append({"signal": c[0], "sub": c[1], "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"]})
    # 2) 거래소 수시공시(I001): 최대주주변경·횡령배임·불성실공시 등
    for x in S.list_all(b, e, detail="I001"):
        c = S.classify(x["report_nm"])
        if c and c[0] == "RISK" and x.get("stock_code"):
            events.append({"signal": "RISK", "sub": c[1], "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"]})
    print("B001/I001 이벤트:", len(events))
    # 3) 자사주 원문 파싱(소각 목적·규모)
    n_doc = 0
    for ev in events:
        if ev["signal"] == "BUYBACK":
            try:
                ev["extra"] = S.parse_buyback(S.doc_text(ev["rcept_no"]))
                n_doc += 1
                time.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                ev["extra"] = {"err": str(exc)[:60]}
    # 4) 내부자 보고서(D002): 최근부터 MAX_DOCS건 원문 파싱 (API 한도 때문에 상한)
    ins = S.list_all(b, e, detail="D002")
    ins.sort(key=lambda x: x["rcept_dt"], reverse=True)
    ins_cov_from = None
    for x in ins[:MAX_DOCS]:
        if not x.get("stock_code"):
            continue
        try:
            p = S.parse_insider(S.doc_text(x["rcept_no"]))
            n_doc += 1
            time.sleep(0.1)
        except Exception:  # noqa: BLE001
            continue
        ins_cov_from = x["rcept_dt"]
        if S.insider_ok(p):
            events.append({"signal": "INSIDER", "sub": "장내매수", "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"], "extra": p})
    print("원문 파싱:", n_doc, "/ 내부자 커버리지 시작일:", ins_cov_from, "/ 총 이벤트:", len(events))

    # 5) 가격·시장수익률
    mkt = fetch_daily("069500", count=DAYS + 60)
    cache: dict[str, list] = {}
    missing = 0
    for ev in events:
        t0 = dt.datetime.strptime(ev["date"], "%Y%m%d").date()
        if ev["code"] not in cache:
            try:
                cache[ev["code"]] = fetch_daily(ev["code"], count=DAYS + 60)
                time.sleep(0.2)
            except Exception:  # noqa: BLE001
                cache[ev["code"]] = []
        s = cache[ev["code"]]
        if not s:
            missing += 1
            continue
        for h in HOLD:
            r = ret_after(s, t0, h)
            m = ret_after(mkt, t0, h)
            if r and m:
                ev[f"ar{h}"] = round(r[1] - m[1], 2)
                ev[f"raw{h}"] = round(r[1], 2)
    # 6) 집계
    def agg(evs, h):
        xs = [x[f"ar{h}"] for x in evs if f"ar{h}" in x]
        if not xs:
            return None
        return {"n": len(xs), "mean": round(st.mean(xs), 2), "median": round(st.median(xs), 2),
                "win": round(sum(1 for v in xs if v > 0) / len(xs) * 100, 1),
                "mean_net": round(st.mean(xs) - 0.4, 2)}  # 왕복 비용 0.4% 차감
    groups = {}
    def add(key, evs):
        if evs:
            groups[key] = {f"h{h}": agg(evs, h) for h in HOLD}
    for sig in ("BUYBACK", "CANCEL", "INSIDER"):
        evs = [x for x in events if x["signal"] == sig]
        add(sig, evs)
        for y in sorted({x["date"][:4] for x in evs}):
            add(f"{sig}/{y}", [x for x in evs if x["date"].startswith(y)])
    bb = [x for x in events if x["signal"] == "BUYBACK" and x.get("extra")]
    add("BUYBACK/소각목적", [x for x in bb if x["extra"].get("cancel")])
    add("BUYBACK/비소각", [x for x in bb if not x["extra"].get("cancel")])
    add("BUYBACK/발행주식1%이상", [x for x in bb if (x["extra"].get("ratio_pct") or 0) >= 1])
    add("BUYBACK/100억이상", [x for x in bb if (x["extra"].get("amount") or 0) >= 1e10])
    add("INSIDER/최대주주", [x for x in events if x["signal"] == "INSIDER" and x["extra"].get("is_major")])
    add("INSIDER/5억이상", [x for x in events if x["signal"] == "INSIDER" and x["extra"].get("buy_amount", 0) >= 5e8])
    risk = [x for x in events if x["signal"] == "RISK"]
    add("RISK", risk)
    for sub in sorted({x["sub"] for x in risk}):
        add(f"RISK/{sub}", [x for x in risk if x["sub"] == sub])

    out = {"period": [b, e], "generated": dt.datetime.now().isoformat(timespec="minutes"), "n_events": len(events),
           "price_missing": missing, "insider_coverage_from": ins_cov_from, "groups": groups,
           "events": [{k: v for k, v in x.items() if k != "extra"} | {"extra": x.get("extra")} for x in events]}
    json.dump(out, open(os.path.join(ROOT, "data", "backtest_signals.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 7) 보고서
    L = [f"# DART 공시 시그널 백테스트 ({b}~{e})", "",
         f"- 이벤트 {len(events)}건 (가격 조회 불가·상장폐지 등 제외 {missing}건). 진입: 공시 다음 거래일 종가. AR = 종목수익률 − KODEX200 수익률.",
         f"- 내부자 보고서는 API 한도 때문에 최근 {MAX_DOCS}건만 원문 파싱 → 커버리지 {ins_cov_from or '-'} 이후.",
         "- mean_net = 평균 AR − 왕복 거래비용 0.4%p. 승률 = AR>0 비율. 표본 30건 미만은 참고만.", "",
         "| 그룹 | n | 5일 평균AR | 5일 중앙값 | 5일 승률 | 20일 평균AR | 20일 중앙값 | 20일 승률 | 20일 비용차감 |", "|---|---|---|---|---|---|---|---|---|"]
    for k, g in groups.items():
        a, c = g.get("h5"), g.get("h20")
        if not a and not c:
            continue
        f = lambda x, key: "-" if not x else x[key]
        L.append(f"| {k} | {f(c or a,'n')} | {f(a,'mean')} | {f(a,'median')} | {f(a,'win')}% | {f(c,'mean')} | {f(c,'median')} | {f(c,'win')}% | {f(c,'mean_net')} |")
    open(os.path.join(ROOT, "data", "backtest_report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))

    # 8) 텔레그램 요약
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        from notify_telegram import send
        lines = [f"<b>DART 시그널 백테스트 결과</b> ({b[:4]}.{b[4:6]}~{e[:4]}.{e[4:6]}, 진입=공시 다음날 종가, 시장 대비)"]
        for k in ("BUYBACK", "BUYBACK/소각목적", "CANCEL", "INSIDER", "INSIDER/최대주주", "RISK", "RISK/유상증자", "RISK/CB발행"):
            g = groups.get(k)
            if not g or not g.get("h20"):
                continue
            a, c = g.get("h5") or {}, g["h20"]
            lines.append(f"• {k}: n={c['n']} | 5일 {a.get('mean','-')}% (승률 {a.get('win','-')}%) | 20일 {c['mean']}% (승률 {c['win']}%, 비용차감 {c['mean_net']}%)")
        lines.append("표본 30건 미만 그룹은 참고만. 상세: data/backtest_report.md")
        send("\n".join(lines))


if __name__ == "__main__":
    main()
