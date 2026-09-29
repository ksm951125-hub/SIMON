> **대체됨(2026-09-29):** 현재 설계와 원인 분석은 [root-cause-2026-09-29.md](root-cause-2026-09-29.md)를 따른다. KRX는 선택형 교차검증이며, Naver 종가는 KRX+NXT 통합가로 확인되어 KOSPI primary는 Yahoo `.KS`(KRX 정규장 종가)다.

# KRX 전용 파이프라인 재구축 감사

검사 저장소: `C:/Users/Se Min Kim/Documents/1_개발/2_주식 급락 알람`, 시작 HEAD `099e20e`(작업 전 clean).
앱에 설정된 `Documents/3_미국주식자동`은 실제 존재하지 않으며 이동된 저장소를 찾아 작업했다.

**코드/모의검증과 공식 과거 가격 확정은 구분한다. KRX/KIS 키가 로컬에 없으므로 공식 2026-09-23 회귀 및 한국 실제 Coverage는 미확정이다. 944/944 또는 새 급락 목록을 검증 완료했다고 주장하지 않는다.**

## 1. 실제 Root Cause

Git 이력 `d3f2bee`(KOSPI 도입), `273c578`(이전 보완), 저장 보고서 및 네이버 재조회로 확인했다.

- KOSPI Primary 자체가 공식 KRX가 아니라 네이버 `/api/stock/{code}/price`였다. 정규장/거래소를 보장하는 선택·검증이 없었다.
- `273c578:kospi.py`의 `_download_price_pair`는 네이버 `fluctuationsRatio`와 자체 계산이 0.1%p보다 달라도 `_chart_price_pair`의 양일 값이 같으면 허용했다. **같은 공급자 내부 모순을 같은 공급자의 두 번째 경로로 덮어쓴 것**이 코드상 결정적 결함이다.
- LX홀딩스1우 원본 재조회: 9/22 Close 8,300, 9/23 Close 7,440, 대비 +130, 표시율 +1.78%. 전일 종가를 기준으로 계산하면 -10.361445783133%. 대비금액에서 유추되는 7,310은 전날 행 8,300과 다르다. 이 7,310을 공식 정답으로 채택하지 않았다.
- 과거 Git 파일을 감사 폴더로 복원해 `_download_price_pair`를 실제 호출한 결과 `(8300,7440)`을 다시 반환했고 차트도 동일했다. 날짜 mismatch/숫자만 추출하는 mapping/캐시/Yahoo 조정종가가 이 실행의 원인이 아니었다.
- 현대건설도 과거 함수 `(131800,122300)` 반환을 재현했다. 당일 대비 -9,000, 표시율 -6.85%로 단순 전일 종가 기반 계산과 모순된다. GS건설·계양전기·티와이홀딩스우에도 모순이 확인됐다. 현대제철/SHD는 네이버 내부 산술만 일치하며 이것이 KRX 일치 증거는 아니다.
- NXT 혼입은 가능한 공급자 원인이나 이번 응답에는 거래소 출처 식별 필드가 없어 확정하지 못했다. **NXT 때문이라고 단정하지 않는다.** 공급자의 잘못된 값 생성 원인은 공식 대조 또는 공급자 설명이 추가로 필요하다.

## 2. Yahoo 사용 위치

`market_data.py`: 미국 chart query1/query2 및 yfinance. `sp500.py`: 미국 ticker 매핑. `news.py`: 미국 후보 뉴스 search. `config.py`: 미국 yfinance 캐시 경로. `requirements.txt`: yfinance. 미국 테스트/캘린더 설명/과거 문서/미국 cookie·timezone SQLite도 존재한다.

KOSPI 파일의 전체 Git 변경 이력에서 Yahoo/yfinance/.KS 도입은 발견되지 않았다. KOSPI의 문제 원천은 네이버였다. 현재 KOSPI 실행 파일 `kospi.py`, `krx.py`, `kis_validation.py`에는 Yahoo/Naver 수집 의존성이 없다. 전체 프로젝트에서 Yahoo를 전부 삭제하면 미국 정상 경로가 깨지므로 미국용은 유지했다.

## 3. 제거/변경 데이터 소스

네이버 KOSPI 목록·지수 날짜·일별 가격·차트 교차검증을 제거했다. KRX `stk_isu_base_info`와 `stk_bydd_trd`로 교체했다. KRX 장애의 비공식 fallback은 없다. KIS는 선택형 검증 전용이다.

## 4. KOSPI 구조

