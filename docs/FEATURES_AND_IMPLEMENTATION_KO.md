# 시대비서 프로그램 기능 및 구현 설명

## 1. 프로그램 개요

시대비서는 University of Seoul 학생 생활에 맞춘 로컬 중심(`local-first`) 자동화 도구다. 외부 서비스에서 데이터를 가져오더라도, 최종 기준 데이터는 로컬 SQLite DB에 저장한다. 현재 문서 기준 closed beta의 중심 제품은 "UOS Telegram product"이며, iCloud 대시보드는 선택형 보조 표면으로 본다.

### 1.1 현재 베타 크리티컬 패스

- UOS official API / UOS portal fallback 기반 시간표 및 과목 메타데이터
- UClass/Moodle 기반 강의자료 및 과제
- Telegram 명령과 아침/저녁 브리핑
- 사용자별 날씨
- UOS 일반공지 / 학사공지

현재 저장소에서 이 경로의 가시적인 구현 중심은 `sync_uos_portal_timetable`, UClass WS/HTML 수집, `telegram-listener`, `sync-weather`, 포털 공지 조회다. 반대로 iCloud 대시보드, GUI, relay는 현재 마일스톤에서 비차단 표면이다.

현재 코드 기준 전체 흐름은 아래처럼 요약할 수 있다.

`설정 로드 -> UOS 시간표/과목 메타데이터 확보 -> UClass 자료/과제 수집 -> 날씨/공지 동기화 -> SQLite 정규화 저장 -> Telegram 응답/리마인더/브리핑 생성 -> 선택형 iCloud 배포`

핵심 모듈은 다음과 같다.

- `src/sidae_secretary/config.py`
  - `config.toml`, `.env`, 환경변수를 합쳐 설정 객체를 만든다.
- `src/sidae_secretary/db.py`
  - SQLite 스키마, 마이그레이션, facade를 담당한다.
  - 세부 read/write 경계는 `db_auth_attempts.py`, `db_connections.py`, `db_sync.py`, `db_dashboard_queries.py` 로 분리됐다.
- `src/sidae_secretary/jobs/pipeline.py`
  - UOS 포털/UClass/날씨/Telegram 중심 수집과 브리핑 orchestration을 맡는다.
  - Telegram view-model 계산은 `telegram_setup_state.py`, `day_agenda_state.py`, `ops_health_state.py` 로 분리됐다.
- `src/sidae_secretary/onboarding.py`
  - HTTP transport와 onboarding wiring을 맡는다.
  - 실제 school-account connect 흐름은 `onboarding_service.py`, `onboarding_school_connect.py`, `portal_sync_service.py` 로 분리됐다.
- `src/sidae_secretary/ops_dashboard.py`
  - ops dashboard HTTP adapter를 맡는다.
  - snapshot/action 계산은 `ops_snapshot_service.py`, `ops_action_service.py` 로 분리됐고, HTML은 `ops_dashboard_assets/dashboard.html` 에 있다.
- `src/sidae_secretary/connectors/*.py`
  - UOS 포털, UClass, Telegram, 날씨, 학교 포털 공지와 선택형 LLM 연동을 담당한다.
- `src/sidae_secretary/cli.py`
  - 루트 Typer wiring과 공용 helper를 유지한다.
  - command 구현은 `cli_ops.py`, `cli_onboarding.py`, `cli_admin.py`, `cli_launchd.py` 로 분리됐다.
- `src/sidae_secretary/publish/dashboard.py`
  - 선택형 iCloud Drive 정적 대시보드를 렌더링한다.
- `src/sidae_secretary/gui.py`
  - 선택형 macOS 수동 실행 GUI를 제공한다.

## 2. 핵심 구조

### 2.1 설정 로드

설정은 `load_settings()`가 아래 순서로 합친다.

1. `config.toml`
2. 같은 디렉터리의 `.env`
3. 현재 환경변수

이후 `DATABASE_PATH`, `ICLOUD_DIR` 같은 경로를 설정 파일 기준 절대경로로 정규화한다. 그래서 launchd나 GUI에서 실행해도 같은 설정을 안정적으로 재사용할 수 있다.

### 2.2 로컬 DB

프로그램의 중심 저장소는 SQLite다. 주요 테이블은 다음과 같다.

- `events`
  - 시간표, 포털 일정, UClass 일정, 복습 이벤트
