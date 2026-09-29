# S&P 500 + KOSPI 급락 모니터

매일 07:30 KST에 두 시장의 **직전 거래일 대비 정규장 종가** 급락 종목을 찾아 Gmail로 보고합니다.

| 시장 | 대상 | 탐지 조건 (반올림 전 raw 값) |
|---|---|---|
| US | S&P 500 현재 구성종목 (Wikipedia, 실패 시 캐시 + WARNING) | `(close / prev_close - 1) * 100 <= -10.0` |
| KR | KOSPI 상장 주식(보통주·우선주·리츠 등, ETF/ETN 제외) | `(close / prev_close - 1) * 100 <= -7.0` |

`round()`는 표시에만 사용합니다. 예: -9.996%는 `-10.00%`로 보이지만 탐지하지 않습니다.

## 거래일

시장별로 독립 판정합니다(`market_calendar.py`, `kospi.get_kospi_session_context`).

- US: NYSE 캘린더(휴장·조기폐장·DST). 마감 후 1시간이 지난 세션만 분석. 월요일 세션의 직전 거래일은 금요일.
  미국 휴장일 아침에는 최근 완료 세션을 다시 분석하고 본문에 안내(notice)합니다.
- KR: KOSPI 지수의 **실제 거래일 이력**으로 판정하고 XKRX 캘린더와 교차 확인합니다(불일치 시 WARNING).
  지수 이력 조회가 실패하면 XKRX 캘린더로 대체(WARNING)합니다.

## 가격 데이터

### US

1. Primary: Yahoo v8 chart 일봉 `quote.close` (`includePrePost=false`, `adjclose` 미사용). 반환된 마지막 행이 아니라
   **분석일/직전 거래일 날짜로 행을 선택**하고, 정규장 시간 밖 timestamp·휴장일 행·중복·ticker 불일치·null 종가·
   분석일 거래량 0·양일 동일 OHLCV(stale)를 거부합니다. 분할은 분할조정 종가 기준으로 계산하고 표시합니다.
2. 독립 교차검증: Nasdaq historical "Close/Last"(공식 정규장 종가)로 **전 종목** 양일 종가를 대조합니다.
   Yahoo 실패 종목은 Nasdaq 값으로 대체(fallback)합니다. 두 소스가 다르면 표시하고, 어느 한쪽이라도 기준을
   충족하면 탐지합니다(누락 방지 우선). Nasdaq이 전체적으로 막히면 25회 연속 실패 후 교차검증을 중단하고 안내합니다.

### KR

Naver/Daum의 일별 "종가"는 Nextrade(NXT) 도입 이후 **KRX+NXT 통합 최종 체결가(시간외 20:00까지 포함)** 입니다.
2026-09-28/29 전 종목 대조에서 Naver 종가가 KRX 기준가(=전일 KRX 종가)와 일치한 비율은 21%뿐이었습니다.
따라서:

1. Primary: Yahoo `<code>.KS` 일봉 = KRX 정규장 종가 (Naver KRX 기준가와 1,873/1,886 일치 확인).
2. 교차검증: Naver 일별 행의 `closePrice - compareToPreviousClosePrice` = 그날의 **KRX 기준가**로 전일 종가를 전 종목 검증.
3. 무거래(거래정지) 종목: 종가 = KRX 기준가, 등락 0%로 유효 처리.
4. Fallback: Yahoo 실패 시 KRX 기준가 + Naver 통합 최종가(근사치, NXT 포함 가능)로 계산하고 `대체`로 표시.
5. ±30% 초과 변동(정리매매·신규/재상장)은 제외하지 않고 탐지 + 확인 필요로 표시.
6. 선택: `KRX_API_KEY`(KRX Open API 공식 종가), `KIS_VALIDATE=1`(KIS 국내주식기간별시세, `J`/원주가)이 설정되면 추가 대조.

KOSPI 목록은 FAIL-SOFT입니다. Naver `totalCount`와 수신 건수가 다르면 최대 3회 재조회해 합집합을 만들고,
`expected / loaded / missing-unresolved`를 로그·메일에 남긴 뒤 나머지를 분석합니다. 수신률 98% 미만이거나
주식이 500개 미만이면 목록 자체가 깨진 것으로 보고 FAILED입니다. 종목 수는 하드코딩하지 않습니다.

