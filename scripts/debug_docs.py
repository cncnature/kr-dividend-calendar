"""파서 점검용: 최근 공시 원문에서 핵심 구간을 그대로 출력 (Actions 로그로 확인)."""
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
import dart_signals as S  # noqa: E402

end = dt.date.today()
b = (end - dt.timedelta(days=20)).strftime("%Y%m%d")
e = end.strftime("%Y%m%d")

print("=== B001 report_nm 종류 ===")
names = {}
for x in S.list_all(b, e, detail="B001", max_pages=5):
    n = re.sub(r"\s", "", x["report_nm"])
    names[n] = names.get(n, 0) + 1
for n, c in sorted(names.items(), key=lambda t: -t[1])[:40]:
    print(c, n)

print("\n=== 자사주취득 원문 ===")
k = 0
for x in S.list_all(b, e, detail="B001", max_pages=5):
    if "자기주식취득결정" in x["report_nm"].replace(" ", "") and "신탁" not in x["report_nm"]:
        t = S.doc_text(x["rcept_no"])
        i = t.find("취득예정")
        print(x["corp_name"], x["rcept_no"], "\n", t[max(0, i - 200): i + 900], "\n---")
        k += 1
        if k >= 2:
            break

print("\n=== 임원·주요주주 보고서 원문 (장내매수 포함 건) ===")
k = 0
for x in S.list_all(b, e, detail="D002", max_pages=10):
    t = S.doc_text(x["rcept_no"])
    if "장내매수" in t:
        i = t.find("장내매수")
        print(x["corp_name"], x["rcept_no"], "\n[HEAD]", t[:1500], "\n[ROWS]", t[max(0, i - 600): i + 700], "\n---")
        k += 1
        if k >= 3:
            break
