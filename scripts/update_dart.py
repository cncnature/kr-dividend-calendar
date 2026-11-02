"""DART 공시에서 '현금·현물배당결정' 을 수집해 기준일·배당금을 확정값으로 저장.

- OpenDART API 키 필요 (https://opendart.fss.or.kr, 무료) → 환경변수 DART_API_KEY
- 공시목록 API(list.json)로 종목별 주요사항보고서를 조회하고, 보고서명에 '배당결정'이 포함된 건의
  원문(document.xml, zip)을 받아 '1주당 배당금' '배당기준일' '배당금지급 예정일자' 를 정규식으로 추출.
- 결과: data/confirmed.json  {code: [{period, record_date, dps, pay_date, source, rcept_no, disclosed}]}
  build.py가 이 값을 '확정' 상태로 캘린더에 반영한다.

참고: 분기배당 기준일만 먼저 공고(예: 삼성전자 '분기배당 기준일 안내')하고 금액은 이사회 후 공시하는 경우가
있어 record_date는 있고 dps가 null인 항목도 생길 수 있다.
"""
from __future__ import annotations

import datetime as dt
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
CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key="

sys.path.insert(0, os.path.dirname(__file__))


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def corp_codes() -> dict[str, str]:
    """종목코드 → DART corp_code 매핑 (corpCode.xml zip)."""
    cache = os.path.join(ROOT, "data", "corp_codes.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 30 * 86400:
        return json.load(open(cache, encoding="utf-8"))
    z = zipfile.ZipFile(io.BytesIO(_get(CORP_URL + KEY)))
    xml = z.read(z.namelist()[0]).decode("utf-8")
    m = {}
    for blk in re.findall(r"<list>(.*?)</list>", xml, re.S):
        cc = re.search(r"<corp_code>(\d+)</corp_code>", blk)
        sc = re.search(r"<stock_code>\s*(\d{6})\s*</stock_code>", blk)
        if cc and sc:
            m[sc.group(1)] = cc.group(1)
    json.dump(m, open(cache, "w", encoding="utf-8"))
    return m


def list_disclosures(corp_code: str, bgn: str, end: str) -> list[dict]:
    # 현금·현물배당결정은 거래소 수시공시(I)로 분류됨 — 유형 필터 없이 보고서명으로 거른다
    q = dict(crtfc_key=KEY, corp_code=corp_code, bgn_de=bgn, end_de=end, page_count=100)
    data = json.loads(_get(LIST_URL + urllib.parse.urlencode(q)))
    if data.get("status") != "000":
        return []
    return [x for x in data.get("list", []) if "배당" in x.get("report_nm", "") and "결정" in x.get("report_nm", "") and "자회사" not in x.get("report_nm", "")]


def parse_doc(rcept_no: str) -> dict:
    """공시 원문에서 배당 항목 추출."""
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
    txt = re.sub(r"\s+", " ", txt)

    def grab(pattern):
        m = re.search(pattern, txt)
        return m.group(1).strip() if m else None

    dps = grab(r"1주당\s*배당금\s*\(?원?\)?\s*보통주식\s*([\d,]+)") or grab(r"보통주식\s*1주당\s*배당금[^\d]*([\d,]+)")
    rec = grab(r"배당기준일\s*(\d{4}-\d{2}-\d{2})")
    pay = grab(r"배당금\s*지급\s*예정일자\s*(\d{4}-\d{2}-\d{2})")
    kind = grab(r"배당종류\s*([가-힣]+배당)")
    return {"dps": int(dps.replace(",", "")) if dps else None, "record_date": rec, "pay_date": pay, "kind": kind}


def period_from(record: str, kind: str | None) -> str:
    y, m = int(record[:4]), int(record[5:7])
    if kind and "결산" in kind:
        return f"{y - 1 if m <= 6 else y}-결산"
    q = {1: "결산", 2: "결산", 3: "1Q", 4: "1Q", 5: "1Q", 6: "2Q", 7: "2Q", 8: "2Q", 9: "3Q", 10: "3Q", 11: "3Q", 12: "결산"}[m]
    return f"{y - 1 if q == '결산' and m <= 2 else y}-{q}"


def main():
    if not KEY:
        print("DART_API_KEY 없음 — 확정 데이터 갱신 건너뜀")
        return
    stocks = json.load(open(os.path.join(ROOT, "data", "stocks.json"), encoding="utf-8"))["stocks"]
    path = os.path.join(ROOT, "data", "confirmed.json")
    confirmed = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    codes = corp_codes()
    end = dt.date.today()
    bgn = end - dt.timedelta(days=int(os.environ.get("DART_LOOKBACK_DAYS", "120")))
    for st in stocks:
        cc = codes.get(st["code"]) or codes.get(st.get("parent_code", ""))
        if not cc:
            print(f"[{st['code']}] corp_code 없음")
            continue
        seen = {c["rcept_no"] for c in confirmed.get(st["code"], []) if c.get("rcept_no")}
        for disc in list_disclosures(cc, bgn.strftime("%Y%m%d"), end.strftime("%Y%m%d")):
            rn = disc["rcept_no"]
            if rn in seen:
                continue
            try:
                info = parse_doc(rn)
            except Exception as exc:  # noqa: BLE001
                print(f"[{st['code']}] {rn} parse fail: {exc}")
                continue
            if not info["record_date"]:
                continue
            entry = {
                "period": period_from(info["record_date"], info["kind"]),
                "record_date": info["record_date"], "dps": info["dps"], "pay_date": info["pay_date"],
                "source": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}", "rcept_no": rn,
                "disclosed": disc["rcept_dt"], "report": disc["report_nm"],
            }
            lst = [c for c in confirmed.get(st["code"], []) if c["record_date"] != entry["record_date"]]
            lst.append(entry)
            confirmed[st["code"]] = sorted(lst, key=lambda x: x["record_date"], reverse=True)
            print(f"[{st['name']}] {entry['period']} 기준일 {entry['record_date']} DPS {entry['dps']} ({disc['report_nm']})")
            time.sleep(0.3)
    json.dump(confirmed, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
