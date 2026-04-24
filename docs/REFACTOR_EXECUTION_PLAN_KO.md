# 구조 개선 실행 계획 및 티켓 설계

기준일: 2026-04-02

## 1. 목적

이 계획의 목적은 현재 레포의 초대형 모듈과 숨은 결합을 단계적으로 줄이되, 기존 기능과 보안 경계를 깨지 않고 안전하게 구조를 개선하는 것이다.

이번 계획은 특히 아래 4개 모듈을 우선 대상으로 본다.

- `src/sidae_secretary/jobs/pipeline.py`
- `src/sidae_secretary/db.py`
- `src/sidae_secretary/onboarding.py`
- `src/sidae_secretary/ops_dashboard.py`

보조 대상으로는 `src/sidae_secretary/cli.py` 를 둔다.

## 2. 현재 구조 문제 요약

### 2-1. 구조적으로 큰 리스크

- `pipeline.py` 가 sync orchestration, Telegram command 처리, Telegram 렌더링, ops health, admin repair 를 동시에 담당한다.
- `db.py` 의 `Database` 가 migration, seeding, user adoption, auth monitor, dashboard read model 까지 흡수하고 있다.
- `onboarding.py` 가 HTTP server, 브라우저 세션 생명주기, 자격 증명 교환, DB 저장, Telegram 알림, post-connect sync 를 한 closure 안에서 처리한다.
- `ops_dashboard.py` 가 3k+ lines inline HTML, 프로세스/로그 수집, snapshot 빌드, admin action endpoint 를 같이 들고 있다.
- `pipeline.py` 와 `onboarding.py` 사이에 순환 의존성이 있다.

### 2-2. 실제로 이미 드러난 신호

- 전체 테스트에서 현재 `2`개 실패가 존재한다.
- 둘 다 Telegram 상태/일정 렌더링과 시간표 상태 판단이 얽힌 영역이다.
- 즉, 현재도 “저장 상태 해석”과 “사용자 메시지 렌더링”이 너무 세게 결합되어 있다.

## 3. 리팩터링 원칙

### 3-1. 기능 원칙

- 사용자 기능은 기존 동작을 기본값으로 유지한다.
- Telegram 명령어 스펙과 메시지 형태는 golden test 로 먼저 고정한 뒤 변경한다.
- prod/beta 분리 가정은 그대로 유지한다.

### 3-2. 보안 원칙

- `.env`, `credentials/`, `data/` 는 사용자 요청 없이는 건드리지 않는다.
- onboarding 의 HTTPS 강제, auth rate limiting, secret store 사용은 구조 개선 이후에도 동일하게 유지한다.
- ops/dashboard 응답은 계속 secret scrub 이후에만 외부로 노출한다.
- beta/prod 가 bot token, DB, onboarding URL 을 공유하지 않는 전제는 깨지지 않아야 한다.

### 3-3. 구현 원칙

- 한 번에 대형 재작성하지 않는다.
- 먼저 “순수 계산 로직”을 분리하고, 그 다음 I/O 와 orchestration 을 옮긴다.
- 기존 public function signature 는 가능한 한 유지하고, 내부 위임만 바꾼다.
- `Database` 는 1차적으로 facade 를 유지하고 내부 구현을 쪼갠다.

## 4. 완료 기준

아래를 만족해야 이번 구조 개선 묶음을 완료로 본다.

- 전체 테스트가 녹색이다.
- 현재 실패 중인 Telegram 관련 테스트 2개가 포함된 회귀군이 안정적으로 유지된다.
- `pipeline.py`, `db.py`, `onboarding.py`, `ops_dashboard.py`, `cli.py` 의 책임 경계가 문서상과 코드상에서 일치한다.
- `pipeline.py` 와 `onboarding.py` 의 순환 의존성이 제거된다.
- ops dashboard 의 UI 자산과 서버 로직이 분리된다.
- `Database` 에서 최소한 ops/auth/connections/sync read model 이 별도 모듈로 이동한다.

## 5. 단계별 실행 계획

### Phase 0. 기준선 고정

목표:
- 현재 동작을 테스트로 먼저 고정한다.
- 리팩터링 시작 전에 이미 깨진 Telegram 상태 판정 문제를 복구한다.

