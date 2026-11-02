"""KRX 휴장일 계산 모듈.

- 2027년: 한국거래소가 공지한 휴장일 목록을 그대로 사용(확정).
- 2028년 이후: 「관공서의 공휴일에 관한 규정」 + 대체공휴일 규칙 + 거래소 관행(연말휴장일)으로 자동 생성(예상).
  KRX가 매년 12월에 다음 해 휴장일을 공지하면 OFFICIAL 딕셔너리에 추가하면 된다.

근거
- 관공서의 공휴일에 관한 규정 제2조·제3조(대체공휴일): 설날·추석 연휴가 일요일/다른 공휴일과 겹치면 그 다음 비공휴일,
  삼일절·광복절·개천절·한글날·어린이날·부처님오신날·기독탄신일·노동절·제헌절이 토·일·다른 공휴일과 겹치면 그 다음 비공휴일.
  (2026년부터 노동절·제헌절 공휴일 편입, 대체공휴일 적용)
- 한국거래소 유가증권시장 업무규정: 12월 31일(토·일이면 직전 거래일) 연말휴장일, 공직선거일 휴장.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

from korean_lunar_calendar import KoreanLunarCalendar

# ── 거래소 공지 확정 휴장일 ────────────────────────────────────────────
# 출처: 한국거래소 휴장일 공지(2027), ETF쇼핑 정리본(2026-10-15 확인)
OFFICIAL: dict[int, dict[str, str]] = {
    2026: {
        "2026-01-01": "신정", "2026-02-16": "설날", "2026-02-17": "설날", "2026-02-18": "설날",
        "2026-03-02": "삼일절 대체휴일", "2026-05-01": "노동절", "2026-05-05": "어린이날",
        "2026-05-25": "부처님오신날 대체휴일", "2026-06-03": "전국동시지방선거", "2026-07-17": "제헌절",
        "2026-08-17": "광복절 대체휴일", "2026-09-24": "추석", "2026-09-25": "추석",
        "2026-10-05": "개천절 대체휴일", "2026-10-09": "한글날", "2026-12-25": "성탄절", "2026-12-31": "연말휴장일",
    },
    2027: {
        "2027-01-01": "신정", "2027-02-08": "설날", "2027-02-09": "설날 대체휴일", "2027-03-01": "삼일절",
        "2027-05-03": "노동절 대체휴일", "2027-05-05": "어린이날", "2027-05-13": "부처님오신날",
        "2027-07-19": "제헌절 대체휴일", "2027-08-16": "광복절 대체휴일",
        "2027-09-14": "추석", "2027-09-15": "추석", "2027-09-16": "추석",
        "2027-10-04": "개천절 대체휴일", "2027-10-11": "한글날 대체휴일",
        "2027-12-27": "성탄절 대체휴일", "2027-12-31": "연말휴장일",
    },
}

# 공직선거일(법정공휴일 → 휴장). 확정 공고 전까지는 선거법상 일정으로 예상.
ELECTIONS: dict[str, str] = {
    "2028-04-12": "제23대 국회의원선거(예상)",
    "2030-06-05": "전국동시지방선거(예상)",
    "2030-03-13": "제22대 대통령선거(예상)",  # 공직선거법 제34조: 임기만료일 전 70일 이후 첫 수요일 — 확정 공고 시 수정
}


def _lunar(y: int, m: int, d: int) -> dt.date:
    c = KoreanLunarCalendar()
    c.setLunarDate(y, m, d, False)
    return dt.date.fromisoformat(c.SolarIsoFormat())


def _generate(year: int) -> dict[str, str]:
    """공휴일 규정 기반 자동 생성(예상)."""
    fixed = {
        (1, 1): "신정", (3, 1): "삼일절", (5, 1): "노동절", (5, 5): "어린이날", (6, 6): "현충일",
        (7, 17): "제헌절", (8, 15): "광복절", (10, 3): "개천절", (10, 9): "한글날", (12, 25): "성탄절",
    }
    # (날짜, 이름) 목록 — 같은 날짜에 두 공휴일이 겹칠 수 있어 dict 대신 list 사용
    base: list[tuple[dt.date, str]] = [(dt.date(year, m, d), n) for (m, d), n in fixed.items()]
    seol = _lunar(year, 1, 1)
    chuseok = _lunar(year, 8, 15)
    for k in (-1, 0, 1):
        base.append((seol + dt.timedelta(k), "설날"))
        base.append((chuseok + dt.timedelta(k), "추석"))
    base.append((_lunar(year, 4, 8), "부처님오신날"))
    base.sort()

    taken: set[dt.date] = {d for d, _ in base}
    hol: dict[dt.date, str] = {}
    for d, n in base:
        hol.setdefault(d, n)

    # 대체공휴일 규칙(현충일·신정은 대상 아님)
    subst_sat_sun = {"삼일절", "광복절", "개천절", "한글날", "어린이날", "부처님오신날", "성탄절", "노동절", "제헌절"}
    overlap_done: set[dt.date] = set()
    for d, name in base:
        overlap_other = any(x == d and n != name for x, n in base)
        if overlap_other and d in overlap_done:
            continue  # 하루 겹침 → 대체공휴일 하루만 생성
        if overlap_other:
            overlap_done.add(d)
        if name in ("설날", "추석"):
            need = d.weekday() == 6 or overlap_other
        elif name in subst_sat_sun:
            need = d.weekday() >= 5 or overlap_other
        else:
            need = False
        if need:
            nd = d + dt.timedelta(1)
            while nd.weekday() >= 5 or nd in taken:
                nd += dt.timedelta(1)
            hol[nd] = f"{name} 대체휴일"
            taken.add(nd)

    out = {d.isoformat(): n for d, n in hol.items() if d.weekday() < 5}
    # 연말휴장일: 12/31, 토·일이면 직전 평일
    ye = dt.date(year, 12, 31)
    while ye.weekday() >= 5:
        ye -= dt.timedelta(1)
    out[ye.isoformat()] = "연말휴장일"
    for k, v in ELECTIONS.items():
        if k.startswith(str(year)):
            out[k] = v
    return dict(sorted(out.items()))


@lru_cache(maxsize=None)
def holidays(year: int) -> dict[str, str]:
    """해당 연도의 평일 휴장일 {YYYY-MM-DD: 사유}. OFFICIAL에 있으면 그것을 우선."""
    if year in OFFICIAL:
        return OFFICIAL[year]
    return _generate(year)


def is_official(year: int) -> bool:
    return year in OFFICIAL


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in holidays(d.year)


def prev_trading_day(d: dt.date, n: int = 1) -> dt.date:
    """d 이전(제외) n번째 거래일."""
    while n > 0:
        d -= dt.timedelta(1)
        if is_trading_day(d):
            n -= 1
    return d


def next_trading_day(d: dt.date, n: int = 1) -> dt.date:
    while n > 0:
        d += dt.timedelta(1)
        if is_trading_day(d):
            n -= 1
    return d


def last_trading_on_or_before(d: dt.date) -> dt.date:
    while not is_trading_day(d):
        d -= dt.timedelta(1)
    return d


if __name__ == "__main__":
    import sys
    y = int(sys.argv[1]) if len(sys.argv) > 1 else 2028
    for k, v in holidays(y).items():
        print(k, dt.date.fromisoformat(k).strftime("%a"), v)
