"""배당 매매 타이밍 텔레그램 알림 (매일 07:10 KST, GitHub Actions).

알림 설계 원칙 — 행동이 필요한 날에만, 근거 숫자와 함께:
  ① 오늘 마지막 매수일      : 15:30 장 마감 전 매수·보유 필요 (가장 중요)
  ② 내일 마지막 매수일      : 하루 전 예고
  ③ 오늘 배당락일           : 팔아도 배당 수령. 과거 배당락일 평균 등락·이론 배당락 대비 초과 하락 제시 →
                              "시초가 매도 vs 보유" 판단 근거
  ④ 상승 구간 진입(D-n)     : 과거 평균 저점 시점(배당락 n거래일 전) ±2일에 도달한 종목 — 선취매 검토 시점
  ⑤ 5거래일 전 예고         : 자금 준비
  ⑥ 월요일 주간 브리핑      : 향후 2주 일정 요약
확정(DART 공시) 건은 ✅, 예상 건은 🔸로 표시. 확률 표현은 표본 수(n)를 함께 적는다.

필요 환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (GitHub Secrets)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
import holidays as H  # noqa: E402

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
KST = dt.timezone(dt.timedelta(hours=9))


def send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
        r.read()


def price_now(code: str) -> float | None:
    try:
        from update_prices import fetch_daily
        s = fetch_daily(code, count=3)
        return s[-1][1] if s else None
    except Exception:  # noqa: BLE001
        return None


def won(v):
    return "미공시" if v is None else f"{v:,.0f}원"


def pct(v):
    return "–" if v is None else f"{v:+.2f}%"


def main():
    if not TOKEN or not CHAT:
        print("TELEGRAM_BOT_TOKEN/CHAT_ID 없음 — 알림 건너뜀")
        return
    data = json.load(open(os.path.join(ROOT, "site", "data.json"), encoding="utf-8"))
    today = dt.datetime.now(KST).date()
    if not H.is_trading_day(today) and today.weekday() != 0:
        print("휴장일 — 알림 없음")
        return
    t = today.isoformat()
    nxt = H.next_trading_day(today).isoformat()
    d5 = H.next_trading_day(today, 5).isoformat()
    ev = [e for e in data["events"] if e["confidence"] != "C" and e["ex_date"] >= t]

    def dps_of(e):
        return e["dps"] if e["dps"] is not None else (e["ref_dividends"][0]["dps"] if e["ref_dividends"] else None)

    def line(e, with_yield=False):
        tag = "✅" if e["status"] == "확정" else "🔸"
        d = dps_of(e)
        s = f"{tag} <b>{e['name']}</b> {e['period']} · {'확정' if e['dps'] is not None else '직전'} {won(d)}"
        if with_yield and d:
            p = price_now(e["code"])
            if p:
                s += f" · 현재가 {p:,.0f}원 → 배당률 {d / p * 100:.2f}%"
        return s

    def stats_line(e):
        s = e["ex_stats"]
        if not s:
            return ""
        return (f"   과거 {s['n']}회: 배당락일 평균 {pct(s['avg_ex_day_pct'])}, 저점→배당락 전일 {pct(s['avg_runup_pct'])}"
                f" (D-{round(s['avg_runup_days'])}부터), 회복 평균 {round(s['avg_recover_days']) if s['avg_recover_days'] is not None else '–'}거래일")

    blocks = []
    buy_today = [e for e in ev if e["last_buy_date"] == t]
    if buy_today:
        blocks.append("🛒 <b>오늘 마지막 매수일 — 15:30 장 마감까지 보유해야 배당</b>\n" +
                      "\n".join(line(e, True) + f"\n   배당락 {e['ex_date'][5:]} · 기준일 {e['record_date'][5:]}" + stats_line(e) for e in buy_today))
    buy_tmrw = [e for e in ev if e["last_buy_date"] == nxt]
    if buy_tmrw:
        blocks.append("⏰ <b>내일이 마지막 매수일</b>\n" + "\n".join(line(e, True) for e in buy_tmrw))
    ex_today = [e for e in ev if e["ex_date"] == t]
    if ex_today:
        rows = []
        for e in ex_today:
            s = e["ex_stats"]
            hint = ""
            if s and s.get("avg_ex_day_pct") is not None:
                d = dps_of(e)
                p = price_now(e["code"])
                theory = -d / p * 100 if (d and p) else None
                if theory is not None:
                    hint = (f"\n   이론 배당락 {pct(theory)} vs 과거 실제 평균 {pct(s['avg_ex_day_pct'])} → "
                            + ("과거엔 배당금보다 덜 빠짐: 시초가 매도도 유리했음" if s["avg_ex_day_pct"] > theory else "과거엔 배당금 이상 빠짐: 회복까지 보유 고려")
                            + f" (n={s['n']})")
            rows.append(line(e) + "\n   오늘 팔아도 배당은 받습니다." + hint)
        blocks.append("📉 <b>오늘 배당락일</b>\n" + "\n".join(rows))
    # 상승 구간 진입: 과거 평균 저점 시점 ±2거래일
    entry = []
    for e in ev:
        s = e["ex_stats"]
        if not s or s["n"] < 2 or s["avg_runup_pct"] is None or s["avg_runup_pct"] <= 0:
            continue
        n = round(s["avg_runup_days"])
        if n >= 40:
            continue  # 조회 구간 한계에 걸린 값은 신호로 쓰지 않음
        target = H.prev_trading_day(dt.date.fromisoformat(e["ex_date"]), n)
        if abs((target - today).days) <= 2 and e["ex_date"] > t:
            entry.append((e, n))
    if entry:
        blocks.append("📈 <b>과거 패턴상 '배당락 전 상승' 시작 구간</b> (선취매 검토)\n" +
                      "\n".join(line(e, True) + f"\n   과거 {e['ex_stats']['n']}회 평균 D-{n}부터 {pct(e['ex_stats']['avg_runup_pct'])} 상승 · 마지막 매수 {e['last_buy_date'][5:]}" for e, n in entry))
    pre5 = [e for e in ev if nxt < e["last_buy_date"] <= d5]
    if pre5:
        blocks.append("📅 <b>5거래일 내 마지막 매수일</b>\n" + "\n".join(line(e) + f" · {e['last_buy_date'][5:]}" for e in pre5))
    if today.weekday() == 0:
        two = (today + dt.timedelta(days=14)).isoformat()
        wk = [e for e in ev if t <= e["last_buy_date"] <= two]
        if wk:
            blocks.append("🗓 <b>2주 내 일정</b>\n" + "\n".join(f"{e['last_buy_date'][5:]} 매수마감 → {e['ex_date'][5:]} 배당락 · {line(e)}" for e in wk))

    if not blocks:
        print("오늘 알림 없음")
        return
    msg = f"<b>배당 캘린더 알림 {t}</b>\n\n" + "\n\n".join(blocks) + \
        "\n\n✅확정(DART) 🔸예상(패턴) · 통계는 과거 실적, 투자 판단은 본인 책임\nhttps://cncnature.github.io/kr-dividend-calendar/"
    for chunk in [msg[i:i + 3900] for i in range(0, len(msg), 3900)]:
        send(chunk)
    print("sent", len(msg))


if __name__ == "__main__":
    main()