## Coverage / 상태

- **Valid Prices**: 양일 정규장 종가가 날짜 일치·양수·비-stale로 검증된 종목 수. **Coverage = Valid / Listing.**
- **OK**: 전 종목 유효, 대체·검증구간 불일치·경고 없음.
- **WARNING**: 일부 누락, fallback, 검증 구간(US -8%, KR -5%) 안의 소스 불일치/검증 불가, 목록·캘린더 경고.
  검증된 종목 기준 결과는 신뢰 가능하며 누락 목록을 함께 보고합니다.
- **FAILED**: Coverage 90% 미만, 데이터 0, 목록 붕괴, 실행시간 초과 등 시장 전체를 신뢰할 수 없을 때만.
  이 경우에도 확인된 급락 종목은 `(불완전)`으로 표시합니다.

종료 코드는 FAILED 시장이 있을 때만 1입니다(WARNING은 보고서로 전달).

메일 제목:

```
[급락 모니터] S&P500 2개 · KOSPI 5개
[급락 모니터][DATA WARNING] S&P500 2개 · KOSPI 5개 | 데이터 누락 US 1건 / KR 2건
[급락 모니터][FAILED US] S&P500 확인필요 · KOSPI 5개 | 데이터 누락 US 확인불가 / KR 0건
```

본문 시장 카드: 분석일 · 이전 거래일 · Listing · Valid Prices · Coverage · Fallback · Missing · Detected.

## 로그

GitHub Actions 로그에 시장별 `SUMMARY analysis_date previous_trading_date listing_count valid_price_count
missing_price_count fallback_count mismatch_count cross_checked candidate_count final_alert_count coverage status`,
검증 구간 이하 모든 후보(`ticker | company | prev | close | raw_change | data_source | validation | note`),
누락 종목 전부(`MISSING code name | reason`)를 남깁니다. 전체 가격표는 artifact `combined-*.json`에 있습니다.

## 재시도 / 시간 제한

요청 timeout (5s 연결, 10s 읽기), 최대 3회 exponential backoff(0.5s, 1s). 실패 종목은 3초 뒤 1회 재조회,
공급자가 세션을 아직 게시하지 않은 것으로 보이면(실패 10% 초과) 60초 간격 최대 2회 재시도.
US·KR은 별도 프로세스로 병렬 실행되며 시장별 480초 hard deadline, job 20분 제한.

## GitHub 설정

| Secret / Variable | 역할 |
|---|---|
| GMAIL_ADDRESS, GMAIL_APP_PASSWORD, ALERT_EMAIL_RECIPIENT | 필수 (메일) |
| KRX_API_KEY | 선택 — KRX 공식 종가 교차검증 |
| KIS_APP_KEY, KIS_APP_SECRET + Variable `KIS_VALIDATE=1` | 선택 — KIS 교차검증 |

cron `30 22 * * 1-5` = KST 화~토 07:30 (US 마감 후 EDT 2.5h / EST 1.5h). 수동 실행은 `send_email=false`가 기본이며
`market`(all/us/kr), 날짜, 1회용 수신자 지정 입력을 지원합니다.

## 실행 / 검증

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe main.py --dry-run                       # 전체 (메일 발송 없음)
.venv\Scripts\python.exe main.py --dry-run --market us --session-date-us 2026-09-28
.venv\Scripts\python.exe main.py --dry-run --market kr --session-date-kr 2026-09-23
$env:RUN_LIVE_TESTS = '1'; .venv\Scripts\python.exe -m pytest tests/test_live_data.py -q; Remove-Item Env:RUN_LIVE_TESTS
```

`tests/test_requirements.py`가 운영 요구사항 TEST 1–18을 하나씩 검증합니다. 2026-09-28 US 회귀 데이터는
`tests/fixtures/us-2026-09-28-closes.csv`(Yahoo·Nasdaq 실제 종가 503종목)입니다.
원인 분석은 [docs/root-cause-2026-09-29.md](docs/root-cause-2026-09-29.md)를 참조하십시오.