날짜별 KRX 마스터 → 종류/시장/식별자 검증 → KRX 두 세션 bulk 데이터 → 마스터와 코드 대조 → 양일 실제 종가/당일 공식 대비·등락률 검사 → 필요 시 KIS 양일 검증 → 미반올림 -7% 판정 → Coverage/메일.

마스터와 당일 가격의 추가/누락·중복을 대조한다. 전일에는 상장폐지 때문에 현재 마스터에 없는 종목이 있을 수 있어 별도로 처리한다. 현재 마스터를 과거 날짜에 무단 재사용하지 않는다.

## 5. S&P500 구조

현재 Wikipedia 구성종목 → Yahoo 날짜별 정규장 일봉 → -10% → yfinance 양일 종가 대조. 500개 회사와 증권 수는 같지 않다(복수 share class). 실제 전 종목 실행 503/503, OK, 급락 0을 확인했다. 미국 소스는 교체하지 않았다. 보조 검증 실패 또는 가격 자체 불일치는 이제 후보 제외·Coverage 감소다.

과거 수동 날짜에 현재 목록을 쓰는 한계가 있어 PARTIAL 경고를 추가했다. 당시 구성종목 복원이나 독립 거래소 공식 원장과의 검증은 지원하지 않는다.

## 6. KRX 정규장 종가 제한

허용 endpoint/필드는 KRX `stk_bydd_trd.TDD_CLSPRC`로 고정했다. 요청 기준일, 응답 BAS_DD, 시장 KOSPI를 확인한다. 임의 currentPrice/NXT/afterHours/adjClose는 계산에 사용되지 않으며 공식 종가가 없으면 실패한다. 실제 인증된 KRX 응답 및 KIS 교차검증은 아직 미실행이다. KRX 필드의 실운영 계약 검증까지 완료했다고 주장하지 않는다. 일별 거래량은 KRX의 ACC_TRDVOL 집계 그대로 기록하며 이를 정규장 체결만의 집계라고 추가 추정하지 않는다.

## 7. Prev Close

XKRX/NYSE 캘린더의 직전 완료 세션을 명시하고 두 날짜를 따로 조회한다. Decimal 계산 후 표시만 2자리다. -6.996%는 -7.00%로 표시되어도 미탐지, -7.004%는 탐지한다. 예상 세션 데이터가 비면 임시휴장으로 자동 간주하지 않는다. 임시휴장 미반영은 FAILED로 드러나므로 잘못된 날짜쌍을 정상 처리하지 않는다.

## 8. Corporate action

KRX 공식 대비금액 및 2자리 공식 등락률을 실제 양일 종가와 대조한다. 허용오차 0.005001%p. 기준가격 변경/분할/병합/증자/배당락/감자/합병 등으로 불일치하면 경고·제외한다. 독립 기준가격/이벤트 원장이 없으므로 이벤트 종류를 자동 확정하거나 임의로 가격을 보정하지 않는다. 공식 대비로 추정한 기준값은 경고 진단일 뿐이다. 이 보수적인 정책에서는 실제 급락 종목도 경고 목록에 남을 수 있다.

## 9. Universe

해당 날짜 유가증권시장 전체 상장주권: 보통주·우선주·종류주 포함. 수익증권/ETF/ETN/신주인수권 제외. 외국주권 분류 허용. REIT도 공식 주권 분류라면 포함. 관리/정지라는 이유로 삭제하지 않는다. 944 고정값이나 숫자 6자리 필터 없음. 알려지지 않은 분류는 수동 확인이 필요한 FAILED다. 실제 API 마스터에서 쓰는 모든 분류를 인증 후 확인해야 한다.

## 10. 누락 32개 원인

원본 보고서의 missing 전부를 복원하고 네이버 두 날짜 행을 재조회했다. **27개는 양일 거래량 0, 4개는 당일만 0, 1개는 전일만 0**이었다. 마지막 경우는 당일 실제 거래가 있어도 제외되는 코드 결함이었다. 요청 timeout/페이지 누락/K suffix 변환 때문이라는 증거는 없다. 거래량 0의 현실 원인이 정지인지 무거래인지 공급자 누락인지는 KRX 상태 자료 없이는 확정할 수 없다.

`0120X0 유진 챔피언중단기크레딧 X클래스`는 상품명상 펀드 혼입이 의심된다. 새 분류는 이름 추정 대신 KRX SECUGRP_NM을 사용한다. 기존 944를 공식 분모라고 간주하지 않는다.