산출물:
- Telegram `/setup`, `/today`, `/tomorrow` 회귀 테스트 강화
- 현재 2개 실패 테스트 수정
- 리팩터링용 smoke test 목록 문서화

게이트:
- `./.venv/bin/python -m pytest -q`
- `./.venv/bin/python -m pytest -q tests/test_p3_telegram_commands.py tests/test_ops_dashboard.py tests/test_p3_moodle_onboarding.py tests/test_p0_config_path_resolution.py`

### Phase 1. pipeline 에서 “상태 계산” 분리

목표:
- 문자열 렌더링과 DB/connector 해석을 분리한다.
- Telegram/ops 에서 쓰는 판단 로직을 순수 view-model builder 로 이동한다.

산출물:
- `telegram_setup_state.py` 또는 유사 모듈
- `daily_agenda_state.py` 또는 유사 모듈
- `ops_health_state.py` 또는 유사 모듈
- `pipeline.py` 는 orchestration 과 thin adapters 위주로 축소

게이트:
- Telegram 명령 회귀 테스트 녹색
- setup/day rendering golden test 녹색

### Phase 2. onboarding 서비스 분리 및 cycle 제거

목표:
- onboarding HTTP handler 와 도메인 서비스를 분리한다.
- `pipeline.py <-> onboarding.py` cycle 을 제거한다.

산출물:
- `onboarding_service.py` 또는 `services/onboarding/`
- post-connect portal sync 를 별도 service/protocol 로 분리
- browser session registry 를 전용 컴포넌트로 추출

게이트:
- onboarding helper / UX / portal onboarding 테스트 녹색
- import graph 상 cycle 제거

### Phase 3. Database 내부 분해

목표:
- `Database` 를 유지하면서 내부 read/write 책임을 repo 모듈로 분리한다.

산출물:
- `db_auth_attempts.py`
- `db_connections.py`
- `db_sync.py`
- `db_dashboard_queries.py`
- `Database` 는 facade + transaction boundary 로 축소

게이트:
- `tests/test_db_upserts.py`
- `tests/test_ops_dashboard.py`
- `tests/test_p3_telegram_commands.py`
- `tests/test_p3_onboarding_server_helpers.py`

### Phase 4. ops dashboard 분해

목표:
- ops dashboard 에서 HTML/JS 자산, snapshot builder, action runner, HTTP server 를 분리한다.

산출물:
- 별도 HTML/JS asset 파일
- JSON snapshot builder 모듈
- action runner/service 모듈
- HTTP handler 는 얇은 transport layer 로 축소

게이트:
- ops dashboard snapshot / action tests 녹색
- secret scrub regression tests 녹색

### Phase 5. CLI 모듈화

목표:
- `cli.py` 를 command registration 중심으로 줄이고, 구현은 하위 command module 로 이동한다.

산출물:
- `cli_ops.py`, `cli_onboarding.py`, `cli_launchd.py`, `cli_admin.py` 등
- 루트 `cli.py` 는 Typer wiring 위주

게이트:
- CLI output shape tests 녹색
- launchd/install/onboarding/ops 관련 테스트 녹색

### Phase 6. 정리 및 운영 검증

목표:
- 문서, import graph, smoke test, 배포 체크리스트를 업데이트한다.

산출물:
- 구조 변경 반영 문서
- 모듈 경계 설명
- beta smoke checklist 업데이트

게이트:
- 전체 테스트 녹색
- `doctor`, `status`, `uclass probe`, `ops snapshot` 수동 점검

## 6. 티켓 설계 원칙

각 티켓은 아래 형식을 따른다.

- 목표가 하나여야 한다.
- 파일 write scope 가 명확해야 한다.
- 테스트 추가 또는 갱신이 반드시 포함되어야 한다.
- 보안 경계가 바뀌는 경우 explicit acceptance criteria 를 둔다.
- 한 티켓에서 runtime behavior change 와 대규모 파일 이동을 동시에 하지 않는다.

## 7. 티켓 목록

### RF-00. Telegram 상태 판정 기준선 복구

