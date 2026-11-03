"""DART 공시 기반 매매 시그널 — 공통 모듈 (분류 규칙 + 원문 파서).

시그널 정의 (근거: 국내외 이벤트 스터디에서 반복 확인된 공시 유형)
  BUYBACK   자기주식취득결정(직접취득, 신탁 제외). 소각 목적이면 가중.            → 매수 후보
  CANCEL    자기주식소각결정                                                        → 매수 후보
  INSIDER   임원·주요주주 특정증권등 소유상황보고서 中 '장내매수', 대표이사/최대주주/임원, 1억원 이상 → 매수 후보
  RISK      유상증자결정, 전환사채·신주인수권부사채·교환사채 발행결정, 최대주주변경, 횡령·배임,
            불성실공시법인 지정, 감사의견 비적정                                    → 보유종목 경보(매도/회피)
공시 유형 코드: B001 주요사항보고서 / D002 임원·주요주주특정증권등소유상황보고서 / I001 거래소 수시공시
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ.get("DART_API_KEY", "")
LIST_URL = "https://opendart.fss.or.kr/api/list.json?"
DOC_URL = "https://opendart.fss.or.kr/api/document.xml?"
sys.path.insert(0, os.path.dirname(__file__))


def _get(url: str, retry: int = 3) -> bytes:
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == retry - 1:
                raise
            time.sleep(2 * (i + 1))
    return b""


def list_all(bgn: str, end: str, pblntf_ty: str | None = None, detail: str | None = None, max_pages: int = 400) -> list[dict]:
    """시장 전체 공시 목록 (법인 지정 없이). 유가·코스닥만."""
    out = []
    # corp_code 없이 조회하면 검색기간 3개월 제한 → 80일 단위로 분할
    import datetime as _dt
    b0, e0 = _dt.datetime.strptime(bgn, "%Y%m%d").date(), _dt.datetime.strptime(end, "%Y%m%d").date()
    chunks = []
    cur = b0
    while cur <= e0:
        nxt = min(cur + _dt.timedelta(days=80), e0)
        chunks.append((cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cur = nxt + _dt.timedelta(days=1)
    for corp_cls in ("Y", "K"):
      for cb, ce in chunks:
        page = 1
        while page <= max_pages:
            q = dict(crtfc_key=KEY, bgn_de=cb, end_de=ce, corp_cls=corp_cls, page_no=page, page_count=100, sort="date", sort_mth="asc")
            if pblntf_ty:
                q["pblntf_ty"] = pblntf_ty
            if detail:
                q["pblntf_detail_ty"] = detail
            data = json.loads(_get(LIST_URL + urllib.parse.urlencode(q)))
            if data.get("status") != "000":
                print(f"list_all {corp_cls} {detail} p{page}: status={data.get('status')} {data.get('message')}")
                break
            if page == 1:
                print(f"list_all {corp_cls} {detail} {cb}~{ce}: total={data.get('total_count')}")
            out.extend(data.get("list", []))
            if page >= int(data.get("total_page", 1)):
                break
            page += 1
            time.sleep(0.15)
    return out


def doc_text(rcept_no: str) -> str:
    z = zipfile.ZipFile(io.BytesIO(_get(DOC_URL + urllib.parse.urlencode(dict(crtfc_key=KEY, rcept_no=rcept_no)))))
    raw = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw.decode("utf-8", "ignore")
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt)


def _num(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(",", "").replace("원", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ── 분류 ─────────────────────────────────────────────────────────────
RISK_PATTERNS = [
    ("유상증자", r"유상증자결정"), ("CB발행", r"전환사채권발행결정"), ("BW발행", r"신주인수권부사채권발행결정"),
    ("EB발행", r"교환사채권발행결정"), ("최대주주변경", r"최대주주변경(?!.*철회)"), ("횡령배임", r"횡령.?배임"),
    ("불성실공시", r"불성실공시법인지정(?!예고)"), ("감사의견비적정", r"감사의견.*(거절|한정|부적정)"),
    ("관리종목", r"관리종목지정(?!해제)"),
]


def classify(report_nm: str) -> tuple[str, str] | None:
    """(시그널, 세부유형) 또는 None. 정정·철회·취소 공시는 제외."""
    n = report_nm.replace(" ", "")
    if re.search(r"정정|철회|취소|기재정정", n):
        return None
    if "자기주식취득결정" in n and "신탁" not in n:
        return ("BUYBACK", "직접취득")
    if "자기주식소각결정" in n:
        return ("CANCEL", "소각")
    for sub, pat in RISK_PATTERNS:
        if re.search(pat, n):
            return ("RISK", sub)
    return None


# ── 원문 파서 ─────────────────────────────────────────────────────────
def parse_buyback(txt: str) -> dict:
    g = lambda pat: (re.search(pat, txt) or [None, None])[1]
    amt = _num(g(r"취득예정금액\s*\(?원?\)?\s*(?:보통주식)?\s*([\d,]+)"))
    shares = _num(g(r"취득예정주식\s*\(?주?\)?\s*(?:보통주식)?\s*([\d,]+)"))
    purpose = (g(r"취득목적\s*(.{2,60}?)\s*취득방법") or "").strip()
    method = (g(r"취득방법\s*(.{2,40}?)\s*위탁투자중개업자") or "").strip()
    ratio = _num(g(r"발행주식총수\s*대비\s*비율\s*\(?%?\)?\s*(?:보통주식)?\s*([\d.]+)"))
    purpose = re.sub(r"\s*\d+\.$", "", purpose)
    method = re.sub(r"\s*\d+\.$", "", method)
    return {"amount": amt, "shares": shares, "purpose": purpose, "method": method,
            "cancel": "소각" in purpose, "ratio_pct": ratio}


def parse_insider(txt: str) -> dict:
    """임원·주요주주 보고서. 표 행: 보고사유 | (증권종류) | 변동전 | 증감 | 변동후 | 단가 — 장내매수 행만 합산."""
    g = lambda pat: (re.search(pat, txt) or [None, None])[1]
    rel = (g(r"임원\s*\(등기여부\)\s*([가-힣\s()]{2,20}?)\s*(?:주요주주|직위)") or "").strip()
    title = (g(r"직위\s*(?:\(직책\))?\s*([가-힣\s()]{2,30}?)\s*(?:주요주주|보고자|선임)") or "").strip()
    major = "최대주주" in txt[:5000]
    total_amt = total_qty = 0.0
    for row in re.finditer(r"장내매수\s*\(\+\)\s*(?:\d{4}-\d{2}-\d{2}\s*)?(?:[가-힣]+\s*)?([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)", txt):
        q, p = _num(row.group(2)), _num(row.group(4))
        if q and p and p > 10:
            total_qty += q
            total_amt += q * p
    return {"relation": rel, "title": title, "is_major": major,
            "buy_qty": total_qty, "buy_amount": total_amt, "has_sell": "장내매도" in txt,
            "option": "스톡옵션" in txt or "주식매수선택권" in txt}


def insider_ok(p: dict, min_amount: float = 1e8) -> bool:
    key_person = p["is_major"] or any(k in (p["title"] + p["relation"]) for k in ("대표이사", "회장", "부회장", "사장", "최대주주", "등기임원", "이사", "감사"))
    return p["buy_amount"] >= min_amount and key_person and not p["option"] and not p["has_sell"]


if __name__ == "__main__":
    for nm in ["주요사항보고서(자기주식취득결정)", "주요사항보고서(자기주식취득신탁계약체결결정)", "[기재정정]주요사항보고서(유상증자결정)",
               "주요사항보고서(전환사채권발행결정)", "최대주주변경", "최대주주변경을수반하는주식양수도계약체결", "주요사항보고서(자기주식소각결정)"]:
        print(nm, "→", classify(nm))