| Code | Company | 재조회한 네이버 거래량 0 날짜 | 이전 제외 이유 |
|---|---|---|---|
| 000300 | DH오토넥스 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 001067 | JW중외제약2우B | 2026-09-23 | 비교 양일 중 거래량 0 |
| 001470 | 삼부토건 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 001570 | 금양 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 002410 | 범양건영 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 004920 | 씨아이테크 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 005030 | 부산주공 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 005110 | 한창 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 005725 | 넥센우 | 2026-09-23 | 비교 양일 중 거래량 0 |
| 007110 | 일신석재 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 008600 | 윌비스 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 009190 | 대양금속 | 2026-09-22 | 비교 양일 중 거래량 0 |
| 009310 | 참엔지니어링 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 009440 | KC그린홀딩스 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 011000 | 진원생명과학 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 011810 | STX | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 0120X0 | 유진 챔피언중단기크레딧 X클래스 | 2026-09-23 | 비교 양일 중 거래량 0 |
| 014160 | 대영포장 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 015020 | 이스타코 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 017040 | 광명전기 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 019490 | 엑시큐어하이트론 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 020760 | 일진디스플 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 069460 | 대호에이엘 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 074610 | 이엔플러스 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 084680 | 이월드 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 093240 | 형지엘리트 | 2026-09-23 | 비교 양일 중 거래량 0 |
| 101530 | 해태제과식품 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 109070 | 주성코퍼레이션 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 119650 | KC코트렐 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 143210 | 핸즈코퍼레이션 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 145210 | 다이나믹디자인 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |
| 348950 | 제이알글로벌리츠 | 2026-09-23, 2026-09-22 | 비교 양일 중 거래량 0 |

## 11. Coverage 개선 결과

기존 912/944=96.61016949%는 PARTIAL이었지만 프로세스 exit 0, 본문 누락 20개/로그 10개만 표시했다. 이제 100%만 OK, 95~100% 미만 PARTIAL, 그 미만/전체 실패 FAILED, **모든 경고 exit 1**. 전 종목 사유를 출력하고 FAILED 표를 숨긴다.

Mock에서는 무거래 종목 포함 100%와 누락/경고 차감 경로가 통과했다. 한국 실제 새 분모/분자는 키 부재로 아직 산출 불가다. 정상 종목 누락을 제거할 설계를 구현했지만 실제 100% 달성을 입증하지는 못했다.

## 12. 2026-09-23 회귀

KRX 실 API 두 endpoint에 무인증 요청을 했고 각각 HTTP 401, `Unauthorized Key`가 반환됐다. 로컬 KRX/KIS 관련 환경변수 없음. GitHub Secrets 실제 값은 로컬에서 읽을 수 없으며 설정되어 있다고 가정하지 않았다.

아래 OLD는 보존된 오류 보고서다. NEW는 공식 인증 부재로 미확정이며 0/기존값으로 채우지 않았다. 전체 -7% 목록도 미확정이다. `audit_regression.py`는 7개 한정이 아닌 KRX 전 Universe를 분석한 다음 비교표와 전체 탐지 목록을 만든다. 현재 실행 결과 BLOCKED, exit 2.

| Code | Company | OLD Prev | OLD Close | OLD % | NEW Prev | NEW Close | NEW % | Source | Result |
|---|---|---|---|---|---|---|---|---|---|
| 012200 | 계양전기 | 3,805.00 | 3,300.00 | -13.27 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 36328K | 티와이홀딩스우 | 3,200.00 | 2,795.00 | -12.66 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 38380K | LX홀딩스1우 | 8,300.00 | 7,440.00 | -10.36 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 004020 | 현대제철 | 35,600.00 | 32,000.00 | -10.11 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 001770 | SHD | 4,700.00 | 4,275.00 | -9.04 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 006360 | GS건설 | 37,500.00 | 34,750.00 | -7.33 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |
| 000720 | 현대건설 | 131,800.00 | 122,300.00 | -7.21 | 미확정 | 미확정 | 미확정 | KRX unavailable | DATA WARNING: KRX_API_KEY is required; no fallback permitted |

OLD는 과거 오류 보고서이며 공식 정답이 아닙니다. 새 값 미확정 상태에서 전체 급락 목록을 확정하지 않습니다.


## 13. 수정 파일

`kospi.py`, `krx.py`(신규), `kis_validation.py`(신규), `detector.py`, `main.py`, `market_result.py`, `market_data.py`, `report.py`, `saved_report.py`(신규), `audit_regression.py`(신규), 두 workflow, README, 본 감사 및 이전 감사의 대체 안내, 한국/미국/통합 테스트와 과거 오류 fixture.