- 목적:
  현재 실패 중인 Telegram 상태/일정 렌더링 회귀를 먼저 해소하고 기준선을 고정한다.
- 대상 파일:
  `src/sidae_secretary/jobs/pipeline.py`
  `tests/test_p3_telegram_commands.py`
- 작업 범위:
  `/setup` 의 시간표 준비 상태 판단 로직을 실제 공식 API 모드/portal session 상태와 일치시킨다.
  `/today` 빈 상태 메시지 판단이 오래된 timetable 데이터나 비의도적 fallback 에 휘둘리지 않게 한다.
  golden-style regression test 를 보강한다.
- 비범위:
  구조 분해 자체는 하지 않는다.
- 완료 조건:
  현재 failing 2 tests 가 녹색이다.
  Telegram command 전체 회귀군이 녹색이다.
- 의존성:
  없음

### RF-01. Telegram Setup 상태 계산기 추출

- 목적:
  `/setup` 렌더링 전에 필요한 연결 상태 계산을 별도 순수 모듈로 분리한다.
- 대상 파일:
  `src/sidae_secretary/jobs/pipeline.py`
  `src/sidae_secretary/telegram_setup_state.py`
  관련 테스트 파일
- 작업 범위:
  `_chat_lms_connection_snapshot`
  `_sync_dashboard_source_card`
  `_evaluate_uclass_setup_health`
  `_evaluate_portal_setup_health`
  `_format_telegram_setup` 의 상태 계산 부분을 분리한다.
  포맷터는 precomputed state 를 받아 문자열만 만든다.
- 비범위:
  Telegram 명령 dispatcher 구조 변경
- 완료 조건:
  `/setup` 출력 회귀 테스트 녹색
  새 상태 계산 모듈 단위 테스트 추가
- 의존성:
  RF-00

### RF-02. Day Agenda View 모델 분리

- 목적:
  `/today`, `/tomorrow`, `/todaysummary`, `/tomorrowsummary` 의 데이터 조합과 문자열 렌더링을 분리한다.
- 대상 파일:
  `src/sidae_secretary/jobs/pipeline.py`
  `src/sidae_secretary/day_agenda_state.py`
  관련 테스트 파일
- 작업 범위:
  `_format_telegram_day_empty_message`
  `_format_telegram_day`
  `DayBriefService` 와 day empty 판정 사이의 결합을 줄인다.
  “사용자에게 보여줄 agenda state” 를 별도 구조체로 만든다.
- 비범위:
  DayBriefService 전체 재작성
- 완료 조건:
  `/today`/`/tomorrow` 관련 회귀 테스트 녹색
  empty state/first sync/stale state 테스트 추가
- 의존성:
  RF-00

### RF-03. Ops Health 상태 계산기 추출

- 목적:
  ops health surface 계산을 `pipeline.py` 에서 별도 모듈로 분리한다.
- 대상 파일:
  `src/sidae_secretary/jobs/pipeline.py`
  `src/sidae_secretary/ops_health_state.py`
  `tests/test_ops_dashboard.py`
- 작업 범위:
  `build_beta_ops_health_report`
  `_build_uos_official_api_health`
  `_build_uclass_sync_health`
  `_build_telegram_listener_health`
  `_build_telegram_send_health`
  `_build_weather_sync_health`
  `_build_notice_feed_health`
  를 옮긴다.
- 비범위:
  ops dashboard HTML/HTTP layer 분리
- 완료 조건:
  ops health snapshot 테스트 녹색
  import cycle 증가 없음
- 의존성:
  RF-01

### RF-04. Onboarding Application Service 도입

- 목적:
  onboarding flow 에서 HTTP transport 와 도메인 로직을 분리한다.
- 대상 파일:
  `src/sidae_secretary/onboarding.py`
  `src/sidae_secretary/onboarding_service.py`
  `src/sidae_secretary/onboarding_school_connect.py`
  관련 테스트 파일
- 작업 범위:
  credential exchange, onboarding session finalize, Telegram notify, DB update 를 service layer 로 이동한다.
  HTTP handler 는 request parsing / response writing 중심으로 줄인다.
- 비범위:
  HTML 렌더 함수 변경
