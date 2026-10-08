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
  미국 휴장일 아침에는 최근 완료 세션을 다시 분석하고 INFO로 안내합니다.
- KR: KOSPI 지수의 **실제 거래일 이력**으로 판정하고 XKRX 캘린더와 교차 확인합니다(불일치 시 WARNING).
  지수 이력 조회가 실패하면 XKRX 캘린더로 대체(INFO)합니다.

## 가격 데이터 (providers/ 어댑터 + validation.py)

모든 소스는 `providers/` 어댑터가 같은 형태(`PriceObservation`: symbol, source, close, previous_close,
각 필드의 trading date·status·session_type, adjustment, fetched_at)로 반환합니다.

- `close` = 분석일 **정규장 종가**, `previous_close` = 직전 거래일 정규장 종가(분석일 주식 수 기준 = 분할 조정,
  배당 조정 없음). pre/post-market·NXT·Adjusted Close는 이 필드에 들어오지 않습니다.
- **요청 거래일 == 반환 가격 날짜**일 때만 사용합니다. 날짜가 다르면 `STALE_SOURCE`(가격 오류 아님).
- 필드 상태: `OK` / `STALE_SOURCE` / `UNAVAILABLE` / `INVALID_PRICE` / `NOT_PROVIDED`.
- 보조값(`corroborative`): 정의가 조금 다를 수 있는 값(KRX+NXT 통합 최종가, CNBC가 당일 배당락으로 재산정한 전일종가).
  **일치할 때만 검증에 사용**하고, 달라도 불일치로 치지 않습니다.

`validation.assess`는 전일종가와 분석일 종가를 **필드별로** 따로 합의합니다(2개 이상 소스 일치 = VERIFIED,
다수와 크게 다른 단독 값은 이상치로 제외). 종목 상태:
`CROSS_VALIDATED` / `FALLBACK_VALIDATED` / `PRIMARY_ONLY` / `FALLBACK_UNVERIFIED` / `CROSS_SOURCE_MISMATCH` / `PRIMARY_MISSING`.
불일치가 해소되지 않으면 어느 한쪽이라도 기준을 충족할 때 탐지합니다(누락 방지 우선).

### US

| 역할 | 소스 | 필드 |
|---|---|---|
| Primary | Yahoo chart 1d `quote.close` (`includePrePost=false`) | 전일·분석일 |
| Secondary | CNBC 시세(100종목 일괄) `last` + 당일 저녁 `previous_day_closing`(보조값) | 분석일(+전일 보조) |
| Tertiary | Nasdaq historical Close/Last | 전일·분석일 (분석일 행은 밤늦게 게시되는 경우가 많음) |

Nasdaq은 Yahoo+CNBC로 두 필드가 모두 확인되지 않은 종목과 **검증 구간(-8% 이하) 전 종목**만 조회합니다
(IP당 ~200건 연속 요청 시 HTTP 403 → 0.25초 간격, 동시 3개, 연속 25회 실패 시 중단).
CNBC가 다음 세션으로 전환된 스냅샷(`PRE_MKT`/`*_PREV`/전일종가==현재가)은 배당락 재산정 값이므로 `STALE_SOURCE`입니다.
Yahoo 봉은 NYSE 정규장 시간·휴장일·중복·ticker·null·거래량 0·양일 동일(stale)을 검사합니다.

### US 구성종목 변경(티커 변경·인수합병) 자동 처리

위키피디아 구성종목 목록은 기업 이벤트를 약 하루 늦게 반영합니다. 그래서 데이터로 직접 판정합니다
(특정 종목 예외 없음, `market_data.listing_gap`).

- **거래 종료**: 모든 소스의 마지막 거래일이 전일보다 앞서고 전일·분석일 거래가 없음(인수 완료·상장폐지·거래정지)
  → 급락이 있을 수 없으므로 분석 대상에서 제외하고 INFO로 안내합니다. 한 번에 5종목(또는 1%)을 넘으면
  데이터 장애로 보고 제외하지 않고 누락(WARNING)으로 둡니다.