- `tasks`
  - 과제, 마감, 파일에서 감지한 제출 항목
- `artifacts`
  - 강의자료 파일과 그 메타데이터
- `notifications`
  - UClass 공지, 포털 공지, 기타 운영 알림
- `inbox`
  - Telegram에서 들어온 명령/초안 메시지
- `summaries`
  - LLM이 만든 요약
- `sync_state`
  - 각 작업의 마지막 실행 상태
- `building_map`
  - 교내 건물 번호와 건물명 매핑
- `telegram_reminders`
  - `/plan`으로 만든 예약 리마인더
- `identity_ack`
  - 민감 정보 외부 전송 전 사람의 ACK 기록

저장 방식은 거의 전부 `upsert`다. 즉, 같은 외부 항목을 다시 수집해도 중복을 만들지 않고 최신 정보로 갱신한다. DB 초기화와 마이그레이션에는 파일 락이 걸려 있어 동시 실행 충돌도 막는다.

### 2.3 provenance 추적

이 프로그램은 `metadata_json` 안에 provenance를 함께 넣는다. 예를 들어 항목이 `uclass_ws`에서 온 공식 데이터인지, `telegram_draft`처럼 추정성이 있는 입력인지, `llm_inferred`처럼 해석 결과인지 저장한다. 이 정보는 Telegram 응답과 선택형 iCloud 대시보드에서도 그대로 보여 준다.

### 2.4 현재 모듈 경계

RF-01 ~ RF-10 이후 코드 경계는 다음처럼 정리되어 있다.

- Telegram user-facing state
  - `/setup` 판정: `telegram_setup_state.py`
  - `/today`, `/tomorrow` agenda state: `day_agenda_state.py`
  - beta ops surface health 계산: `ops_health_state.py`
- Onboarding / portal connect
  - transport: `onboarding.py`
  - application service: `onboarding_service.py`
  - school plan 해석: `onboarding_school_connect.py`
  - post-connect portal sync bridge: `portal_sync_service.py`
- Ops dashboard
  - HTTP server / HTML render: `ops_dashboard.py`
  - snapshot 수집 / secret scrub / operator summary: `ops_snapshot_service.py`
  - audit / action queue / action 실행: `ops_action_service.py`
- DB facade split
  - auth attempts: `db_auth_attempts.py`
  - connection/session storage: `db_connections.py`
  - sync/weather read model: `db_sync.py`
  - dashboard read query: `db_dashboard_queries.py`
- CLI split
  - root wiring: `cli.py`
  - ops commands: `cli_ops.py`
  - onboarding commands: `cli_onboarding.py`
  - admin/verify commands: `cli_admin.py`
  - launchd commands: `cli_launchd.py`

즉, 큰 파일 하나가 직접 계산/transport/storage를 다 들고 있는 구조가 아니라, 계산 서비스와 transport adapter가 분리된 상태를 현재 기준 구조로 본다.

## 3. 전체 기능 요약

현재 코드 기준 주요 기능을 현재 베타 우선순위와 함께 정리하면 아래와 같다.

| 기능 | 현재 베타 우선순위 | 사용자 진입점 | 구현 핵심 |
| --- | --- | --- | --- |
| 환경 점검/초기화 | 공통 | `doctor`, `init`, `status` | 설정 병합, 의존성 검사, DB 초기화 |
| UOS 시간표/과목 메타데이터 | 베타 핵심 | `/connect`, `sync --all`, 포털 세션 prime | `sync_uos_portal_timetable`, course alias binding, UOS portal fallback |
| UClass 수집 | 베타 핵심 | `sync-uclass`, `uclass-poller` | Moodle WS + HTML 세션 fallback으로 공지/과제/자료 수집 |
| 강의자료/과제 후처리 | 베타 핵심 | UClass 동기화 내부 | 파일 저장, hash 재사용, 텍스트 추출, 요약, 파일 기반 과제 감지 |
| 날씨/미세먼지 | 베타 핵심 | `sync-weather`, `/weather`, `launchd install-weather-sync` | KMA 초단기/동네예보와 서울시 구별 대기질 snapshot 저장 |
| Telegram bot / 리마인더 | 베타 핵심 | `sync-telegram`, `telegram-listener`, `/plan` | update 수집, inbox 저장, 명령 실행, 리마인더 발송, bot menu 등록 |
| 아침/저녁 브리핑 | 베타 핵심 | `send-briefings`, `telegram-listener` | Telegram 직발송 브리핑 생성 |
| UOS 공지 | 베타 핵심 | `/notice_general`, `/notice_academic`, `/notice_uclass` | 포털/UClass 공지 조회 |
| iCloud 대시보드 / 오프라인 검증 | 선택형 | `publish`, `verify mobile-offline` | dashboard snapshot, materials archive, iPhone offline 체크 |
| GUI / relay | 선택형/비차단 | `gui`, `relay serve` | 운영 보조 또는 비핵심 표면 |
| 문서 메타데이터 유지 / UClass 진단 | 운영 보조 | `docs-artifacts`, `uclass probe` | docs metadata 일관성 검사, wsfunction 점검 |

