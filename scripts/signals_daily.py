"""DART 신규 공시 시그널 텔레그램 알림 (장중 매시간 + 아침 07:10).

- 마지막 확인 이후 새로 올라온 공시만 처리(data/signals_seen.json에 rcept_no 기록)
- BUYBACK/CANCEL/INSIDER: 매수 후보 — 조건 충족 건만, 백테스트 통계(있으면)와 함께
- RISK: 경보 — WATCHLIST(data/stocks.json 종목 + 환경변수 WATCH_CODES)에 해당하면 🚨, 그 외는 요약 1줄
환경: DART_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WATCH_CODES(쉼표 구분 종목코드, 선택)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import dart_signals as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEN = os.path.join(ROOT, "data", "signals_seen.json")
KST = dt.timezone(dt.timedelta(hours=9))


def main():
    if not S.KEY:
        print("DART_API_KEY 없음")
        return
    seen = set(json.load(open(SEEN)) if os.path.exists(SEEN) else [])
    now = dt.datetime.now(KST)
    bgn = (now - dt.timedelta(days=2)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    watch = {s["code"] for s in json.load(open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8"))["stocks"]}
    watch |= {c.strip() for c in os.environ.get("WATCH_CODES", "").split(",") if c.strip()}
    bt = {}
    bp = os.path.join(ROOT, "data", "backtest_signals.json")
    if os.path.exists(bp):
        bt = json.load(open(bp, encoding="utf-8")).get("groups", {})

    def stat(key):
        g = bt.get(key, {}).get("h20")
        return f" | 과거 20일 평균 {g['mean']:+.1f}% 승률 {g['win']}% (n={g['n']})" if g else ""

    buy, risk_w, risk_o, new_seen = [], [], [], []
    for detail in ("B001", "I001"):
        for x in S.list_all(bgn, end, detail=detail, max_pages=30):
            rn = x["rcept_no"]
            if rn in seen or not x.get("stock_code"):
                continue
            new_seen.append(rn)
            c = S.classify(x["report_nm"])
            if not c:
                continue
            link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}"
            if c[0] == "BUYBACK":
                try:
                    p = S.parse_buyback(S.doc_text(rn))
                    time.sleep(0.1)
                except Exception:  # noqa: BLE001
                    p = {}
                amt = p.get("amount")
                key = "BUYBACK/소각목적" if p.get("cancel") else "BUYBACK"
                buy.append(f"🟢 <b>{x['corp_name']}</b> 자사주 직접취득{' (소각목적)' if p.get('cancel') else ''}"
                           f"{f' {amt/1e8:,.0f}억' if amt else ''}{f' · 발행주식 {p['ratio_pct']}%' if p.get('ratio_pct') else ''}{stat(key)}\n<a href='{link}'>공시</a>")
            elif c[0] == "CANCEL":
                buy.append(f"🟢 <b>{x['corp_name']}</b> 자기주식 소각 결정{stat('CANCEL')}\n<a href='{link}'>공시</a>")
            elif c[0] == "RISK":
                line = f"{x['corp_name']} — {c[1]} <a href='{link}'>공시</a>"
                (risk_w if x["stock_code"] in watch else risk_o).append(line)
    # 내부자 (원문 파싱 필요 — 신규 건만)
    for x in S.list_all(bgn, end, detail="D002", max_pages=30):
        rn = x["rcept_no"]
        if rn in seen or not x.get("stock_code"):
            continue
        new_seen.append(rn)
        try:
            p = S.parse_insider(S.doc_text(rn))
            time.sleep(0.1)
        except Exception:  # noqa: BLE001
            continue
        if S.insider_ok(p, min_amount=3e8):
            who = p["title"] or p["relation"] or ("최대주주" if p["is_major"] else "임원")
            buy.append(f"🟢 <b>{x['corp_name']}</b> 내부자 장내매수 — {who}{' (최대주주)' if p['is_major'] and '최대주주' not in who else ''} {p['buy_amount']/1e8:,.1f}억{stat('INSIDER/최대주주' if p['is_major'] else 'INSIDER')}\n<a href='https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}'>공시</a>")

    seen |= set(new_seen)
    json.dump(sorted(seen)[-20000:], open(SEEN, "w"))
    if not (buy or risk_w or risk_o):
        print("신규 시그널 없음")
        return
    blocks = []
    if buy:
        blocks.append("<b>매수 후보 공시</b>\n" + "\n".join(buy))
    if risk_w:
        blocks.append("🚨 <b>보유/관심 종목 위험 공시</b>\n" + "\n".join(risk_w))
    if risk_o:
        blocks.append(f"⚠️ 기타 위험 공시 {len(risk_o)}건\n" + "\n".join(risk_o[:8]) + ("\n…" if len(risk_o) > 8 else ""))
    msg = f"<b>DART 시그널 {now.strftime('%m/%d %H:%M')}</b>\n\n" + "\n\n".join(blocks) + "\n\n매수 후보는 백테스트상 우위가 약함(참고용). 위험 공시는 20일 승률 33~40%로 회피 근거 뚜렷. 투자 판단은 본인 책임"
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat:
        from notify_telegram import send
        for chunk in [msg[i:i + 3900] for i in range(0, len(msg), 3900)]:
            send(chunk)
    print(msg)


if __name__ == "__main__":
    main()