- **티커 변경**: 분석일 거래가 어디에도 없고 거래소가 심볼을 모르는 경우(Nasdaq `Symbol not exists`, CNBC 미인식)
  → Yahoo 검색에서 **회사명이 같은 미국 상장 종목**(다른 현재 구성종목 제외)을 찾아 새 티커로 교차검증 후 분석(INFO).
  못 찾으면 "티커 변경·상장폐지 추정 — 확인 필요"(WARNING).
- **신규 상장 첫 거래일**(스핀오프·신규 상장): 분석일이 그 종목의 첫 거래일이면 어느 소스에도 전일 종가가 없습니다.
  Yahoo가 밝히는 첫 거래일(`firstTradeDate`)이 전일 이후이고 전일 종가를 가진 소스가 없을 때만
  "신규 상장 첫 거래일"(INFO)로 분류해 분석에서 제외합니다(`market_data.new_listing`). 근거 없는 공백은 그대로 누락(WARNING)이며,
  한 번에 5종목(또는 1%)을 넘으면 마찬가지로 제외하지 않습니다. 다음 거래일부터는 일반 종목처럼 분석합니다.
- CNBC가 같은 티커를 다른 시장 종목으로 연결하면(예: WBD → 이탈리아 Webuild, EUR) 사용하지 않습니다(국가·통화 확인).

### KR

Naver/Daum의 일별 "종가"는 Nextrade(NXT) 도입 이후 **KRX+NXT 통합 최종가(시간외 20:00까지)** 입니다
(2026-09-29 기준 Naver 종가 = KRX 종가 207/944뿐). 정규장 종가 소스:

| 역할 | 소스 | 필드 |
|---|---|---|
| Primary | Yahoo `<code>.KS` 1d (익일 KRX 기준가와 913/913 일치 확인) | 전일·분석일 |
| Secondary | Daum 시세 스냅샷 `regularTradePrice`(NXT 제외)·`prevClosingPrice`·`stockState` | 분석일(+전일) + 시장상태 |
| Tertiary | Naver 일별 `closePrice - compareToPreviousClosePrice` = 분석일 KRX 기준가 | 전일 |
| 과거일 재실행 | Naver 익일 행의 KRX 기준가 = 분석일 KRX 종가 (익일 기준가 재산정 시 사용 안 함) | 분석일 |
| 필요 시 | Daum 일별 `prevClosingPrice` | 전일 |
| 선택 | KRX Open API(`KRX_API_KEY`, 설정 시 최우선), KIS(`KIS_VALIDATE=1`) | 전일·분석일 |

- 통합 최종가는 Daum `afterMarketAvailable=false`(NXT 미거래 종목)이거나 무거래(거래량 0·종가=기준가)일 때만
  정규장 종가로 인정합니다. 그 외에는 보조값(일치 시에만 확인)으로만 씁니다. 근사치를 최종 종가로 쓰지 않습니다.
- 무거래일 종가 = KRX 기준가(거래소 규칙). Yahoo 전일종가와 기준가가 일치하면 이를 교차 확인으로 인정합니다.
- **특수거래(special_trading.py)**: 가격제한폭은 전일종가가 아니라 **KRX 기준가**(및 Daum 상/하한가) 기준으로 판정합니다.
  범위를 벗어나면 시장상태를 먼저 확인합니다: 정리매매(`isPreDelistingTrading`), 가격제한폭 미적용(상/하한가 0),
  신규/재상장(상장일 10일 이내), 액면변경(`parValueChange`), 기준가 재산정(`revaluation`), 권리/배당락(`ex`).
  확인되면 `SPECIAL_TRADING_VALIDATED`(INFO)로 정상 탐지, 확인할 수 없으면 `SPECIAL_TRADING_UNCONFIRMED`(WARNING, 탐지는 유지).
  특정 종목코드 예외는 없습니다.

KOSPI 목록은 FAIL-SOFT입니다. Naver `totalCount`와 수신 건수가 다르면 최대 3회 재조회해 합집합을 만들고,
`expected / loaded / missing-unresolved`를 로그·메일에 남긴 뒤 나머지를 분석합니다. 수신률 98% 미만이거나
주식이 500개 미만이면 목록 자체가 깨진 것으로 보고 ERROR입니다. 종목 수는 하드코딩하지 않습니다.