## 4. 기능별 구현 설명

### 4.1 환경 점검과 상태 조회

`sidae doctor`는 단순 설정 체크가 아니라 운영 준비 상태를 한 번에 점검한다.

- 필수 설정 누락 확인
- Python/SSL 런타임 확인
- Telegram 및 선택형 LLM 관련 import 가능 여부 확인
- 선택형 iCloud 경로 존재/쓰기 가능 여부 확인
- DB 테이블별 개수 확인
- `--fix` 사용 시 필요한 디렉터리 자동 생성

`sidae status`는 DB 카운트만 보여 주지 않고, `sync_state`를 읽어 최근 동기화 결과와 source별 상태 카드까지 함께 출력한다.

### 4.2 전체 동기화 파이프라인

`run_all_jobs()`의 현재 실행 순서는 아래와 같다.

1. `sync_uclass`
2. `sync_uos_portal_timetable`
3. `sync_weather`
4. `sync_telegram`
5. `send_scheduled_briefings`
6. `sync_llm_summaries`
7. `send_daily_digest`
8. `publish_dashboard`

즉, 현재 `sync --all`은 UClass -> UOS 포털 시간표 -> 날씨 -> Telegram 중심 파이프라인이다.

각 단계는 `sync_state`에 기록되고, CLI는 JSON으로 결과를 반환한다. 또한 `sync --all`, `sync-uclass`, `sync-telegram`, `sync-weather`, `send-briefings`는 각각 별도 lock file을 사용해 동시 실행을 막는다.

### 4.3 외부 시간표 공유 링크 처리

현재 closed beta의 시간표 경로는 UOS 공식 API/포털 경로로 고정되어 있다. 따라서 Telegram으로 들어오는 외부 시간표 공유 링크는 동기화 입력으로 쓰지 않고, 학교 계정 연결 경로(`/connect`)를 안내하는 거절 메시지로 처리한다.
- 위치 문자열에서 건물 번호/강의실 분리
- `building_map`이 있으면 건물명을 metadata에 추가
- 최종 결과를 `events`에 upsert

즉, 교시 중심 데이터를 캘린더 중심 데이터로 바꾸는 변환기 역할이다.

### 4.4 UClass 동기화

UClass는 이 프로그램에서 가장 큰 파이프라인이다. 구현 흐름은 다음과 같다.

1. 인증 준비
- `UCLASS_WSTOKEN`이 있으면 그대로 사용
- 없으면 ID/PW로 Moodle token 발급 시도
- 필요 시 모바일 launch 경로에서 token 후보 추출
- 동시에 HTML 세션 로그인을 열어 웹 화면에서만 보이는 첨부파일도 수집

2. Moodle Web Service 호출
- site info
- popup notifications
- action events
- enrolled courses
- course contents
- assignments
- forum / discussion

3. 정규화
- 공지는 `notifications`
- 액션 이벤트/과제는 `tasks`
- 일정성 항목은 `events`
- 각 항목은 공통 external_id 규칙으로 멱등 저장

4. 자료 후보 수집
- 알림/과제/event raw payload 안의 URL
- course contents의 module/contents 파일 URL
- HTML 세션으로 긁은 course page 링크와 게시판 첨부파일

5. 자료 파일 저장
- `iCloud/SidaeSecretary/materials/<course>/<date>/...`
- 파일명 정리, 충돌 회피, content hash 계산
- 이미 있는 파일이면 재다운로드 대신 재사용

6. 후처리
- 텍스트 추출
- 자료 요약 생성
- 파일 기반 과제 마감 감지
- 선택 시 Telegram으로 새 요약 push

즉, UClass 기능은 단순 크롤러가 아니라 "공식 WS + HTML fallback + 파일 후처리"가 합쳐진 통합 수집기다.

