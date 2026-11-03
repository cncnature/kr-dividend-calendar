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
scripts/dart_signals.py   공시 시그널 분류·원문 파서 (자사주·내부자매수·위험공시)
scripts/backtest_signals.py 시장 전체 공시 2년 백테스트 (시장 대비 초과수익·승률)
scripts/signals_daily.py  신규 공시 시그널 텔레그램 알림 (평일 장중 매시간)
.github/workflows/signals.yml 시그널 알림 + 주간 백테스트
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

## DART 공시 시그널 백테스트 요약 (2024.11~2026.11, 자동 갱신: data/backtest_report.md)

진입 = 공시 다음 거래일 종가, 20거래일 보유, 같은 시장 ETF(KODEX200/코스닥150) 대비 초과수익.

| 시그널 | n | 20일 평균AR | 중앙값 | 승률 | 판단 |
|---|---|---|---|---|---|
| 최대주주변경 | 621 | −4.7% | −6.2% | 33% | **회피/축소 근거 뚜렷** (t=−4.4) |
| CB·EB·BW 발행, 유상증자 | 2,289 | 평균은 소수 급등주에 왜곡 | −4.6~−6.4% | 36~40% | 대부분 하락, 회피 근거 |
| 내부자 장내매수(임원·최대주주, 1억↑) | 841 | +1.5% | +1.1% | 54% | 약한 양(+), 2026.6 한 달이 성과 대부분 → 참고용 |
| 자사주 직접취득 | 470 | −0.6% | −0.4% | 48% | 단기 우위 없음 (학계 확인된 효과는 수개월~1년 장기 드리프트) |

→ 텔레그램 알림은 **보유·관심 종목의 위험 공시 경보**가 핵심이고, 매수 후보 공시는 참고 정보로만 제공한다.

## 관세청 품목별 수출 추세 (site/exports.html)

공공데이터포털 [관세청 품목별 국가별 수출입실적](https://www.data.go.kr/data/15100475/openapi.do) API로 `data/export_items.json`의 HS 4단위 품목을 주요 20개국 합산해 월별 수출금액·전년동월비·연속 증가 개월·국가별 기여를 계산한다(API가 국가코드를 요구해 전체 합계는 불가).
매월 13~25일 09:30 KST에 확인해 새 달 데이터가 반영된 날 1회 텔레그램으로 품목별 증감과 관련 종목을 보낸다. 시크릿 `DATA_GO_KR_KEY` 필요(공공데이터포털 일반 인증키, Decoding 키).

## 로컬 실행

```bash
pip install korean_lunar_calendar
python scripts/build.py            # site/data.json 생성
python -m http.server -d site 8000 # http://localhost:8000
```

## 주의

- 배당액은 참고용(직전 공시)이며 미래 배당을 보장하지 않는다. 배당락 통계는 과거 실적이다.
- 분기배당 기준일만 먼저 공고하고 금액은 이사회 후 결정하는 종목(삼성전자 등)은 기준일 확정 후에도 금액이 null일 수 있다.