## 상태 판정 (market_result.py)

| 상태 | 의미 | 메일 제목 |
|---|---|---|
| NORMAL | 필요한 데이터 확보 + 교차검증 완료 | 태그 없음 |
| INFO | 결과 신뢰 가능, 참고사항만 있음: 2차 소스 지연이지만 다른 소스로 검증, 특수거래 확인, 대체 소스 2곳으로 확인, 휴장 안내 | 태그 없음 |
| WARNING | 결과는 생성했으나 검증 부족: 종목 누락, 기준선 근처(US -8%·KR -5% 이하) 종목의 단일 소스/미해결 불일치/단독 대체값, 특수거래 미확인, 목록·캘린더 경고, 교차검증 불가 종목 과다(5% 초과) | `[DATA WARNING]` |
| ERROR | 핵심 데이터 확보 실패: Coverage 90% 미만, 데이터 0, 거래일 판정 실패, 목록 붕괴, 시간 초과 | `[ERROR US]` 등 |

- 기준선과 먼 종목의 소스 이슈(결과에 영향 없음)는 상태를 바꾸지 않는 **소스 참고**로만 요약합니다.
- 메일 경고 문구는 실제 원인 카테고리(데이터 누락 / 대체 데이터 / 소스 간 가격 불일치 / 2차 검증 불가 /
  2차 검증 소스 지연 / 특수거래 …)에서 동적으로 만들어집니다. 원인이 없으면 문구도 없습니다.
- 상단 **DATA WARNING 건수는 WARNING/ERROR 항목만** 셉니다. INFO는 별도의 "참고 (정상 처리)" 상자에 표시합니다.
- 종료 코드는 ERROR 시장이 있을 때만 1입니다.

```
[급락 모니터] S&P500 1개 · KOSPI 13개
[급락 모니터][DATA WARNING] S&P500 2개 · KOSPI 5개 | 데이터 누락 US 1건 / KR 2건
[급락 모니터][ERROR US] S&P500 확인필요 · KOSPI 5개 | 데이터 누락 US 확인불가 / KR 0건
```

본문 시장 카드: 분석일 · 이전 거래일 · Listing · Valid Prices · Coverage · Fallback · Missing · Detected
+ 교차검증 완료 수 · 소스 불일치 수.

## 로그

GitHub Actions 로그에 시장별 `SUMMARY analysis_date previous_trading_date listing_count valid_price_count
missing_price_count fallback_count mismatch_count cross_validated candidate_count final_alert_count coverage status
validation={상태별 종목 수}`, 소스별 수집 결과(대상 수·응답·STALE 수), 검증 구간 이하 모든 후보
(`ticker | company | prev | close | raw_change | data_source | validation | note`), 모든 이슈(`LEVEL CATEGORY 종목 | 내용`)를
남깁니다. 전체 가격표·이슈는 artifact `combined-*.json`에 있습니다.

## 장애 대응(안전장치)

"어떤 한 가지가 고장 나도 그날 메일은 나간다"를 원칙으로 합니다.

