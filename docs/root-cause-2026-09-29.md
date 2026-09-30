# 2026-09-29 장애 원인 분석 및 수정

대상 커밋: 운영 `099e20e`(GitHub Actions 실행 코드). 작업 트리에 남아 있던 미커밋 KRX 전용 재구축본은
`KRX_API_KEY`가 없어 운영 불가 상태였으며, 이번 수정에서 KRX/KIS는 선택형 교차검증으로 편입했다.

## US: "503/503, Coverage 100%, 탐지 0개" (분석일 2026-09-28)

**결론: 2026-09-28 결과(0개)는 정확했다. 코드가 놓친 종목은 없다.**

- Yahoo chart와 독립 소스 Nasdaq historical(공식 Close/Last)로 503종목 전체를 재수집했다.
  두 소스의 9/25·9/28 종가 불일치 0건(502종목 대조, BF.B는 Nasdaq 미지원). 정규장 종가 기준 -10% 이하 0개.
- 최대 하락 BE(Bloom Energy) 288.70 → 262.87 = **-8.947%**. 장중 저가 259.46은 전일 대비 -10.13%였다.
  장중가·시간외가를 보면 "-10% 종목이 있다"고 보일 수 있으나 정규장 종가 기준으로는 미달이다.
- 같은 기간 실제 -10% 이하 종가 하락(FSLR/GEN/MGM 9/24 등)은 운영 결과와 일치한다.
- 9/28 Wikipedia 실시간 목록 = 캐시 목록(503, 추가/삭제 0).

그럼에도 발견·수정한 구조적 위험:

| 위험 | 기존 코드 | 수정 |
|---|---|---|
| 2차 검증이 같은 공급자(yfinance=Yahoo) | `verify_candidates` | 독립 소스 Nasdaq로 **전 종목** 대조 |
| 2차 검증 실패 시 후보 제외(미커밋본) / 20일 거래량 표본 부족(신규 편입) 시 제외 | fail-closed → 실제 급락 누락 가능 | 검증 실패는 표시만 하고 탐지 유지 |
| Yahoo 실패 종목 대체 경로 없음 | 누락 | Nasdaq fallback |
| 마감 직후 last-trade timestamp(16:00:0x) 거부(미커밋본) | 누락 | 마감+5분 허용 |
| stale 행(분석일 거래량 0, 양일 동일) 미검출 | 통과 | 거부 |
| 데이터 미게시 상태 대응 없음 | 1회 재시도 | 60초 간격 최대 2회 availability 재시도 |
| batch 사이 고정 sleep, 요청 timeout 15s×재시도 | 느림 | 5s/10s timeout, 지수 backoff, 병렬 풀 |

## KR: "ValueError: KOSPI listing incomplete: 2479/2480"

- 위치: `099e20e:kospi.py:81` `if len(stocks) != total_count: raise ValueError(...)`.
- `total_count`는 Naver `marketValue/KOSPI` 응답의 `totalCount`(ETF 1,171 + ETN 364 + 주식 944 = 2,479 수준).
- 목록을 끝까지 페이지네이션했으나 수신 2,479 < totalCount 2,480. 08:00 KST 전후 Naver가 종목 상태
  (`tradableStatusUpdatedAt` 08:01~08:02)를 갱신하는 시점과 겹쳐 count와 목록이 불일치한 것으로 보인다.
  재현 시점(21:42 KST)에는 2,479/2,479로 정상. 누락된 1건은 ETF/ETN일 확률이 62%이며 이 경우 분석 대상도
  아니었다. 운영 로그가 없어 어떤 1건인지는 식별할 수 없다.
- 결함: 전체 상품 수의 1건 차이로 예외 → `run_kr_monitor` 전체 실패 → 정상 944개 결과 폐기, `FAILED 0/0`.
- 수정: 최대 3회 재조회해 합집합, `expected/loaded/missing-unresolved` 로그·WARNING, 나머지 분석 계속.
  수신률 98% 미만 또는 주식 500개 미만일 때만 FAILED. 중복·빈 코드는 제거/경고.

## KR 추가 발견: Naver 종가 = KRX+NXT 통합가 (정규장 종가 아님)

운영 KOSPI 가격원(Naver `/api/stock/{code}/price`, `siseJson`)의 일별 종가는 Nextrade 시간외(~20:00)까지 포함한
통합 최종 체결가다. 근거(2026-09-28/29, 944종목, 1,886 거래일쌍):

- Naver `closePrice - compareToPreviousClosePrice`(= KRX 기준가) == Yahoo `.KS` 전일 종가: **1,873건(99.3%)**
- 같은 기준가 == Naver 자신의 전일 종가: **397건(21%)**
- Daum `prevClosingPrice`도 KRX 값(예: SK이노베이션 9/28 KRX 158,200 vs Naver/Daum 종가 158,900).

2026-09-23 운영 보고서(구 Naver 방식) vs 수정 후(KRX 정규장 종가, 944/944 교차검증, OK):

