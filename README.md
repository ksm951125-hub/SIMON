# S&P 500 급락 자동 모니터

미국 정규장이 끝난 뒤 S&P 500 전 구성종목을 확인해 전 거래일 대비 종가가 10.0% 이상 하락한 종목을 찾고, CSV/JSON/Markdown 보고서를 저장한 뒤 Gmail에서 지정한 수신 메일로 보냅니다. PC가 꺼져 있어도 GitHub Actions가 실행합니다.

## 동작 방식

- NYSE 공식 거래 캘린더로 대상일과 직전 거래일을 계산합니다.
- Wikipedia의 현재 S&P 500 목록(`ticker`, 회사명, 섹터)을 받고 성공한 목록을 `data/sp500_constituents.csv`에 캐시합니다.
- Yahoo Finance의 분할 조정 정규장 일봉을 100종목 batch로 내려받습니다. 일부 ticker 누락은 별도 batch로 재시도하며, BRK.B 같은 기호는 Yahoo의 BRK-B 형식으로 자동 변환합니다.
- 누락, NaN, 0 이하 가격, 거래일 불일치, 중복 ticker를 검증합니다.
- 가격 커버리지가 95% 미만이면 불완전한 "급락 없음" 알림을 보내지 않고 실행을 실패 처리합니다.
- Yahoo가 일부 종목을 NaN으로 돌려주면 누락 종목 전체를 소규모·단일 스레드로 재조회합니다.
- 재조회 후에도 모니터링이 실패하면 메일이 조용히 누락되지 않도록 실패 알림을 보냅니다.
- 10% 이상 하락 후보만 별도로 다시 받아 등락률을 2차 확인합니다.
- 최종 후보가 있을 때만 뉴스를 조회합니다. 뉴스 실패는 가격 보고서 생성을 막지 않습니다.

Yahoo Finance는 API Key가 필요 없지만 비공식 데이터 경로이므로 일시적인 제한이나 형식 변경 가능성이 있습니다. batch, 재시도, 누락 기록, 후보 재검증을 적용했습니다.

## 로컬 실행

Python 3.11 이상을 설치한 뒤 PowerShell에서 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pytest -q
python main.py --dry-run
```

특정 과거 거래일을 테스트하려면:

```powershell
python main.py --dry-run --session-date 2026-09-22
```

결과는 `output/YYYY-MM-DD.csv`, `.json`, `.md`에 저장됩니다. 휴장일에는 "미국 증시 휴장 - 분석 대상 없음" 보고서가 정상 생성됩니다.

## GitHub에 올리기

GitHub에서 빈 저장소를 만든 뒤 이 폴더에서 아래 명령을 실행합니다. `YOUR_NAME`과 저장소 이름은 바꾸십시오.

```powershell
git init
git add .
git commit -m "Build automated S&P 500 drop monitor"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/sp500-drop-monitor.git
git push -u origin main
```

비공개 저장소도 사용할 수 있습니다. GitHub 인증은 브라우저 또는 Git Credential Manager 안내에 따라 본인이 완료해야 합니다.

## Gmail 발송 설정

1. 발신 Google 계정에서 **2단계 인증**을 켭니다.
2. [Google 앱 비밀번호](https://myaccount.google.com/apppasswords)에서 GitHub Actions용 16자리 앱 비밀번호를 생성합니다. 일반 Google 로그인 비밀번호는 사용할 수 없습니다.
3. GitHub 저장소의 **Settings → Secrets and variables → Actions → New repository secret**에서 아래 값을 만듭니다.

| Secret | 값 |
|---|---|
| `GMAIL_ADDRESS` | `seminfam11@gmail.com` |
| `GMAIL_APP_PASSWORD` | Google에서 생성한 16자리 앱 비밀번호 |
| `ALERT_EMAIL_RECIPIENT` | `master1256@naver.com` |

Secret이 없으면 모니터와 보고서 생성은 성공하고 메일 전송만 건너뜁니다. 비밀번호를 파일, 커밋, 채팅, Actions 로그에 넣지 마십시오.

## GitHub Actions와 자동 실행 시간

`.github/workflows/daily_monitor.yml`은 `7 3 * * 2-6`으로 실행됩니다. 이는 DST와 무관하게 **한국시간 화요일~토요일 오후 12:07**입니다. KST는 DST를 사용하지 않습니다. Yahoo의 일봉이 장 마감 직후 일부 종목에서 비어 있는 운영 사례를 반영해 정규장 종료 후 최소 6시간이 지난 세션만 처리합니다. GitHub 공식 문서상 매시 정각은 예약 실행 부하가 높아질 수 있어 7분으로 분산했으며, 이 시각도 큐 상황에 따라 더 늦어질 수 있으므로 정확한 도착 시각은 보장되지 않습니다.

예약 workflow는 default branch에서만 실행됩니다. 또한 공개 저장소가 60일 동안 비활성 상태이면 GitHub가 예약 workflow를 자동 비활성화할 수 있으므로, 장기 무관리 운영에는 비공개 저장소를 권장합니다. 자세한 제한은 [GitHub의 schedule 이벤트 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)를 확인하십시오.

GitHub의 **Actions → S&P 500 Daily Drop Monitor → Run workflow**에서 수동 실행할 수 있으며, 필요하면 미국 거래일을 입력할 수 있습니다. 실행 페이지 로그에서 `[1/6]`부터 `[6/6]`까지 진행 상황과 누락 종목을 확인합니다. 실행 결과 파일은 페이지 하단 **Artifacts**에서 90일 동안 내려받을 수 있습니다.

미국 휴장일에는 최신 과거 결과를 중복 발송하지 않고 휴장 안내를 발송합니다. 조기 종료일도 NYSE 캘린더의 실제 장 종료 시각을 사용합니다.

## 오류 확인

1. GitHub 저장소의 **Actions** 탭에서 실패한 실행을 엽니다.
2. `Run unit tests`와 `Run monitor` 로그를 확인합니다.
3. 구성종목 다운로드 실패 시 `data/sp500_constituents.csv` 캐시 사용 여부를 확인합니다.
4. 가격 누락은 로그와 Markdown/JSON 보고서의 누락 목록에 표시됩니다.
5. 메일만 오지 않으면 Google 2단계 인증, 세 Secret 이름, 앱 비밀번호, Naver 스팸함을 확인합니다.

## 파일 구조

```text
main.py                 실행 진입점
market_calendar.py      NYSE 거래일 판단
sp500.py                구성종목 및 캐시
market_data.py          batch 가격 수집과 후보 재검증
detector.py             등락률 및 임계치
validator.py            데이터 무결성 검사
news.py                 후보 뉴스와 보수적인 원인 분류
report.py               CSV/JSON/Markdown 보고서
notifier.py             Gmail SMTP 메일 알림
tests/                  단위 테스트
.github/workflows/      자동 실행 설정
```
