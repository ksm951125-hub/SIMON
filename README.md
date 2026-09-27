# S&P 500 + KOSPI 급락 모니터

현재 S&P 500 구성종목과 KOSPI 보통주·우선주(ETF/ETN/ELW 제외)의 **직전 거래일 정규장 종가 → 분석 거래일 정규장 종가**를 비교합니다.

- S&P 500: `((close / previous_close) - 1) * 100 <= -10.0`
- KOSPI: 같은 계산으로 `-7.0% 이하`
- 경계값 계산의 이진 부동소수점 잡음은 소수 12자리에서 정규화합니다.
- 현재 목록으로 과거 날짜를 조회하므로 과거 구성종목을 재현하는 백테스트는 아닙니다.

## 데이터와 거래일

미국은 Wikipedia의 현재 목록을 검증한 후 Yahoo Finance 날짜 지정 `chart`의 `interval=1d`, `includePrePost=false`, `quote.close`를 사용합니다. 현재가, 시간외 가격, screener snapshot, `Adjusted Close`는 계산에 사용하지 않습니다. Yahoo의 센트 단위 가격에 남는 이진수 잡음은 제거합니다. 2차 yfinance 조회도 `auto_adjust=False`, `prepost=False`입니다. 이 두 경로는 같은 공급자를 사용하므로 독립된 거래소 공식 원장 검증은 아닙니다.

NYSE 캘린더에서 직전 실제 세션을 찾고 정규장 종료 + 2시간이 지난 세션만 선택합니다. 조기 폐장과 DST를 반영합니다. 휴장·주말에는 최신 완료 세션을 표시합니다. 미래/미확정 날짜의 수동 실행은 실패합니다. 공급자 데이터가 아직 없으면 이전 날짜 가격을 당일 가격으로 대체하지 않습니다.

한국 목록은 Naver KOSPI 목록의 전체 페이지 수와 중복을 검사합니다. 분석일은 특정 종목이 아닌 KOSPI 지수의 실제 일별 날짜로 선택하며, KRX 캘린더와 다르면 경고합니다. 당일 자료는 KST 18시 이후만 허용하여 수동 장중 실행과 시간외 데이터의 혼입을 방지합니다. 오전 08시 실행은 최근 완료 한국 거래일을 분석합니다. 휴장일에는 날짜가 며칠 전이어도 정상일 수 있습니다.

한국 가격은 날짜 지정 `closePrice`를 사용합니다. 표시 등락률이 계산과 0.1%p 이상 다르면 별도 Naver 일봉 차트에서 두 날짜의 종가를 다시 확인합니다. 두 가격이 일치할 때만 사용하며 표시 등락률로 계산을 대체하지 않습니다.

### Corporate action 및 이상 데이터

- Yahoo 분할/병합 이벤트가 비교 구간에 있으면 자동 급락 확정을 보류하고 누락/경고로 기록합니다. 공급자의 분할 조정 여부를 추측하여 가격을 다시 나누지 않습니다.
- 배당 조정 `Adjusted Close`를 섞지 않습니다. 따라서 일반 배당락에 따른 실제 종가 하락은 가격 수익률에 포함됩니다(배당 재투자 총수익률이 아님).
- 한국 ±30.01% 초과, 종가/교차 차트 불일치, 거래량 0은 권리락·재상장·거래정지 등 수동 확인 대상으로 분리합니다. 큰 실제 변동이 경고에 포함될 수 있으므로 누락 목록도 확인해야 합니다.
- 누락 날짜, NaN/무한대/0/음수, 중복 날짜, ticker 불일치, 상장 변경에 따른 비교 불가를 급락으로 확정하지 않습니다.
- 공급자 이벤트 누락·데이터 오류를 완전히 보증하는 시스템은 아닙니다. 정식 거래소 데이터 계약/API는 사용하지 않습니다.

## Coverage 및 실패 처리

분모는 실제 검증한 현재 종목 목록 수입니다. 고정된 503/944를 사용하지 않습니다. 분자는 두 날짜를 정상 비교할 수 있는 종목 수이며 미국 후보 재검증에서 거절된 종목도 제외합니다.

| 상태 | 조건 | 프로세스 종료 |
|---|---|---|
| OK | 전체 유효, 데이터 경고 없음 | 0 |
| PARTIAL | 95% 이상이나 누락/목록 최신성/캘린더 경고 있음 | 0, DATA WARNING 메일 |
| DATA_INCOMPLETE | 유효 데이터가 있으나 95% 미만 | 1, 결과 확정 불가 |
| FAILED | 유효 데이터 없음, 수집 실패 또는 실행 제한 초과 | 1 |

목록 캐시 사용 시 최신성 미검증을 메일에도 표시합니다. 시장 하나가 실패해도 다른 시장의 결과와 통합 실패 보고서는 생성합니다. 제목에는 `급락: S&P500 N개 · KOSPI N개 | 데이터 오류: US N건 / KR N건`을 표시합니다. 시장 전체 오류는 숫자 대신 확인필요로 표시하며 본문에 사유를 남깁니다.

## 실행 시각과 GitHub Actions

`.github/workflows/daily_monitor.yml`의 `0 23 * * 1-5`는 UTC 월~금 23:00, **KST 화~토 오전 08:00**입니다. 미국 장 마감은 DST 때 KST 05시, 표준시 때 06시이므로 각각 3시간/2시간 이후입니다. 이는 공급자의 모든 데이터가 반드시 확정된다는 보장이 아니며 실제 날짜·Coverage 검증으로 보완합니다.

