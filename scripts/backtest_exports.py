"""관세청 품목별 수출 → 업종 로테이션 백테스트 (2016.01~현재).

가설(근거: 경제적 연결 정보의 느린 확산 — Cohen & Frazzini 2008; 산업 간 교역연결의 수익률 예측 — EMFT 2020):
  m월 수출(전년동월비·가속·연속 증가)이 공개되는 시점(m+1월 15일경) 이후, 해당 품목 관련 종목이 시장 대비 초과수익을 내는가.

설계(보수적)
  신호 시점: m월 데이터는 (m+1)월 16일 이후 첫 거래일 종가에 진입 (관세청 월별 반영 시점 ~15일을 보수적으로 반영)
  보유: 다음 신호까지 1개월(20거래일), 또는 3개월(60거래일)
  수익률: 종목 수익률 − KODEX200(069500) 수익률
  신호 버킷: YoY>0 / YoY≤0, 가속(YoY > 직전 3개월 평균 YoY) / 둔화, 3개월 연속 증가 여부
  보고: 버킷별 n·평균AR·중앙값·승률·t값, 연도별, 품목별
출력: data/exports_history.json(원자료 캐시), data/backtest_exports.json, data/backtest_exports.md, 텔레그램 요약
환경: DATA_GO_KR_KEY, TELEGRAM_*, EXP_BT_FROM(기본 201601)
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics as st
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
import holidays as H  # noqa: E402
import update_exports as X  # noqa: E402
from update_prices import fetch_daily  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HIST = os.path.join(ROOT, "data", "exports_history.json")
FROM = os.environ.get("EXP_BT_FROM", "201601")
T0 = time.time()


def log(*a):
    print(f"[{(time.time() - T0) / 60:5.1f}m]", *a, flush=True)


def windows(frm: str) -> list[tuple[str, str]]:
    y, m = int(frm[:4]), int(frm[4:])
    today = dt.date.today().replace(day=1)
    out = []
    while dt.date(y, m, 1) <= today:
        y2, m2 = y, m + 11
        if m2 > 12:
            y2, m2 = y + 1, m2 - 12
        end = min(dt.date(y2, m2, 1), today)
        out.append((f"{y}{m:02d}", end.strftime("%Y%m")))
        y, m = end.year, end.month + 1
        if m > 12:
            y, m = y + 1, 1
    return out


def load_history(items, countries) -> dict[str, dict[str, float]]:
    """hs → {YYYY.MM: 합산 수출$}. 캐시(hs,cnty,창)에 없는 조합만 조회."""
    cache = json.load(open(HIST, encoding="utf-8")) if os.path.exists(HIST) else {}
    wins = windows(FROM)
    cur_win = wins[-1]  # 진행 중인 창은 항상 다시 조회
    jobs = [(it["hs"], c, b, e) for it in items for c in countries for b, e in wins
            if f"{it['hs']}|{c}|{b}" not in cache or (b, e) == cur_win]
    log(f"창 {len(wins)}개, 요청 {len(jobs)}건 (캐시 {len(cache)}건)")

    def work(j):
        try:
            return j, X.fetch(*j)
        except Exception as exc:  # noqa: BLE001
            return j, {"_err": str(exc)[:80]}

    err = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, (j, r) in enumerate(ex.map(work, jobs)):
            if "_err" in r:
                err += 1
                if err <= 5:
                    log("오류", j, r["_err"])
                continue
            cache[f"{j[0]}|{j[1]}|{j[2]}"] = {m: v[0] for m, v in r.items()}
            if i % 500 == 0:
                json.dump(cache, open(HIST, "w"))
    json.dump(cache, open(HIST, "w"))
    log(f"오류 {err}건")
    series: dict[str, dict[str, float]] = {}
    for k, r in cache.items():
        hs = k.split("|")[0]
        s = series.setdefault(hs, {})
        for m, v in r.items():
            s[m] = s.get(m, 0) + v
    return series


def entry_date(month: str) -> dt.date:
    """m월 데이터 공개 후 진입일: (m+1)월 16일 이후 첫 거래일."""
    y, m = int(month[:4]), int(month[5:])
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return H.next_trading_day(dt.date(y, m, 15))


def ret(series, d0: dt.date, hold: int):
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    if d0 not in dates:
        return None
    i = dates.index(d0)
    if i + hold >= len(closes):
        return None
    return (closes[i + hold] / closes[i] - 1) * 100


def main():
    if not X.KEY:
        print("DATA_GO_KR_KEY 없음")
        return
    cfg = json.load(open(os.path.join(ROOT, "data", "export_items.json"), encoding="utf-8"))
    items, countries = cfg["items"], cfg["countries"]
    series = load_history(items, countries)
    months = sorted({m for s in series.values() for m in s})
    log("월 범위", months[0], "~", months[-1])

    # 가격
    codes = sorted({c for it in items for c in it["stocks"]})
    px = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, s in ex.map(lambda c: (c, fetch_daily(c, count=3200)), codes + ["069500"]):
            px[c] = s
    mkt = px["069500"]
    log("가격", len(px), "종목")

    # 신호·수익
    obs = []
    for it in items:
        s = series.get(it["hs"], {})
        for m in months:
            if m not in s:
                continue
            y, mm = m.split(".")
            pm = f"{int(y) - 1}.{mm}"
            if not s.get(pm):
                continue
            yoy = (s[m] / s[pm] - 1) * 100
            idx = months.index(m)
            prev3 = []
            for k in (1, 2, 3):
                if idx - k >= 0:
                    m2 = months[idx - k]
                    p2 = f"{int(m2[:4]) - 1}.{m2[5:]}"
                    if s.get(m2) and s.get(p2):
                        prev3.append((s[m2] / s[p2] - 1) * 100)
            if len(prev3) < 3:
                continue
            accel = yoy > st.mean(prev3)
            streak = 1 if yoy > 0 else 0
            if streak:
                for v in prev3:
                    if v > 0:
                        streak += 1
                    else:
                        break
            d0 = entry_date(m)
            for code, name in it["stocks"].items():
                for hold in (20, 60):
                    r, rm = ret(px.get(code, []), d0, hold), ret(mkt, d0, hold)
                    if r is None or rm is None:
                        continue
                    obs.append({"hs": it["hs"], "item": it["name"], "code": code, "name": name, "month": m, "entry": d0.isoformat(),
                                "yoy": round(yoy, 1), "accel": accel, "streak": streak, "hold": hold, "ar": round(r - rm, 2), "raw": round(r, 2)})
    log("관측치", len(obs))

    def agg(xs):
        if len(xs) < 2:
            return None
        sd = st.stdev(xs)
        return {"n": len(xs), "mean": round(st.mean(xs), 2), "median": round(st.median(xs), 2),
                "win": round(sum(1 for v in xs if v > 0) / len(xs) * 100, 1),
                "t": round(st.mean(xs) / (sd / math.sqrt(len(xs))), 2) if sd else None}

    groups = {}
    def add(key, f):
        for hold in (20, 60):
            xs = [o["ar"] for o in obs if o["hold"] == hold and f(o)]
            a = agg(xs)
            if a:
                groups.setdefault(key, {})[f"h{hold}"] = a
    add("전체", lambda o: True)
    add("YoY>0", lambda o: o["yoy"] > 0)
    add("YoY≤0", lambda o: o["yoy"] <= 0)
    add("YoY>0 & 가속", lambda o: o["yoy"] > 0 and o["accel"])
    add("YoY>0 & 둔화", lambda o: o["yoy"] > 0 and not o["accel"])
    add("YoY≤0 & 둔화", lambda o: o["yoy"] <= 0 and not o["accel"])
    add("3개월+ 연속 증가", lambda o: o["streak"] >= 3)
    add("3개월+ 연속 증가 & 가속", lambda o: o["streak"] >= 3 and o["accel"])
    add("YoY>+20%", lambda o: o["yoy"] > 20)
    add("YoY<-20%", lambda o: o["yoy"] < -20)
    for y in sorted({o["month"][:4] for o in obs}):
        add(f"연도 {y}: YoY>0&가속", lambda o, y=y: o["month"].startswith(y) and o["yoy"] > 0 and o["accel"])
        add(f"연도 {y}: YoY≤0", lambda o, y=y: o["month"].startswith(y) and o["yoy"] <= 0)
    for it in items:
        add(f"품목 {it['name']}: YoY>0&가속", lambda o, h=it["hs"]: o["hs"] == h and o["yoy"] > 0 and o["accel"])
        add(f"품목 {it['name']}: YoY≤0", lambda o, h=it["hs"]: o["hs"] == h and o["yoy"] <= 0)

    out = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "months": [months[0], months[-1]], "n_obs": len(obs), "groups": groups, "obs": obs}
    json.dump(out, open(os.path.join(ROOT, "data", "backtest_exports.json"), "w", encoding="utf-8"), ensure_ascii=False)
    L = [f"# 수출 추세 → 관련 종목 백테스트 ({months[0]}~{months[-1]})", "",
         "- 진입: 해당 월 수출 데이터 공개 후(익월 16일 이후 첫 거래일) 종가. AR = 종목 − KODEX200. 거래비용 미차감(왕복 ≈0.4%p).",
         "- 가속 = 전년동월비가 직전 3개월 평균보다 높음. 품목별 관련 종목은 data/export_items.json.", "",
         "| 그룹 | n | 1개월 평균AR | 중앙값 | 승률 | t | 3개월 평균AR | 중앙값 | 승률 | t |", "|---|---|---|---|---|---|---|---|---|---|"]
    for k, g in groups.items():
        a, b = g.get("h20") or {}, g.get("h60") or {}
        L.append(f"| {k} | {a.get('n', b.get('n'))} | {a.get('mean', '-')} | {a.get('median', '-')} | {a.get('win', '-')}% | {a.get('t', '-')} | {b.get('mean', '-')} | {b.get('median', '-')} | {b.get('win', '-')}% | {b.get('t', '-')} |")
    open(os.path.join(ROOT, "data", "backtest_exports.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L), flush=True)

    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        from notify_telegram import send
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines = [f"<b>수출 추세 백테스트</b> ({months[0]}~{months[-1]}, 공개 후 진입, KODEX200 대비)"]
        for k in ("전체", "YoY>0 & 가속", "YoY>0 & 둔화", "YoY≤0 & 둔화", "3개월+ 연속 증가 & 가속", "YoY>+20%", "YoY<-20%"):
            g = groups.get(k)
            if g:
                a, b = g.get("h20") or {}, g.get("h60") or {}
                lines.append(f"• {esc(k)}: n={a.get('n')} | 1개월 {a.get('mean')}% (승률 {a.get('win')}%, t={a.get('t')}) | 3개월 {b.get('mean')}% (승률 {b.get('win')}%, t={b.get('t')})")
        lines.append("|t|≥2 이고 승률이 55% 이상인 버킷만 신호로 채택. 상세: data/backtest_exports.md")
        send("\n".join(lines))


if __name__ == "__main__":
    main()
