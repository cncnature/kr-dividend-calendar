"""DART 공시 시그널 백테스트 — 최근 N일 시장 전체 공시로 '공시 다음 거래일 종가 매수' 기준 초과수익 측정.

측정 방식(보수적·재현 가능)
  진입: 공시일(t0) 다음 거래일 종가 (일 1회 배치로 실제 체결 가능한 가장 빠른 가격)
  보유: 5거래일 / 20거래일
  초과수익(AR) = 종목 수익률 − 같은 시장 ETF(유가증권 KODEX200 069500 / 코스닥 KODEX코스닥150 229200) 같은 기간 수익률
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
WORKERS = int(os.environ.get("BT_WORKERS", "6"))
BUDGET = int(os.environ.get("BT_TIME_BUDGET", str(140 * 60)))  # 원문 파싱에 쓸 최대 초
HOLD = (5, 20)
CACHE = os.path.join(ROOT, "data", "signals_cache.json")  # rcept_no → 파싱 결과 (재실행 시 재사용)
T0 = time.time()


def log(*a):
    print(f"[{(time.time() - T0) / 60:5.1f}m]", *a, flush=True)


def parse_many(items: list[tuple[str, str]], cache: dict) -> dict:
    """items: [(rcept_no, kind)] kind ∈ buyback|insider. 캐시에 없는 건만 병렬 다운로드·파싱."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = [(rn, k) for rn, k in items if rn not in cache]
    log(f"원문 파싱 대상 {len(items)}건 중 캐시 {len(items) - len(todo)}건, 신규 {len(todo)}건")

    def work(rn, k):
        txt = S.doc_text(rn)
        return rn, (S.parse_buyback(txt) if k == "buyback" else S.parse_insider(txt))

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, rn, k): rn for rn, k in todo}
        for f in as_completed(futs):
            rn = futs[f]
            try:
                _, p = f.result()
                cache[rn] = p
            except Exception as exc:  # noqa: BLE001
                cache[rn] = {"err": str(exc)[:60]}
            done += 1
            if done % 200 == 0:
                log(f"  파싱 {done}/{len(todo)}")
                json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            if time.time() - T0 > BUDGET:
                log("시간 예산 초과 → 남은 건은 다음 실행에서 파싱")
                for g in futs:
                    g.cancel()
                break
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return cache


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
    log(f"기간 {b}~{e}")

    events = []  # {signal, sub, code, name, date, extra}
    # 1) 주요사항보고서(B001): 자사주·유증·CB·BW·EB
    for x in S.list_all(b, e, detail="B001"):
        c = S.classify(x["report_nm"])
        if c and x.get("stock_code"):
            events.append({"signal": c[0], "sub": c[1], "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"], "mkt": x["corp_cls"]})
    # 2) 거래소 수시공시(I001): 최대주주변경·횡령배임·불성실공시 등
    for x in S.list_all(b, e, detail="I001"):
        c = S.classify(x["report_nm"])
        if c and c[0] == "RISK" and x.get("stock_code"):
            events.append({"signal": "RISK", "sub": c[1], "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"], "mkt": x["corp_cls"]})
    log("B001/I001 이벤트:", len(events), {s: sum(1 for x in events if x["signal"] == s) for s in ("BUYBACK", "CANCEL", "RISK")})
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    # 3) 자사주 원문 파싱(소각 목적·규모)
    parse_many([(ev["rcept_no"], "buyback") for ev in events if ev["signal"] == "BUYBACK"], cache)
    for ev in events:
        if ev["signal"] == "BUYBACK":
            ev["extra"] = cache.get(ev["rcept_no"], {})
    # 4) 내부자 보고서(D002): 최근부터 MAX_DOCS건 원문 파싱 (API 한도 때문에 상한)
    ins = [x for x in S.list_all(b, e, detail="D002") if x.get("stock_code")]
    ins.sort(key=lambda x: x["rcept_dt"], reverse=True)
    log("D002 보고서:", len(ins))
    ins = ins[:MAX_DOCS]
    parse_many([(x["rcept_no"], "insider") for x in ins], cache)
    ins_cov_from, n_ok, n_err = None, 0, 0
    for x in ins:
        p = cache.get(x["rcept_no"])
        if not p or "err" in p:
            n_err += 1
            continue
        n_ok += 1
        ins_cov_from = x["rcept_dt"]
        if S.insider_ok(p):
            events.append({"signal": "INSIDER", "sub": "장내매수", "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"], "mkt": x["corp_cls"], "extra": p})
    n_buy = sum(1 for x in events if x["signal"] == "INSIDER")
    log(f"내부자 파싱 성공 {n_ok} / 실패 {n_err} / 장내매수 조건충족 {n_buy} / 커버리지 시작일 {ins_cov_from} / 총 이벤트 {len(events)}")
    # 파서 점검: 장내매수 금액이 0이 아닌 건 비율 (너무 낮으면 정규식 점검 필요)
    nz = sum(1 for x in ins if (cache.get(x["rcept_no"]) or {}).get("buy_amount", 0) > 0)
    log(f"파서 점검: 장내매수 금액 추출 {nz}/{n_ok}건, 자사주 금액 추출 {sum(1 for x in events if x['signal']=='BUYBACK' and x['extra'].get('amount'))}/{sum(1 for x in events if x['signal']=='BUYBACK')}건")

    # 5) 가격·시장수익률
    # 시장 벤치마크: 유가증권 → KODEX 200(069500), 코스닥 → KODEX 코스닥150(229200)
    bench = {"Y": fetch_daily("069500", count=DAYS + 60), "K": fetch_daily("229200", count=DAYS + 60)}
    codes = sorted({ev["code"] for ev in events})
    log(f"주가 조회 종목 {len(codes)}개")
    from concurrent.futures import ThreadPoolExecutor
    def _fd(c):
        try:
            return c, fetch_daily(c, count=DAYS + 60)
        except Exception:  # noqa: BLE001
            return c, []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        prices = dict(ex.map(_fd, codes))
    missing = 0
    for ev in events:
        t0 = dt.datetime.strptime(ev["date"], "%Y%m%d").date()
        s = prices.get(ev["code"]) or []
        if not s:
            missing += 1
            continue
        for h in HOLD:
            r = ret_after(s, t0, h)
            m = ret_after(bench.get(ev.get("mkt", "Y"), bench["Y"]), t0, h)
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
    add("INSIDER/대표이사", [x for x in events if x["signal"] == "INSIDER" and "대표이사" in x["extra"].get("title", "")])
    add("BUYBACK/공개매수", [x for x in bb if x["extra"].get("tender")])
    for m_, nm in (("Y", "유가증권"), ("K", "코스닥")):
        add(f"BUYBACK/{nm}", [x for x in events if x["signal"] == "BUYBACK" and x.get("mkt") == m_])
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
         f"- 이벤트 {len(events)}건 (가격 조회 불가·상장폐지 등 제외 {missing}건). 진입: 공시 다음 거래일 종가. AR = 종목수익률 − 같은 시장 ETF(KODEX200/코스닥150) 수익률.",
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
    log("완료"); print("\n".join(L), flush=True)

    # 8) 텔레그램 요약
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        from notify_telegram import send
        lines = [f"<b>DART 시그널 백테스트 결과</b> ({b[:4]}.{b[4:6]}~{e[:4]}.{e[4:6]}, 진입=공시 다음날 종가, 시장 대비)"]
        for k in ("BUYBACK", "BUYBACK/소각목적", "BUYBACK/공개매수", "CANCEL", "INSIDER", "INSIDER/최대주주", "INSIDER/대표이사", "INSIDER/5억이상", "RISK", "RISK/최대주주변경", "RISK/CB발행", "RISK/EB발행"):
            g = groups.get(k)
            if not g or not g.get("h20"):
                continue
            a, c = g.get("h5") or {}, g["h20"]
            lines.append(f"• {k}: n={c['n']} | 5일 {a.get('mean','-')}% (승률 {a.get('win','-')}%) | 20일 {c['mean']}% (승률 {c['win']}%, 비용차감 {c['mean_net']}%)")
        lines.append("표본 30건 미만 그룹은 참고만. 상세: data/backtest_report.md")
        send("\n".join(lines))


if __name__ == "__main__":
    main()