예약 실행은 default branch에 있는 코드가 적용됩니다. GitHub의 큐 때문에 실제 시작이 늦어지거나 예약이 누락될 수 있습니다. 매일 정확히 08:00 도착하는 SLA는 아닙니다. [GitHub schedule 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Python 3.12, 읽기 전용 contents 권한, 의존성 설치, 단위 테스트, 모니터 실행, 결과 artifact 업로드 순서입니다. job timeout은 20분, 개별 시장은 별도 프로세스로 **300초** 후 강제 종료합니다. 개별 요청에는 timeout과 제한된 재시도를 적용합니다. 수동 입력은 shell에 직접 삽입하지 않고 환경변수로 전달합니다.

수동 실행: Actions → **US + KOSPI Daily Drop Monitor → Run workflow**. `session_date_us`, `session_date_kr`를 비우면 자동 선택합니다. `send_email=false`가 기본 dry-run이며 `true`는 실제 메일을 보냅니다. 수동 메일 제목에는 `[TEST]`가 붙습니다. `recipient_override`를 입력하면 해당 수동 실행에만 그 수신처를 사용하며, 비우면 저장된 Secret의 수신처를 사용합니다. 정기 발송 수신처는 바뀌지 않습니다.

## Secrets와 이메일

저장소 Settings → Secrets and variables → Actions에 다음 세 Secret이 필요합니다. 실제 값은 코드·문서·채팅에 넣지 마십시오.

| Secret | 용도 |
|---|---|
| GMAIL_ADDRESS | Gmail 발신 계정 |
| GMAIL_APP_PASSWORD | 2단계 인증 후 발급한 Google 앱 비밀번호 |
| ALERT_EMAIL_RECIPIENT | 수신 주소; 여러 주소는 쉼표/세미콜론으로 구분 |

SMTP SSL 465, 인증서/호스트 검증, 30초 timeout, UTF-8 plain text + HTML을 사용합니다. Secret 누락·인증 오류·수신 거절은 실패로 처리합니다. SMTP 수락 여부가 불명확한 오류는 중복 전송 위험 때문에 자동 재발송하지 않습니다. 서버 수락은 받은편지함 도착을 보증하지 않습니다.

로컬 `.env`는 자동으로 읽지 않습니다. 로컬 실제 발송이 필요하면 위 환경변수를 안전하게 설정한 후 `--notify`를 사용하십시오. `--dry-run`이 함께 있으면 실제 발송하지 않습니다. `.env*`, output, 임시 브라우저 프로필은 Git에서 제외합니다.

## 로컬 실행과 테스트

Python 3.12 권장. Windows에서 이미 있는 가상환경이 정상이라면 재생성하지 말고 사용하십시오.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe main.py --dry-run
.venv\Scripts\python.exe main.py --dry-run --session-date-us 2026-09-25 --session-date-kr 2026-09-23
```

단위 테스트는 외부 API를 호출하지 않습니다. 실제 데이터 검증은 별도로 실행하며 메일을 보내지 않습니다.

```powershell
$env:RUN_LIVE_TESTS = '1'
.venv\Scripts\python.exe -m pytest tests/test_live_data.py -q
Remove-Item Env:RUN_LIVE_TESTS
```

pytest 임시폴더 소유권 문제가 있으면 새 경로를 지정합니다: `python -m pytest -q -p no:cacheprovider --basetemp=output/test-new-run` (기존 자료가 없는 경로 사용).

## 출력과 오류 조사

`output/combined-실행시각.json`, `.md`, `.html` 및 시장별 후보 CSV를 생성합니다. 통합 보고서는 실행시각이 달라지면 이전 결과를 덮어쓰지 않습니다. 후보 CSV는 시장별 분석일 파일을 갱신합니다. Actions artifact에는 전체 결과가 저장됩니다. HTML은 기존 table/inline CSS 디자인을 유지하며, 과다한 경고는 시장별 20건까지 표시하고 전체 목록은 JSON/Markdown에 남깁니다.

로그에서 시장별 분석일·이전일, 대상/수집 수, Coverage, 기준, 급락 수, 실행시간을 확인하십시오. 누락 로그는 시장별 10개 예시만 출력합니다. 데이터 경고에서는 급락 0건을 전체 종목 정상으로 해석하지 마십시오.

1. Actions 실패 단계가 dependency/test/monitor 중 어디인지 확인합니다.
2. JSON의 `missing`, `error`, `coverage`, 날짜를 확인합니다.
3. 대규모 HTTP 429/403 또는 timeout이면 공급자 장애·접속 제한을 확인합니다.
4. 메일 인증 실패면 Secrets 이름/앱 비밀번호를 확인하고 수신 누락이면 스팸함도 확인합니다.
5. 로컬 수정은 GitHub 기본 브랜치에 반영되기 전에는 예약 실행에 적용되지 않습니다.

## 주요 파일

| 파일 | 역할 |
|---|---|
| main.py | 실행, 상태·Coverage, 시장 격리, 보고서 저장 |
| runtime.py | 시장별 프로세스와 강제 종료 제한 |
| market_calendar.py | 미국 거래일·DST·확정 대기 |
| sp500.py | 현재 구성종목/캐시·최신성 경고 |
| market_data.py | 날짜 지정 미국 일봉/후보 검증 |
| kospi.py | 한국 목록/지수 날짜/종가·차트 대조 |
| detector.py / validator.py | 경계값·가격 무결성 |
| market_result.py | 시장별 결과 모델 |
| report.py / notifier.py | 기존 HTML·텍스트·제목 / Gmail SMTP |
| news.py | 후보 부가 뉴스(가격 판정에 사용하지 않음) |
| tests/ | API 비의존 테스트 및 opt-in 실제 데이터 테스트 |
| docs/operational-audit-2026-09-27.md | 운영 재검증 결과와 제한사항 |