### 4.5 강의자료 다운로드와 중복 회피

자료 저장은 `_download_material()`과 `record_artifact()` 흐름으로 처리된다.

- 외부 URL을 받아 iCloud materials 폴더에 저장
- 다운로드된 바이트의 해시를 계산
- 기존 artifact의 `content_hash`와 비교
- 내용이 같으면 기존 파일을 재사용
- 이름 충돌이 나면 해시 일부를 붙여 안전하게 저장

결과적으로 같은 자료를 반복 수집해도 불필요한 중복 파일이 쌓이지 않도록 설계되어 있다.

### 4.6 자료 텍스트 추출

`extract_material_text()`는 현재 다음 형식을 지원한다.

- PDF: `pypdf`
- PPTX: `python-pptx`
- HWP: `olefile` 기반 preview/body 추출

추출 결과는 `artifacts.metadata_json["text_extract"]`에 저장된다. 여기에는 추출 성공 여부, 타입, 해시, 글자 수, excerpt가 들어간다. 추출 실패도 metadata에 남기기 때문에 다음 실행에서 실패 상태를 추적할 수 있다.

### 4.7 자료 요약 생성

자료 요약은 `_build_material_brief()`가 담당한다.

동작 순서는 다음과 같다.

1. 먼저 heuristic 요약 생성
- 제목/헤딩/짧은 문장을 바탕으로 bullet 생성
- 핵심 키워드 추출
- 복습 질문 생성

2. `LLM_ENABLED=true`면 로컬 LLM 호출 시도
- 현재 코드는 `LLM_PROVIDER=local`만 허용
- 성공하면 LLM bullet과 action item으로 보강
- 실패하면 heuristic 결과로 fallback

3. 결과 저장
- `metadata_json["brief"]`
- provenance는 heuristic이면 low, LLM이면 medium

즉, LLM이 꺼져 있어도 기능 자체는 동작하고, LLM이 있으면 더 자연스러운 요약으로 덮어쓴다.

### 4.8 자료에서 과제 마감 감지

자료 본문 안의 "제출", "마감", 날짜/시간 표현을 보고 과제를 찾는 기능도 있다.

구현은 두 단계다.

- heuristic 감지
  - 힌트 키워드 + 날짜 패턴이 가까이 있는 문단을 찾음
  - 제목과 due 시각을 추정
- LLM 감지
  - 힌트가 있을 때 JSON만 반환하는 프롬프트로 과제 후보 추출

그 후 두 결과를 합쳐 `tasks`에 `uclass:material-task:*` 형태 external_id로 저장한다. provenance를 남겨서 heuristic인지 LLM 추론인지 구분할 수 있다.

### 4.9 날씨와 서울 미세먼지

`sync_weather()`는 KMA와 서울시 OpenAPI를 함께 사용한다.

KMA 쪽 구현은 다음과 같다.

- 위경도를 DFS grid로 변환
- 초단기 실황, 초단기 예보, 동네예보, coverage forecast를 각각 조회
- 여러 forecast를 병합해 현재/오늘/내일 snapshot 생성

서울 대기질 구현은 다음과 같다.

- 설정된 자치구 코드별 API 조회
- 구별 PM10, PM2.5, CAI 등을 정규화
- 가장 최신 측정 시각을 snapshot에 기록

결과는 `sync_state("sync_weather")`에 저장되고, Telegram `/weather`, 브리핑, 대시보드가 이 snapshot을 재사용한다.

운영 배포에서는 `launchd install-weather-sync`로 매시 고정 시각에 `sync-weather`를 실행할 수 있다.
기본 오프셋은 `:20`이며, 서울시 cleanair 페이지 안내상 `:00~:15` 구간에는 이전 시간 자료가 노출될 수 있다는 점을 반영한 값이다.

### 4.10 Telegram inbox와 명령 처리

Telegram 연동은 `getUpdates` 기반이다. 처리 순서는 다음과 같다.

1. 새 update 수집
2. 메시지 분류
- `/...` 형식이면 `command`
- 날짜/시간이 있으면 `event_draft`
- `due`, `by` 같은 힌트가 있으면 `task_draft`
- 그 외는 `note`
3. 모든 메시지를 `inbox`에 저장
4. `command`만 꺼내 실제 명령 실행
5. 결과를 Telegram으로 응답

