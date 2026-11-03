"""관세청 품목별·국가별 수출입실적(공공데이터포털 1220000/nitemtrade) → 업종별 수출 추세 + 텔레그램 월간 알림.

API 제약(기술문서 v2 기준): 국가코드 필수, 조회기간 1년 이내, XML, 월 1회 갱신(전월분 ~15일경), 30 tps.
→ data/export_items.json 의 주요 수출국을 합산(한국 전체 수출의 약 80~85% 커버). 전년동월비(YoY)는 같은 국가 집합끼리 비교하므로 왜곡이 작다.

출력: data/exports.json (품목별 월별 합산, YoY·MoM·3개월 추세), site/exports.html 데이터는 build 시 복사
알림: 새 월 데이터가 생겼을 때만 텔레그램 1회 (data/exports.json 의 latest_month 와 비교)
환경: DATA_GO_KR_KEY (필수), TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (선택), EXPORT_PROBE=1 이면 국가코드 전체조회 가능 여부만 시험
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get("DATA_GO_KR_KEY", "")
URL = "https://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList?"
OUT = os.path.join(ROOT, "data", "exports.json")


def fetch(hs: str, cnty: str, bgn: str, end: str) -> dict[str, tuple[float, float]]:
    """{YYYY.MM: (수출금액$, 수출중량kg)} — '총계' 행 제외."""
    q = dict(serviceKey=KEY, strtYymm=bgn, endYymm=end, hsSgn=hs, cntyCd=cnty)
    url = URL + urllib.parse.urlencode(q)
    for i in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                raw = r.read()
            break
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(1 + i)
    root = ET.fromstring(raw)
    if root.tag == "OpenAPI_ServiceResponse":  # 포털 오류(키 미등록·한도 초과 등)
        raise RuntimeError(root.findtext(".//returnAuthMsg") or raw[:200].decode("utf-8", "ignore"))
    code = root.findtext(".//resultCode")
    if code != "00":
        raise RuntimeError(f"{code} {root.findtext('.//resultMsg')}")
    out = {}
    for it in root.iter("item"):
        y = (it.findtext("year") or "").strip()
        if not y or y == "총계":
            continue
        out[y] = (float(it.findtext("expDlr") or 0), float(it.findtext("expWgt") or 0))
    return out


def month_windows(n_months: int = 26) -> list[tuple[str, str]]:
    """최근 n개월을 12개월 이하 창으로 분할 (조회기간 1년 제한)."""
    today = dt.date.today().replace(day=1)
    months = []
    cur = today
    for _ in range(n_months):
        months.append(cur)
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
    months.reverse()
    wins = []
    for i in range(0, len(months), 12):
        chunk = months[i:i + 12]
        wins.append((chunk[0].strftime("%Y%m"), chunk[-1].strftime("%Y%m")))
    return wins


def main():
    if not KEY:
        print("DATA_GO_KR_KEY 없음")
        return
    cfg = json.load(open(os.path.join(ROOT, "data", "export_items.json"), encoding="utf-8"))
    countries, items = cfg["countries"], cfg["items"]
    wins = month_windows()
    print("조회 창:", wins, "국가:", len(countries), "품목:", len(items), flush=True)

    if os.environ.get("EXPORT_PROBE"):
        for c in ("", "ALL", "00", "ZZ"):
            try:
                r = fetch("8542", c, wins[-1][0], wins[-1][1])
                print(f"cntyCd={c!r}: {len(r)}개월 {list(r.items())[:2]}")
            except Exception as exc:  # noqa: BLE001
                print(f"cntyCd={c!r}: 오류 {exc}")
        return

    prev = json.load(open(OUT, encoding="utf-8")) if os.path.exists(OUT) else {}
    prev_latest = prev.get("latest_month")

    jobs = [(it["hs"], c, b, e) for it in items for c in countries for b, e in wins]
    results: dict[tuple, dict] = {}
    errors = 0

    def work(j):
        try:
            return j, fetch(*j)
        except Exception as exc:  # noqa: BLE001
            return j, {"_err": str(exc)[:80]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        for j, r in ex.map(work, jobs):
            if "_err" in r:
                errors += 1
                if errors <= 5:
                    print("오류", j[:2], r["_err"], flush=True)
                continue
            results[j] = r
    print(f"요청 {len(jobs)}건, 오류 {errors}건", flush=True)
    if errors > len(jobs) * 0.3:
        print("오류가 많아 중단 (키·한도 확인)")
        sys.exit(1)

    series: dict[str, dict[str, float]] = {}  # hs → {YYYY.MM: $}
    by_country: dict[str, dict[str, dict[str, float]]] = {}  # hs → cnty → {월: $}
    for (hs, c, _b, _e), r in results.items():
        for m, (usd, _kg) in r.items():
            hs_s = series.setdefault(hs, {})
            hs_s[m] = hs_s.get(m, 0) + usd
            c_s = by_country.setdefault(hs, {}).setdefault(c, {})
            c_s[m] = c_s.get(m, 0) + usd

    # 최신 '완성된' 월: 모든 품목에 값이 있는 가장 최근 월 (관세청은 전월분을 ~15일경 반영)
    months_all = sorted({m for s in series.values() for m in s})
    latest = months_all[-1] if months_all else None

    def yoy(s, m):
        y, mm = m.split(".")
        pm = f"{int(y) - 1}.{mm}"
        return (s[m] / s[pm] - 1) * 100 if s.get(pm) and s.get(m) else None

    def mom(s, m):
        i = months_all.index(m)
        if i == 0:
            return None
        pm = months_all[i - 1]
        return (s[m] / s[pm] - 1) * 100 if s.get(pm) and s.get(m) else None

    rows = []
    for it in items:
        s = series.get(it["hs"], {})
        if not s or latest not in s:
            continue
        last3 = [m for m in months_all if m <= latest][-3:]
        yoys = [yoy(s, m) for m in last3]
        yoys_ok = [v for v in yoys if v is not None]
        # 연속 증가(전년비 양수) 개월 수
        streak = 0
        for m in reversed([m for m in months_all if m <= latest]):
            v = yoy(s, m)
            if v is None or v <= 0:
                break
            streak += 1
        # 국가별 기여(최신월 YoY 상위)
        top = []
        for c, cs in by_country.get(it["hs"], {}).items():
            v = yoy(cs, latest)
            if cs.get(latest) and v is not None:
                top.append((c, cs[latest], v))
        top.sort(key=lambda t: -t[1])
        rows.append({
            "hs": it["hs"], "name": it["name"], "stocks": it["stocks"],
            "latest_usd": round(s[latest]), "yoy": None if yoy(s, latest) is None else round(yoy(s, latest), 1),
            "mom": None if mom(s, latest) is None else round(mom(s, latest), 1),
            "yoy_3m_avg": round(sum(yoys_ok) / len(yoys_ok), 1) if yoys_ok else None,
            "streak_up": streak,
            "top_countries": [{"cnty": c, "usd": round(u), "yoy": round(v, 1)} for c, u, v in top[:5]],
            "series": {m: round(v) for m, v in sorted(s.items())},
        })
    rows.sort(key=lambda r: -(r["yoy"] if r["yoy"] is not None else -999))

    out = {"generated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
           "latest_month": latest, "countries": countries, "source": "관세청 품목별 국가별 수출입실적(공공데이터포털), 주요 수출국 합산",
           "items": rows}
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("최신월", latest, "품목", len(rows), flush=True)
    for r in rows:
        print(f"{r['name']:14s} {r['latest_usd']/1e8:7.1f}억$ YoY {r['yoy']:+6.1f}% MoM {r['mom'] if r['mom'] is None else round(r['mom'],1)} 3mYoY {r['yoy_3m_avg']} 연속↑{r['streak_up']}")

    # 텔레그램: 새 달 데이터일 때만
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tok and chat and latest and latest != prev_latest:
        from notify_telegram import send
        up = [r for r in rows if r["yoy"] is not None and r["yoy"] > 0]
        dn = [r for r in rows if r["yoy"] is not None and r["yoy"] <= 0]
        L = [f"<b>📦 {latest} 품목별 수출 (관세청, 주요 {len(countries)}개국 합산)</b>"]
        L.append("<b>증가</b>")
        for r in up:
            st_ = f" · {r['streak_up']}개월 연속↑" if r["streak_up"] >= 3 else ""
            L.append(f"▲ {r['name']} {r['latest_usd']/1e8:,.0f}억$ 전년비 <b>{r['yoy']:+.1f}%</b> (3개월 평균 {r['yoy_3m_avg']:+.1f}%){st_}\n   관련: {', '.join(r['stocks'].values())}")
        L.append("<b>감소</b>")
        for r in dn:
            L.append(f"▼ {r['name']} {r['latest_usd']/1e8:,.0f}억$ 전년비 <b>{r['yoy']:+.1f}%</b> (3개월 평균 {r['yoy_3m_avg']:+.1f}%)\n   관련: {', '.join(r['stocks'].values())}")
        L.append("수출 증가가 곧 주가 상승을 뜻하지는 않음(이미 반영된 경우 多). 3개월 이상 연속 증가 + 가속(YoY > 3개월 평균)이면 업황 개선 신호로 참고.")
        msg = "\n".join(L)
        for chunk in [msg[i:i + 3900] for i in range(0, len(msg), 3900)]:
            send(chunk)
        print("텔레그램 발송")


if __name__ == "__main__":
    main()
