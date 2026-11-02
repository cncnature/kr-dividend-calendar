# 국내 배당 캘린더 (kr-dividend-calendar)

종목별 **마지막 매수일 → 배당락일 → 배당기준일**과 **직전 배당액**, **배당락 전후 주가 패턴**(상승 시작 시점·상승폭·배당락일 하락률·회복 기간)을 한 페이지에서 보는 캘린더.
2027년은 한국거래소 공지 휴장일 기준으로 산출하고, 2028년 이후는 공휴일 규정으로 자동 산출한 뒤 GitHub Actions가 매일 DART 공시·주가를 받아 갱신한다.

## 구조

```
data/stocks.json        종목별 기준일 규칙·최근 배당금 시드 (수동 편집 — 종목 추가는 여기에)
data/confirmed.json     DART 공시로 확정된 기준일·배당금 (자동 생성)
data/price_stats.json   배당락 전후 주가 통계 (자동 생성)
scripts/holidays.py     KRX 휴장일 (2026·2027 공지본 + 2028~ 자동 생성)
scripts/build.py        기준일 → 배당락 → 마지막 매수일 계산, site/data.json 생성
scripts/update_dart.py  OpenDART '현금·현물배당결정' 수집
scripts/update_prices.py 네이버 일봉으로 배당락 통계 계산
scripts/notify_telegram.py 매매 타이밍 텔레그램 알림
site/index.html         캘린더 웹페이지 (GitHub Pages)
.github/workflows/update.yml  매일 07:10 KST 자동 갱신 + Pages 배포
```

## 날짜 계산 근거

- 국내 주식은 T+2 결제이므로 **배당기준일 2거래일 전**까지 매수해야 주주명부에 오른다.
- 배당락일 = 기준일(휴장일이면 직전 거래일)의 직전 거래일, 마지막 매수일 = 배당락일의 직전 거래일.
- 검증: 2025-12-31 기준일 → 배당락 12/29, 매수마감 12/26 (KRX 공지와 일치), 2026-02-28(토) 기준일 → 배당락 2/26, 매수마감 2/25 (현대차 공시와 일치) 등 7개 사례 일치.
- 휴장일: 관공서의 공휴일에 관한 규정(대체공휴일 포함, 2026년부터 노동절·제헌절 편입), 연말휴장일(12/31, 주말이면 직전 평일), 공직선거일.
  2027년 자동 산출값이 KRX 공지와 100% 일치함을 확인했다.

## 배당기준일 규칙 유형

| type | 의미 | 예 |
|---|---|---|
| `eom` | 지정 월 말일이 기준일 | 삼성전자 3·6·9·12월, 현대차 2·5·8·11월 |
| `fixed` | 관행적 월·일 (이사회 결의로 확정, ±1~2주 변동) | KB금융 2/27·5/8·8/7·11/6 |

`confidence` A(정관·법령으로 고정) / B(최근 공시 패턴) / C(불확실, 기본 숨김). DART 공시가 수집되면 해당 건은 **확정**으로 표시된다.

## 설정

1. 이 폴더를 GitHub 저장소(main)로 푸시.
2. Settings → Pages → Source: **GitHub Actions**.
3. [OpenDART](https://opendart.fss.or.kr)에서 무료 API 키 발급 → Settings → Secrets → `DART_API_KEY`.
4. 텔레그램 알림: Secrets에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 추가 (매일 07:10 매수마감·배당락·상승구간 알림).
5. Actions 탭에서 "배당 캘린더 갱신" 수동 실행(workflow_dispatch) → 이후 매일 자동.

## 로컬 실행

```bash
pip install korean_lunar_calendar
python scripts/build.py            # site/data.json 생성
python -m http.server -d site 8000 # http://localhost:8000
```

## 주의

- 배당액은 참고용(직전 공시)이며 미래 배당을 보장하지 않는다. 배당락 통계는 과거 실적이다.
- 분기배당 기준일만 먼저 공고하고 금액은 이사회 후 결정하는 종목(삼성전자 등)은 기준일 확정 후에도 금액이 null일 수 있다.