이 구조의 장점은 "먼저 저장, 나중 처리"다. 그래서 자유 입력 메시지를 바로 버리지 않고, `inbox list`, `inbox apply`, `inbox ignore` 같은 관리 기능으로 이어질 수 있다.

또한 `sync_telegram()`은 Telegram bot menu(`setMyCommands`)를 자동 등록한다. 메뉴 해시를 `sync_state`에 저장해 불필요한 재등록을 줄인다.

### 4.11 지원 Telegram 명령

현재 코드 기준 주요 명령은 다음과 같다.

- `/start`
- `/help`
- `/setup`
- `/status`
- `/today`
- `/tomorrow`
- `/weather` 또는 `/todayweather`
- `/todaysummary`
- `/tomorrowsummary`
- `/notice_uclass`
- `/notice_general`
- `/notice_academic`
- `/inbox`
- `/apply <id|all>`
- `/done task <id|external_id>`
- `/plan <자연어 문장>`

허용 chat ID에 없는 채팅은 `/start`, `/help`, `/setup`만 통과시키고 나머지는 차단한다.

### 4.12 `/today`, `/tomorrow`, `/todaysummary`, `/tomorrowsummary`

이 명령들은 DB의 여러 테이블을 엮어 하루 단위 뷰를 만든다.

- UOS portal timetable occurrence를 기본 수업 뷰로 사용
- `canonical_course_id`와 alias 매핑으로 UClass 자료/공지/과제를 연결
- 필요하면 과거에 저장된 비공식 일정 source도 보조 overlay로 함께 보여 줄 수 있음
- 연결된 자료 요약과 과제 정보를 함께 출력

즉, 단순 목록 출력이 아니라 "UOS 시간표 중심으로 관련 자료와 할 일을 묶는" 뷰 생성기다.

### 4.13 자연어 리마인더 `/plan`

`/plan`은 즉시 메시지를 보내는 기능이 아니라 예약 리마인더 생성기다.

구현 방식은 다음과 같다.

- `TELEGRAM_SMART_COMMANDS_ENABLED`가 켜져 있어야 함
- LLM이 활성화되어 있으면 JSON만 반환하도록 프롬프트를 보냄
- LLM이 없거나 실패하면 `dateutil.parser` 기반 fallback
- 결과를 `telegram_reminders`에 `pending` 상태로 저장
- 이후 `sync-telegram` 또는 `telegram-listener`가 due reminder를 찾아 `[Reminder] ...` 형식으로 발송

### 4.14 장기 실행 Telegram listener

`telegram-listener`는 Telegram 처리의 권장 운영 모드다.

- 하나의 프로세스가 long polling 루프 유지
- 새 메시지 수집, 명령 처리, due reminder 발송을 한 cycle에서 수행
- briefings direct 모드면 같은 루프에서 아침/저녁 브리핑도 검사
- 에러가 계속되면 종료해서 launchd가 재시작하게 함
- 일반 `sync-telegram`과 같은 lock을 공유해 충돌 방지

### 4.15 inbox 적용

`inbox apply`는 Telegram 초안을 실제 일정/과제로 승격한다.

- `event_draft`는 `events`로 upsert
- `task_draft`는 `tasks`로 upsert
- 처리 후 `processed=1` 표시

즉, inbox는 free-form 입력의 staging area다.

### 4.16 아침/저녁 브리핑

브리핑은 `_build_scheduled_briefing()`, `build_precomputed_telegram_briefings()`, `send_scheduled_briefings()`가 담당한다.

메시지 구성 요소는 다음과 같다.

- 현재/오늘/내일 날씨
- UOS portal timetable 기반 수업 occurrence와 과목 메타데이터
- 수업과 연결된 최근 강의자료 요약
- 관련 UClass 공지
- 수업 관련 과제
- 일정 기간 내 마감 과제
- 선택적 LLM 가이드 3줄

브리핑 전달 모드는 두 가지다.

- `direct`
  - Mac이 Telegram bot으로 직접 전송
- `precompute_only`
  - 브리핑 payload와 txt/json 파일만 생성

`publish`는 선택형 표면이지만, 켜 두면 `dashboard/telegram_briefings/` 아래에 manifest와 slot별 `.json`, `.txt` 파일이 함께 생성된다.

### 4.17 Daily Digest

`send_daily_digest()`는 하루 한 번 Telegram으로 보내는 축약 요약이다.