| 종목 | 기존 | KRX 정규장 종가 | 판정 |
|---|---|---|---|
| 계양전기 | 3,805→3,300 -13.27% | 3,820→3,235 -15.31% | 탐지 유지 |
| 티와이홀딩스우 | 3,200→2,795 -12.66% | 2,800→2,790 -0.36% | **기존 오탐** |
| LX홀딩스1우 | 8,300→7,440 -10.36% | 7,310→7,440 +1.78% | **기존 오탐** |
| 현대제철 | -10.11% | -10.25% | 탐지 유지 |
| SHD | -9.04% | -9.04% | 탐지 유지 |
| GS건설 | -7.33% | -7.61% | 탐지 유지 |
| 현대건설 | -7.21% | -7.77% | 탐지 유지 |
| 대양금속 | 누락(전일 거래량 0으로 제외) | 1,131→965 -14.68% | **기존 미탐** |
| 신풍 | 미탐 | 1,228→1,119 -8.88% | **기존 미탐** |
| 대우건설 | 미탐 | 18,800→17,440 -7.23% | **기존 미탐** |

기존 코드는 거래량 0인 날이 하나라도 있으면 종목을 제외해 32개를 누락시켰다. 수정 후 무거래 종목은
종가=KRX 기준가(0%)로 유효 처리한다.

## 검증 명령

```
python -m pytest -q                                   # 2026-09-30 기준 160+ passed, live skipped
python main.py --dry-run --market us --session-date-us 2026-09-28
python main.py --dry-run --market kr --session-date-kr 2026-09-23
python main.py --dry-run
```


---

# 2026-09-30 07:37 KST 메일: 정상 결과가 DATA WARNING으로 발송된 문제

메일: `[급락 모니터][DATA WARNING] S&P500 1개 · KOSPI 13개 | 데이터 누락 US 0건 / KR 0건(대체 1)`.
두 탐지 결과(FICO -26.52%, 부산주공 -92.39%)는 모두 실제 정규장 종가 기준으로 정확했다.

## Root cause

1. **US - 2차 소스 지연 = 경고로 직결.** 검증 구조가 "Yahoo + Nasdaq" 2개뿐이었다. Nasdaq historical은
   분석일 행을 밤늦게 게시하므로 07:30 KST(18:30 ET)에는 FICO의 09-29 행이 없었다(최신 09-28).
   코드는 이를 곧바로 "2차 검증 불가"로 보고 시장 전체를 WARNING 처리했다. 3차 소스가 없었고,
   Nasdaq에 09-28 종가(전일종가)는 있었는데도 필드 단위 검증을 하지 않아 그 정보도 버려졌다.
2. **KR - ±30%를 전일종가 기준으로 무조건 이상치 처리.** KRX 가격제한폭은 기준가 기준이며 정리매매·신규상장 등에는
   적용되지 않는다. 시장상태 확인 없이 `abs(change) > 30%`만으로 WARNING을 냈다. 또 Yahoo에 전일 봉이 없자
   Naver 통합 최종가(NXT 포함 가능)를 "근사치" 종가로 사용했다.
3. **메일 문구가 상태와 무관한 고정 문장.** WARNING이면 원인과 상관없이 "일부 데이터 누락·대체·불일치"를 출력했고,
   상단 경고 건수는 모든 이슈를 셌다. Missing 0 / Fallback 0 / 불일치 0과 모순.

## 수정

- `providers/` 어댑터(yahoo, nasdaq, cnbc, naver, daum, krx_official)가 필드별 날짜·상태를 가진 표준 관측치를 반환.
- `validation.py`: 전일종가·분석일 종가를 필드별로 합의(2개 이상 일치 = 검증). 지연 소스는 `STALE_SOURCE`일 뿐 오류가 아님.
- US 3차 소스 CNBC(일괄, 키 불필요). FICO: 전일 840.89 = Yahoo+Nasdaq, 종가 617.87 = Yahoo+CNBC → `CROSS_VALIDATED`, INFO.
  CNBC가 다음 세션으로 전환된 스냅샷은 배당락 재산정 값(예: AMT 167.35→165.56)이므로 사용하지 않음.
- KR 2차 소스 Daum: `regularTradePrice`(NXT 제외 KRX 종가)와 `stockState`(정리매매·거래정지·액면변경·감자·권리락),
  상/하한가. `special_trading.py`가 기준가·시장상태로 판정 → 부산주공은 정리매매·가격제한폭 미적용으로
  `SPECIAL_TRADING_VALIDATED`, 전일 486 = Naver 기준가 + Daum 일별 기준가, 종가 37 = Daum + Naver(비NXT 종목) → INFO.
- 상태 체계 NORMAL / INFO / WARNING / ERROR, 원인별 문구, DATA WARNING 건수는 WARNING/ERROR만.

## 2026-09-29 재실행(dry-run, 발송 없음)

| | US | KR |
|---|---|---|
| Listing / Valid | 503 / 503 | 944 / 944 |
| 교차검증 완료 | 502 (BF.B: Nasdaq 미지원) | 944 |
| Missing / Fallback / 불일치 | 0 / 0 / 0 | 0 / 1(부산주공, 2개 소스로 확인) / 0 |
| 탐지 | 1 (FICO -26.52%) | 13 (부산주공 -92.39% 포함) |
| 상태 | INFO | INFO |

제목: `[급락 모니터] S&P500 1개 · KOSPI 13개` (DATA WARNING 없음).