- 완료 조건:
  onboarding server helper / UX / portal onboarding 테스트 녹색
  secret store, rate limit, HTTPS guard 회귀 테스트 유지
- 의존성:
  RF-00

### RF-05. pipeline-onboarding 순환 의존성 제거

- 목적:
  `pipeline.py` 와 `onboarding.py` 사이의 cycle 을 제거한다.
- 대상 파일:
  `src/sidae_secretary/jobs/pipeline.py`
  `src/sidae_secretary/onboarding.py`
  `src/sidae_secretary/portal_sync_service.py` 또는 동등 모듈
- 작업 범위:
  post-connect portal prime/record 기능을 별도 service/protocol 로 추출한다.
  onboarding 에서 pipeline import 를 하지 않게 만든다.
- 비범위:
  portal sync 로직 자체의 기능 변경
- 완료 조건:
  import graph 상 cycle 제거
  onboarding 및 portal timetable 관련 테스트 녹색
- 의존성:
  RF-04

### RF-06. Database Auth/Connection Repo 분리

- 목적:
  `Database` 에서 onboarding/연결 관련 책임을 별도 repo 로 분리한다.
- 대상 파일:
  `src/sidae_secretary/db.py`
  `src/sidae_secretary/db_auth_attempts.py`
  `src/sidae_secretary/db_connections.py`
  관련 테스트 파일
- 작업 범위:
  `record_auth_attempt`, `count_auth_attempts`, `list_auth_attempts`, `auth_attempt_dashboard_snapshot`
  `upsert_moodle_connection`, `list_moodle_connections`
  `upsert_lms_browser_session`, `list_lms_browser_sessions`
  를 내부 위임 구조로 옮긴다.
- 비범위:
  DB schema 변경
- 완료 조건:
  onboarding/auth/connection 테스트 녹색
  `Database` public API 호환 유지
- 의존성:
  RF-04

### RF-07. Database Sync/Dashboard Query Repo 분리

- 목적:
  `Database` 의 운영용 read model 과 sync dashboard 쿼리를 별도 모듈로 분리한다.
- 대상 파일:
  `src/sidae_secretary/db.py`
  `src/sidae_secretary/db_sync.py`
  `src/sidae_secretary/db_dashboard_queries.py`
  관련 테스트 파일
- 작업 범위:
  `sync_dashboard_snapshot`
  `latest_weather_snapshot`
  `dashboard_snapshot`
  관련 helper 를 read-model 모듈로 이동한다.
- 비범위:
  sync state schema 변경
- 완료 조건:
  ops/dashboard/status 관련 테스트 녹색
  facade 유지
- 의존성:
  RF-03, RF-06

### RF-08. Ops Dashboard UI 자산 분리

- 목적:
  `ops_dashboard.py` 에서 대형 inline HTML/JS 를 분리한다.
- 대상 파일:
  `src/sidae_secretary/ops_dashboard.py`
  `src/sidae_secretary/ops_dashboard_assets/` 또는 동등 경로
  관련 테스트 파일
- 작업 범위:
  `_ops_dashboard_html_template` 를 별도 asset/template 로 이동한다.
  refresh interval injection 만 서버에서 수행한다.
  snapshot/action API 는 그대로 유지한다.
- 비범위:
  UI 재디자인
- 완료 조건:
  HTML 자산 분리 완료
  ops dashboard API/HTML smoke tests 녹색
- 의존성:
  RF-03

### RF-09. Ops Dashboard Snapshot/Action 분리

- 목적:
  ops dashboard 의 데이터 수집, action 실행, HTTP transport 를 분리한다.
- 대상 파일:
  `src/sidae_secretary/ops_dashboard.py`
  `src/sidae_secretary/ops_snapshot_service.py`
  `src/sidae_secretary/ops_action_service.py`
  관련 테스트 파일
- 작업 범위:
  instance discovery, process/log snapshot, user/global audit, action queue 를 서비스 모듈로 추출한다.
  HTTP server 는 request/response adapter 로 축소한다.
- 비범위:
  action 종류 추가
- 완료 조건:
  snapshot/action regression 녹색
  secret scrub regression 녹색
- 의존성:
  RF-08, RF-07