포함 내용은 다음과 같다.

- 마지막 발송 이후 새 공지 수
- 며칠 내 마감 과제
- 마지막 발송 이후 새 자료

중복 발송 방지를 위해 `sync_state("daily_digest")`에 오늘 발송 여부를 기록한다.

### 4.18 선택형 iCloud 정적 대시보드

`render_dashboard_snapshot()`은 선택형 iCloud 산출물을 만든다.

- `dashboard/data.json`
- `dashboard/index.html`
- `dashboard/telegram_briefings/*.json|*.txt`
- `materials/`

구현 포인트는 다음과 같다.

- `db.dashboard_snapshot()`으로 events/tasks/notifications/materials/inbox/summaries/weather/sync 상태를 한 번에 수집
- 같은 JSON을 `index.html` 내부 `<script type="application/json">`에도 임베드
- 그래서 별도 웹서버 없이 `file://`로 열어도 동작

이 설계 덕분에 iPhone에서 iCloud 파일만 열어도 오프라인 확인용 대시보드로 쓸 수 있다.

### 4.20 포털 학사일정과 학교 공지

포털 관련 기능은 두 가지다.

1. 학사일정 import
- `portal import --ics-url|--ics-file|--csv`
- ICS/CSV를 `PortalEvent`로 정규화
- `events`에 upsert

2. 학교 공지 조회
- Telegram `/notice_general`, `/notice_academic`
- 학교 공지 목록 페이지 HTML을 직접 읽어 최근 제목 10개 추출

### 4.21 건물 번호 매핑

`buildings set`, `buildings import`, `buildings list`, `buildings seed-uos`로 건물 번호 매핑을 관리한다.

이 매핑은 주로 아래 위치에서 활용된다.

- 브리핑/Telegram에서 `21-101` 같은 위치를 사람이 읽기 쉬운 건물명으로 렌더링

### 4.22 UClass probe

`uclass probe`는 운영 진단 도구다.

- 각 wsfunction을 샘플 파라미터로 호출
- `OK`, `FAIL`, `SKIP` matrix 출력
- 응답 shape fingerprint 기록
- 선택 시 JSON 파일로 저장

UClass 구조가 바뀌는 경우, 실제 동기화 전에 이 probe가 경고 역할을 한다.

### 4.23 선택형 검증 기능

`verify mobile-offline`은 실제 오프라인 운영 가능성을 검사한다. 이 검증은 iCloud 같은 선택형 표면이 켜져 있을 때 특히 의미가 크다.

- publish가 최근 수행됐는지
- `dashboard/index.html`이 있는지
- iCloud materials 파일이 실제로 존재하는지

`verify closed-loop`는 아래 세 단계를 한 번에 실행한다.

1. doctor readiness
2. `sync --all --wait`
3. mobile-offline verify

즉, "설정 -> 동기화 -> 결과물"이 닫힌 고리로 정상인지 점검하는 기능이다.

### 4.24 export / import / backup

운영 복구용 기능도 준비되어 있다.

- `export`
  - 주요 테이블 전체를 JSON으로 저장
  - secret 값은 scrub 처리
- `import`
  - export JSON을 다시 DB에 upsert
- `backup --to-icloud`
  - DB, export JSON, `uclass_probe.json`을 zip으로 묶어 iCloud 백업 폴더에 저장

### 4.25 선택형 GUI

GUI는 별도 앱이 아니라 "CLI 실행기"에 가깝다.

- AppleScript 목록 창에서 작업 선택
- 선택된 작업을 새 Terminal 창에서 실행
- 로그에서 마지막 JSON 객체를 뽑아 한국어 요약 팝업 표시

현재 GUI가 제공하는 작업은 다음 세 가지다.

- 전체 동기화
- Telegram만 처리
- 상태 확인

### 4.26 launchd 자동 실행

CLI는 launchd plist를 직접 생성한다.

현재 beta-critical 경로에서 우선 설치할 대상은 다음과 같다.

- UClass poller
- fixed hourly weather sync
- onboarding
- Telegram listener

선택형/비차단 설치 대상은 다음과 같다.

- 일반 daily full sync
- Telegram poller
- publish
- fixed-time briefings
- relay server

생성되는 plist의 특징은 다음과 같다.

- 정확한 `sys.executable` 경로를 사용
- `--config-file`을 절대경로로 고정
- `WorkingDirectory`를 config 디렉터리로 설정
- agent / daemon scope를 구분