requirements는 추가 패키지 없이 유지했다. 기존 메일 Secrets와 스케줄 유지. 이전 잘못된 artifact 수동 재발송은 새 schema+해시 검사로 막는다.

## 14. 테스트

등락률·-7% 표시/판정 경계, 한국 주말/추석/장중/미래, 미국 휴장/DST, 정규장/extended/adjusted 제외, KRX short code/ISIN/우선주, 날짜/시장/중복/NaN 검증, 공식 대비/등락률 및 8개 이벤트의 기준가격 불일치, 무거래/신규상장/누락, API 인증/실패/재시도, source/version/date cache 격리, KIS J 원주가 양일 검증, Coverage/상태/HTML 전체 경고/실패 표 숨김, 레거시 artifact 차단, 9/23 LX 잘못된 통과 방지.

과거 네이버 차트 일치 시 모순을 허용하던 테스트는 제거하고 반드시 거절하는 테스트로 대체했다. 테스트를 통과시키기 위해 잘못된 옛 동작을 유지하지 않았다.

## 15. 실행 결과

최종 단위/통합/렌더 검증 결과는 아래 마지막 검증 기록 참조. 시작 baseline 71 passed, 6 skipped. 중간 실패는 미국 보조 API 장애 시 후보 유지라는 이전 기대였고 새로운 fail-closed 정책으로 변경해 재검증했다.

실제 전체 dry-run: US 503/503, OK, 급락 0; KR FAILED(KRX_API_KEY 없음); JSON/Markdown/HTML 생성, 실제 메일 발송 없음, exit 1. 첫 실행 63.47초. 정상 완료로 가장하지 않는 장애 경로를 확인했다. 실제 미국 샘플 integration 3 passed, KRX 실데이터 테스트 1 skipped(인증 없음).

## 16. GitHub Actions

공식 actionlint v1.7.12 바이너리를 release checksums SHA256과 대조한 후 두 workflow 검사. Secrets/variable 연결·원래 cron·메일 설정·실패 artifact 업로드 유지. GitHub 서버에서 수정본 workflow를 실행하거나 push하지 않았다. 로컬 syntax 검증과 GitHub 실제 실행을 구분한다.

## 17. 필요한 설정

필수 Secret `KRX_API_KEY`: KRX 인증키 발급+유가증권 일별매매정보/종목기본정보 서비스 활용 승인. 선택 Secret `KIS_APP_KEY`, `KIS_APP_SECRET` 및 repository variable `KIS_VALIDATE=1`. 기존 GMAIL_ADDRESS/GMAIL_APP_PASSWORD/ALERT_EMAIL_RECIPIENT 유지. 실제 값을 저장소나 로그에 넣지 않는다.

## 18. 제한사항

공식 KRX 가격/Universe/전체 급락 회귀 확정, KIS 실호출, corporate action 독립 원장, 공급자 가격 생성 오류의 정확한 원인(NXT 여부), 수정본 GitHub 실제 실행은 미검증이다. KRX full-market 응답에 독립 totalCount가 없어 두 endpoint가 동시에 같은 집합을 누락하는 상류 오류까지 입증할 수는 없다. source/date/schema/교차 집합 검사는 이를 완전히 대체하지 않는다.

실제 KRX 키를 설정한 뒤 `audit_regression.py` 및 opt-in integration을 실행해야 운영 정확성/한국 Coverage 개선이 최종 확정된다. 승인 없는 키로 우회하거나 비공식 값을 공식 값으로 꾸미지 않았다.

## 출처/로컬 증거

- [KRX 인증과 서비스별 승인 안내](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO003.jsp)
- [KRX 유가증권 일별매매정보](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=JvJFzlAENzZlPBDNGAWC)
- [KRX 서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)
- [KIS 공식 기간별시세 예제: J=KRX, 1=원주가](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)
- `output/audit/krx-rebuild/legacy-*.json`: 잘못된 이전 네이버 원본(공식 정답 아님)
- `output/audit/krx-rebuild/legacy-reproduction.json`: 과거 함수 재현
- `output/audit/krx-rebuild/missing32-evidence.json`: 32개 거래량 0 날짜 재조회
- `tests/fixtures/legacy-2026-09-23.json`: 원본 7개/32개 보존(비공식)
- `output/krx-regression/comparison.json/.md`: 새 값 미확정 비교표
- `output/audit/krx-rebuild/`: 실제 실행/검증 로그