### RF-10. CLI Command Group 모듈화

- 목적:
  `cli.py` 를 command registration 중심으로 줄인다.
- 대상 파일:
  `src/sidae_secretary/cli.py`
  `src/sidae_secretary/cli_ops.py`
  `src/sidae_secretary/cli_onboarding.py`
  `src/sidae_secretary/cli_launchd.py`
  `src/sidae_secretary/cli_admin.py`
  관련 테스트 파일
- 작업 범위:
  ops, onboarding, launchd, admin, verify 계열 command 구현을 하위 모듈로 이동한다.
  루트 `cli.py` 는 Typer wiring 과 공용 helper 일부만 유지한다.
- 비범위:
  CLI 옵션 스펙 변경
- 완료 조건:
  CLI output shape, launchd install, onboarding serve, ops serve 관련 테스트 녹색
- 의존성:
  RF-04, RF-08, RF-09

### RF-11. 최종 정리 및 운영 검증

- 목적:
  구조 개선 이후 문서와 운영 검증 절차를 정리한다.
- 대상 파일:
  `README.md`
  `docs/BETA_RELEASE_CHECKLIST_KO.md`
  `docs/FEATURES_AND_IMPLEMENTATION_KO.md`
  필요 시 추가 문서
- 작업 범위:
  새 모듈 경계, smoke test 순서, 운영자가 확인해야 할 포인트를 문서화한다.
  beta/prod 운영 체크리스트가 새 구조와 일치하도록 갱신한다.
- 비범위:
  새 기능 추가
- 완료 조건:
  관련 문서 업데이트
  구조 변경 후 수동 검증 절차 문서 반영
- 의존성:
  RF-01 ~ RF-10

## 8. 추천 실행 순서

1. RF-00
2. RF-01
3. RF-02
4. RF-03
5. RF-04
6. RF-05
7. RF-06
8. RF-07
9. RF-08
10. RF-09
11. RF-10
12. RF-11

이 순서를 추천하는 이유는 다음과 같다.

- 먼저 사용자 메시지와 상태 판정 회귀를 안정화해야 이후 분해 작업의 안전망이 생긴다.
- 그 다음 `pipeline.py` 에서 순수 계산을 분리하면 onboarding/db/ops 분해 시 충격이 작아진다.
- cycle 제거는 onboarding service 분리 이후에 하는 편이 안전하다.
- `Database` 내부 분해는 view-model 분리 이후에 해야 read/write 경계가 자연스럽다.
- CLI 모듈화는 하위 구현이 정리된 뒤에 해야 단순 파일 이동으로 끝난다.

## 9. 병렬 처리 가능 구간

- RF-01 과 RF-02 는 동시에 진행 가능하다. 단, 동일 helper 함수 write conflict 를 피하도록 파일 경계를 먼저 잡아야 한다.
- RF-08 과 RF-10 은 RF-03 이후 병렬 진행 가능하다.
- RF-06 과 RF-07 은 facade 유지 전제하에 단계 분리를 잘하면 병렬 일부 진행 가능하다.

## 10. 티켓 공통 체크리스트

각 티켓 완료 전 아래를 확인한다.

- [ ] public behavior change 가 있으면 테스트에 먼저 반영했다.
- [ ] 새 모듈이 secrets/token/password 를 직접 문자열로 로그에 남기지 않는다.
- [ ] beta/prod instance 분리 가정이 코드에서 유지된다.
- [ ] onboarding / ops dashboard 응답은 계속 scrub 또는 redaction 을 거친다.
- [ ] `./.venv/bin/python -m pytest -q <관련 회귀군>` 을 통과했다.
- [ ] 필요한 경우 `./.venv/bin/python -m pytest -q` 전체 확인을 수행했다.

## 11. 한 줄 결론

이번 구조 개선은 “대형 파일을 쪼갠다”가 아니라 “상태 계산, I/O, transport, persistence 의 경계를 다시 세운다”가 핵심이다. 실행 순서는 반드시 `기준선 복구 -> 상태 계산 분리 -> onboarding cycle 제거 -> Database 내부 분해 -> ops/cli 분리` 로 가져가는 것이 안전하다.