운영자가 새 구조에서 기억할 점은 다음과 같다.

- command registration 은 `cli.py` 에 있지만 실제 구현은 하위 `cli_*` 모듈에 있다.
- launchd/job 동작 검증은 CLI output shape만 보지 말고 `status`, `ops snapshot`, Telegram `/setup` 까지 같은 인스턴스를 가리키는지 확인해야 한다.
- onboarding 문제는 bot/DB 문제와 proxy/public route 문제를 분리해서 봐야 한다.
  - local onboarding: `http://127.0.0.1:8791/...`
  - public onboarding: `ONBOARDING_PUBLIC_BASE_URL/...`
  - local 은 되는데 public 만 실패하면 보통 proxy/Funnel routing 문제다.

### 4.27 UClass poller

`uclass-poller`는 장기 실행 수집기다.

- UClass host가 reachable 될 때까지 주기적으로 연결 확인
- 연결이 살아나면 `sync-uclass` 실행
- 이후 interval이 지나면 다시 실행

즉, "네트워크가 열릴 때만 도는 collector" 성격이다.

### 4.28 선택형 서명 relay 서버

브리핑을 외부 발송자에게 맡길 때 Telegram bot token을 외부에 주지 않기 위해 relay 기능이 있다.

구현 방식은 다음과 같다.

- 브리핑 payload를 canonical form으로 정규화
- HMAC-SHA256 서명 생성
- `relay serve`가 서명 검증
- 같은 `item_key`를 state file로 dedupe
- 허용된 chat ID에만 Telegram 대신 전송

즉, iPhone Shortcut이나 외부 자동화는 서명된 요청만 보내고, 실제 Telegram 발송은 이 Mac이 맡는다.

### 4.29 docs-artifacts

`docs-artifacts`는 코드 기능이 아니라 저장소 운영 보조 도구다.

- 현재 git branch / HEAD / dirty 상태 수집
- `docs/snapshot.json`, `docs/audit.json`, `docs/SNAPSHOT.md`, `docs/AUDIT.md`의 생성 시각과 git 메타데이터 동기화
- `check` 모드에서는 불일치만 검증

## 5. 안정성 및 보안 설계

현재 코드에서 눈에 띄는 안전 장치는 다음과 같다.

- 작업별 file lock
- DB migration lock
- export 시 secret scrub
- Telegram 허용 chat ID 제한
- provenance와 confidence 추적
- conflict warning 생성
- `INCLUDE_IDENTITY=true`일 때 identity ACK gate

특히 identity gate는 중요하다. Telegram 전송이든 LLM 전송이든, `include_identity`가 켜져 있으면 먼저 `sidae ack identity ...`로 사람 ACK를 기록해야 외부 송신이 허용된다.

## 6. 현재 코드 기준 주의할 점

현재 저장소에는 "설계 흔적은 남아 있지만 지금은 비활성"인 부분도 있다.

### 6.1 LLM provider는 local only

`connectors/llm.py`는 현재 `LLM_PROVIDER=local`만 허용한다. 따라서 실제 동작하는 LLM 경로는 로컬 endpoint뿐이고, 이것도 beta-critical path의 필수 조건은 아니다.

### 6.2 review 스케줄 로직은 존재하지만 노출이 제한적

`schedule_review_events()`와 `mark_review_status()` 같은 내부 로직은 존재하지만, 현재 CLI에는 review 전용 명령 그룹이 노출되어 있지 않고 `run_all_jobs()`에도 포함되지 않는다. 따라서 복습 이벤트 기능은 코드에 일부 준비돼 있으나, 현재 공식 운영 경로의 중심 기능이라고 보기는 어렵다.

## 7. 결론

시대비서의 현재 beta-critical path는 아래 다섯 가지로 압축된다.

- UOS official API / UOS portal fallback 기반 시간표와 과목 메타데이터
- UClass/Moodle 기반 강의자료와 과제
- Telegram 명령과 아침/저녁 브리핑
- 사용자별 날씨
- UOS 일반공지 / 학사공지

iCloud 대시보드, GUI, relay는 현재 마일스톤의 비차단/선택형 기능이다. 이 프로그램의 핵심 설계는 외부 서비스가 아니라 로컬 DB를 기준점으로 삼는 것이며, 그 DB를 바탕으로 UOS Telegram product를 안정적으로 운영하는 데 있다.