| 계층 | 장애 | 처리 |
|---|---|---|
| 응답 | 이상한 JSON/값(문자열 상·하한가, inf, 형식 변경) | 어댑터(`providers/`)는 절대 예외를 던지지 않고 해당 종목만 `INVALID_PRICE` 처리. 검증 엔진도 유한한 양수만 사용 |
| 종목 | 한 종목 처리 중 예외 | 그 종목만 "내부 처리 오류"로 누락 처리, 나머지 계속 |
| 소스 | 한 소스가 죽음/차단 | 연속 25회 실패하면(`FailFast`) 그 소스를 건너뛰고 나머지 소스로 판정 (소스별 성공/실패/생략 수는 로그 `HEALTH`) |
| 시간 | 소스가 느림 | 단계별 예산(Yahoo 200초, Daum·Naver 200초, Nasdaq 200초) 소진 시 남은 종목은 다른 소스로 처리. 시장별 hard deadline 600초, step 14분, job 20분 |
| 시장 | 한 시장 전체 실패 | 그 시장만 ERROR(원인 문구 포함)로 표시, 다른 시장은 정상 보고 |
| 보고서 | HTML/텍스트 생성 오류 | 간이 텍스트 리포트로 대체해 발송(탐지 종목 포함), 파일 저장 실패도 발송을 막지 않음 |
| 메일 | Gmail 접속/로그인 일시 오류 | 접속·로그인 단계만 최대 3회 재시도(아직 아무것도 보내기 전이라 중복 없음). 비밀번호 오류·발송 중 오류는 재시도하지 않음 |
| 워크플로 | checkout/pip/SMTP/step 시간 초과 등으로 메일이 안 나감 | 마지막 "Safety net" 단계가 `output/mail_sent.flag` 부재를 보고 간이 경고 메일 발송 |
| 테스트 | 단위 테스트 실패 | 모니터는 그대로 실행·발송하고 run만 실패로 표시(메일보다 테스트가 우선하지 않음) |

메일 크기: Gmail은 HTML 약 102KB 초과 시 하단을 잘라 보여줍니다. 상위 10개는 기존 디자인, 그 아래는 가벼운 목록으로
표시하고(시장당 최대 100개), 90KB를 넘으면 목록/이슈 수를 단계적으로 줄입니다. 전체 목록은 텍스트 본문과 JSON에 항상 있습니다.

## 재시도 / 시간 제한

요청 timeout (5s 연결, 10s 읽기), 최대 3회 exponential backoff(0.5s, 1s). 실패 종목은 3초 뒤 1회 재조회,
공급자가 세션을 아직 게시하지 않은 것으로 보이면(실패 10% 초과) 60초 간격 최대 2회 재시도(단계 예산이 모자라면 생략).
US·KR은 별도 프로세스로 병렬 실행되며 시장별 600초 hard deadline.

## 알려진 운영 제약

- GitHub는 **공개 저장소의 예약 실행을 60일간 저장소 활동이 없으면 자동 중지**합니다. 워크플로가 매 실행마다 활성화 API를
  호출하지만(최선 노력), 확실한 방법은 두 달에 한 번이라도 커밋하는 것입니다. 메일이 갑자기 끊기면 Actions 탭에서
  "Enable workflow"가 표시되는지 먼저 확인하세요.
- 의존 패키지는 `requirements.txt`(직접)와 `constraints.txt`(하위 포함 전체 정확 버전)로 고정되어 있습니다. 올릴 때는 두 파일을 같이 갱신하고
  단위 테스트 + `RUN_LIVE_TESTS=1 pytest tests/test_live_data.py` + 수동 dry-run으로 확인하세요.
- `ubuntu-latest`는 2026-10-19부터 Ubuntu 26으로 바뀌므로 `ubuntu-24.04`로 고정했습니다. 옮길 때는 먼저 수동 dry-run으로 확인하세요.
- Nasdaq은 같은 IP에서 약 200건을 연달아 요청하면 약 15분간 HTTP 403으로 차단합니다(로컬에서 대량 재실행할 때 유의).
- 한글날·대체공휴일 등 한국 휴장 다음 날 아침에는 직전 거래일을 다시 분석하며 메일에 INFO로 안내합니다.

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

`tests/test_requirements.py`가 운영 요구사항 TEST 1–18을, `tests/test_regression_20260929.py`가 2026-09-29 FICO·005030
사례를 실제 수집 payload(tests/fixtures)로 검증합니다. 어댑터(`test_providers.py`), 합의 엔진(`test_validation.py`),
특수거래(`test_special_trading.py`)는 각각 단위 테스트가 있습니다. 2026-09-28 US 회귀 데이터는
`tests/fixtures/us-2026-09-28-closes.csv`(Yahoo·Nasdaq 실제 종가 503종목)입니다.
원인 분석은 [docs/root-cause-2026-09-29.md](docs/root-cause-2026-09-29.md)(2026-09-29/30 장애 포함)를 참조하십시오.
