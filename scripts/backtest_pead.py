"""DART 잠정실적(PEAD) · 단일판매공급계약 공시 백테스트.

근거
  PEAD: Ball & Brown(1968) 이후 전 세계·한국에서 재확인된 실적발표 후 주가 드리프트. 서프라이즈 대용치로
        ① 영업이익 전년동기비 증감률(공시 원문), ② 공시 전후 2일 주가반응 EAR = close(t+1)/close(t−1) − 1 (Brandt et al. 2008) 사용.
  공급계약: 국내 선행연구에서 계약금액/최근 매출액 비율이 클수록 공시일 CAR 양(+). 공시 후 드리프트가 있는지 검정.
설계(보수적): 진입 = 공시일 다음 거래일 종가(t+1), 보유 20/60거래일, AR = 종목 − 같은 시장 ETF(KODEX200 / 코스닥150).
출력: data/backtest_pead.json, data/backtest_pead.md, 텔레그램 요약
환경: DART_API_KEY, BT_DAYS(730), BT_WORKERS(6), BT_TIME_BUDGET
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))
import dart_signals as S  # noqa: E402
import holidays as H  # noqa: E402
from update_prices import fetch_daily  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS = int(os.environ.get("BT_DAYS", "730"))
WORKERS = int(os.environ.get("BT_WORKERS", "6"))
BUDGET = int(os.environ.get("BT_TIME_BUDGET", str(140 * 60)))
CACHE = os.path.join(ROOT, "data", "pead_cache.json")
T0 = time.time()
HOLD = (20, 60)


def log(*a):
    print(f"[{(time.time() - T0) / 60:5.1f}m]", *a, flush=True)


def _n(s):
    if s is None:
        return None
    s = s.replace(",", "").replace("△", "-").replace("(", "-").replace(")", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


NUM = r"(-?[\d,]+|-|\(\s*[\d,]+\s*\))"
PCT = r"(-?[\d.,]+|-|\(\s*[\d.,]+\s*\)|적자전환|흑자전환|적자지속|흑자지속)"


def parse_earn(txt: str) -> dict:
    """잠정실적 표: 구분 | 당기실적 | 전기실적 | 전기대비증감율 | 전년동기실적 | 전년동기대비증감율.
    매출액·영업이익 행의 당기·전년동기 값으로 YoY 계산(증감율 칸은 '적자전환' 같은 문자가 있어 직접 계산)."""
    out = {}
    for key in ("매출액", "영업이익", "당기순이익"):
        m = re.search(rf"{key}\s*(?:당해실적|당기실적)?\s*{NUM}\s+{NUM}\s+{PCT}\s+{NUM}\s+{PCT}", txt)
        if m:
            cur, prev_y = _n(m.group(1)), _n(m.group(4))
            out[key] = {"cur": cur, "py": prev_y,
                        "yoy": (cur - prev_y) / abs(prev_y) * 100 if cur is not None and prev_y not in (None, 0) else None}
    out["consolidated"] = "연결" in txt[:800]
    return out


def parse_contract(txt: str) -> dict:
    g = lambda pat: (re.search(pat, txt) or [None, None])[1]
    amt = _n(g(r"계약금액\s*\(?원?\)?\s*(?:\(VAT\s*포함\))?\s*([\d,]+)"))
    sales = _n(g(r"최근\s*매출액\s*\(?원?\)?\s*([\d,]+)"))
    ratio = _n(g(r"매출액\s*대비\s*\(?%?\)?\s*([\d.,]+)"))
    if ratio is None and amt and sales:
        ratio = amt / sales * 100
    party = (g(r"계약상대방?\s*(.{2,40}?)\s*(?:계약내용|판매ㆍ공급지역|판매·공급지역|공급지역)") or "").strip()
    return {"amount": amt, "sales": sales, "ratio_pct": ratio, "party": party, "export": bool(re.search(r"해외|수출|미국|유럽|중국|일본", party))}


def parse_many(items, cache):
    todo = [(rn, k) for rn, k in items if rn not in cache]
    log(f"원문 파싱 {len(items)}건 중 신규 {len(todo)}건")

    def work(rn, k):
        t = S.doc_text(rn)
        return rn, (parse_earn(t) if k == "earn" else parse_contract(t)), t

    done, shown = 0, {"earn": 0, "contract": 0}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(work, rn, k): (rn, k) for rn, k in todo}
        for f in as_completed(futs):
            rn, k = futs[f]
            try:
                _, p, t = f.result()
                cache[rn] = p
                ok = ("영업이익" in p) if k == "earn" else (p.get("ratio_pct") is not None)
                if not ok and shown[k] < 2:  # 파서 점검용 원문 조각
                    shown[k] += 1
                    i = t.find("영업이익" if k == "earn" else "계약금액")
                    log(f"파싱실패 샘플[{k}] {rn}: {t[max(0, i - 150): i + 350]}")
            except Exception as exc:  # noqa: BLE001
                cache[rn] = {"err": str(exc)[:60]}
            done += 1
            if done % 300 == 0:
                log(f"  파싱 {done}/{len(todo)}")
                json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            if time.time() - T0 > BUDGET:
                log("시간 예산 초과")
                for g in futs:
                    g.cancel()
                break
    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)


def ret_from(series, d_from: dt.date, hold: int):
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    if d_from not in dates:
        return None
    i = dates.index(d_from)
    if i + hold >= len(closes):
        return None
    return (closes[i + hold] / closes[i] - 1) * 100


def ear(series, t0: dt.date):
    """공시 전후 2일 수익률: close(t+1)/close(t−1) − 1."""
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    d1 = H.next_trading_day(t0)
    if d1 not in dates:
        return None
    i = dates.index(d1)
    if i < 2:
        return None
    return (closes[i] / closes[i - 2] - 1) * 100


def agg(xs):
    if len(xs) < 3:
        return None
    sd = st.stdev(xs)
    return {"n": len(xs), "mean": round(st.mean(xs), 2), "median": round(st.median(xs), 2),
            "win": round(sum(1 for v in xs if v > 0) / len(xs) * 100, 1),
            "t": round(st.mean(xs) / (sd / math.sqrt(len(xs))), 2) if sd else None}


def main():
    if not S.KEY:
        print("DART_API_KEY 없음")
        return
    end = dt.date.today()
    b, e = (end - dt.timedelta(days=DAYS)).strftime("%Y%m%d"), end.strftime("%Y%m%d")
    log("기간", b, e)
    cache = json.load(open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}

    # 1) 잠정실적 (공정공시 I002)
    earn = []
    for x in S.list_all(b, e, detail="I002"):
        n = x["report_nm"].replace(" ", "")
        if "잠정" in n and "실적" in n and not re.search(r"정정|철회", n) and x.get("stock_code"):
            earn.append(x)
    # 2) 단일판매·공급계약 (수시공시 I001)
    cont = []
    for x in S.list_all(b, e, detail="I001"):
        n = x["report_nm"].replace(" ", "")
        if re.search(r"단일판매.?공급계약체결", n) and not re.search(r"정정|해지|철회", n) and x.get("stock_code"):
            cont.append(x)
    log(f"잠정실적 {len(earn)}건, 공급계약 {len(cont)}건")
    parse_many([(x["rcept_no"], "earn") for x in earn] + [(x["rcept_no"], "contract") for x in cont], cache)

    # 3) 가격
    codes = sorted({x["stock_code"] for x in earn + cont})
    log("가격 조회", len(codes))
    bench = {"Y": fetch_daily("069500", count=DAYS + 90), "K": fetch_daily("229200", count=DAYS + 90)}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        px = dict(ex.map(lambda c: (c, (lambda: fetch_daily(c, count=DAYS + 90))() if True else []), codes))

    def measure(x, extra, kind):
        t0 = dt.datetime.strptime(x["rcept_dt"], "%Y%m%d").date()
        s = px.get(x["stock_code"]) or []
        if not s:
            return None
        d1 = H.next_trading_day(t0)
        ev = {"kind": kind, "code": x["stock_code"], "name": x["corp_name"], "date": x["rcept_dt"], "rcept_no": x["rcept_no"], "mkt": x["corp_cls"], "extra": extra}
        e0 = ear(s, t0)
        em = ear(bench[x["corp_cls"]], t0)
        ev["ear"] = round(e0 - em, 2) if e0 is not None and em is not None else None
        for h in HOLD:
            r, m = ret_from(s, d1, h), ret_from(bench[x["corp_cls"]], d1, h)
            if r is not None and m is not None:
                ev[f"ar{h}"] = round(r - m, 2)
        return ev

    events = []
    for x in earn:
        p = cache.get(x["rcept_no"]) or {}
        if "영업이익" in p:
            ev = measure(x, p, "EARN")
            if ev:
                events.append(ev)
    for x in cont:
        p = cache.get(x["rcept_no"]) or {}
        if p.get("ratio_pct") is not None:
            ev = measure(x, p, "CONTRACT")
            if ev:
                events.append(ev)
    n_e = sum(1 for v in events if v["kind"] == "EARN")
    log(f"측정 이벤트 {len(events)} (잠정실적 {n_e}, 공급계약 {len(events) - n_e}) / 파싱 성공률 잠정 {sum(1 for x in earn if '영업이익' in (cache.get(x['rcept_no']) or {}))}/{len(earn)}, 계약 {sum(1 for x in cont if (cache.get(x['rcept_no']) or {}).get('ratio_pct') is not None)}/{len(cont)}")

    # 4) 집계
    E = [v for v in events if v["kind"] == "EARN"]
    C = [v for v in events if v["kind"] == "CONTRACT"]
    groups = {}

    def add(key, evs):
        g = {f"h{h}": agg([v[f"ar{h}"] for v in evs if f"ar{h}" in v]) for h in HOLD}
        if any(g.values()):
            groups[key] = g

    def q(evs, field, k=5):
        vals = sorted(v for v in (x.get(field) if field != "opyoy" else (x["extra"].get("영업이익") or {}).get("yoy") for x in evs) if v is not None)
        if len(vals) < k * 5:
            return []
        cuts = [vals[int(len(vals) * i / k)] for i in range(1, k)]
        def which(v):
            return sum(1 for c in cuts if v >= c)
        return [(i, [x for x in evs if (x.get(field) if field != "opyoy" else (x["extra"].get("영업이익") or {}).get("yoy")) is not None and which(x.get(field) if field != "opyoy" else x["extra"]["영업이익"]["yoy"]) == i]) for i in range(k)]

    add("잠정실적 전체", E)
    for i, evs in q(E, "ear"):
        add(f"잠정실적 EAR {i + 1}/5분위{'(최저)' if i == 0 else '(최고)' if i == 4 else ''}", evs)
    for i, evs in q(E, "opyoy"):
        add(f"잠정실적 영업이익YoY {i + 1}/5분위{'(최저)' if i == 0 else '(최고)' if i == 4 else ''}", evs)
    add("잠정실적 EAR>+5%", [v for v in E if (v.get("ear") or 0) > 5])
    add("잠정실적 EAR<-5%", [v for v in E if v.get("ear") is not None and v["ear"] < -5])
    add("잠정실적 EAR>+5% & 영업이익YoY>+30%", [v for v in E if (v.get("ear") or 0) > 5 and ((v["extra"].get("영업이익") or {}).get("yoy") or 0) > 30])
    add("잠정실적 흑자전환(전년 적자→당기 흑자)", [v for v in E if (v["extra"].get("영업이익") or {}).get("py") is not None and v["extra"]["영업이익"]["py"] < 0 and (v["extra"]["영업이익"].get("cur") or 0) > 0])
    add("잠정실적 적자전환", [v for v in E if (v["extra"].get("영업이익") or {}).get("py") is not None and v["extra"]["영업이익"]["py"] > 0 and (v["extra"]["영업이익"].get("cur") or 0) < 0])
    add("공급계약 전체", C)
    for lo, hi, lab in ((0, 10, "<10%"), (10, 30, "10~30%"), (30, 100, "30~100%"), (100, 1e9, "≥100%")):
        add(f"공급계약 매출대비 {lab}", [v for v in C if lo <= (v["extra"]["ratio_pct"] or 0) < hi])
    add("공급계약 해외", [v for v in C if v["extra"].get("export")])
    add("공급계약 코스닥 & ≥30%", [v for v in C if v["mkt"] == "K" and (v["extra"]["ratio_pct"] or 0) >= 30])
    add("공급계약 EAR>+5%", [v for v in C if (v.get("ear") or 0) > 5])
    add("공급계약 EAR≤0", [v for v in C if v.get("ear") is not None and v["ear"] <= 0])

    out = {"period": [b, e], "generated": dt.datetime.now().isoformat(timespec="minutes"), "groups": groups, "events": events}
    json.dump(out, open(os.path.join(ROOT, "data", "backtest_pead.json"), "w", encoding="utf-8"), ensure_ascii=False)
    L = [f"# 잠정실적(PEAD)·공급계약 공시 백테스트 ({b}~{e})", "",
         "- 진입: 공시 다음 거래일(t+1) 종가. EAR = 공시 전후 2일 초과수익(t−1 종가→t+1 종가, 시장 대비). AR = 종목 − 같은 시장 ETF. 거래비용 미차감.",
         "", "| 그룹 | n | 20일 평균AR | 중앙값 | 승률 | t | 60일 평균AR | 중앙값 | 승률 | t |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, g in groups.items():
        a, c = g.get("h20") or {}, g.get("h60") or {}
        L.append(f"| {k} | {a.get('n', c.get('n'))} | {a.get('mean', '-')} | {a.get('median', '-')} | {a.get('win', '-')}% | {a.get('t', '-')} | {c.get('mean', '-')} | {c.get('median', '-')} | {c.get('win', '-')}% | {c.get('t', '-')} |")
    open(os.path.join(ROOT, "data", "backtest_pead.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L), flush=True)

    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        from notify_telegram import send
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines = [f"<b>잠정실적·공급계약 백테스트</b> ({b[:4]}.{b[4:6]}~{e[:4]}.{e[4:6]}, t+1 진입, 시장 대비)"]
        for k, g in groups.items():
            if "분위" in k and "5/5" not in k and "1/5" not in k:
                continue
            a, c = g.get("h20") or {}, g.get("h60") or {}
            lines.append(f"• {esc(k)}: n={a.get('n')} | 20일 {a.get('mean')}% (승률 {a.get('win')}%, t={a.get('t')}) | 60일 {c.get('mean')}% (승률 {c.get('win')}%, t={c.get('t')})")
        msg = "\n".join(lines)
        for ch in [msg[i:i + 3900] for i in range(0, len(msg), 3900)]:
            send(ch)


if __name__ == "__main__":
    main()
