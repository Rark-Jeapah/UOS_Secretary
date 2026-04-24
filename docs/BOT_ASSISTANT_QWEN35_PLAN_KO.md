# `/bot` beta 실사용 및 Gemma 4 개선 계획

## 목적

- beta 는 `/bot`을 실제로 쓰는 검증 환경으로 운영한다.
- prod 는 beta 에서 검증한 동일 커밋만 승격한다.
- 로컬 Gemma 4 경로를 유지하되, 느리고 흔들리는 자유형 planner 대신 더 좁고 안정적인 assistant 로 다듬는다.

## 현재 확인된 문제

- 실제 활성 설정과 beta 예시 설정 모두 `/bot` 기본값이 꺼져 있었다.
- 현재 capability 범위는 `today/tomorrow/weather`, 날씨 지역 변경, 1회성 리마인더, 알림 정책, capability 설명 정도로 좁다.
- 정책 문서에 적힌 `/bot inbox ...`, `/bot task 42 ...`, `/bot 지금 설정 상태 ...` 는 구현 범위와 맞지 않는다.
- 복합 질의에서 planner 가 과잉 action 을 붙인다.
  - 예: `오늘 일정이랑 날씨 알려줘` 에 `내일 날씨`, `내일 일정`까지 같이 실행됨.
- 비지원/파괴적 요청이 명시적 거절이 아니라 clarification 으로 흘러 안전 경계가 약하다.
- local Gemma 4 호출은 beta 체감 속도와 안정성을 기준으로 계속 검증한다.

## 설계 원칙

- 자주 쓰는 요청은 LLM 앞단에서 deterministic fast path 로 먼저 처리한다.
- Gemma 4 에는 작은 schema 와 좁은 capability 집합만 보여준다.
- unsupported 와 destructive 요청은 clarification 이 아니라 refusal 로 분리한다.
- multi-action 은 요청 슬롯과 day scope 를 검증한 뒤 후처리로 잘라낸다.
- beta 에서는 write path 도 켜서 실제 사용성을 본다. prod 승격 전까지는 beta 에서만 검증한다.

## 단계별 개발 계획

### 1. 배포 및 운영 기준 정리

- beta 권장 설정을 `TELEGRAM_ASSISTANT_ENABLED=true`, `TELEGRAM_ASSISTANT_WRITE_ENABLED=true`, `LLM_ENABLED=true` 로 고정한다.
- `doctor` 에서 beta 인스턴스가 `/bot` 실사용 기준을 만족하지 않으면 경고한다.
- beta release checklist 에 `/bot` read/write smoke 를 추가한다.

### 2. Gemma 4 최적화용 라우팅 계층

- `router` fast path 를 추가한다.
  - `오늘 일정`, `내일 일정`, `오늘 날씨`, `내일 날씨`, `날씨 지역`, `내일 오전 8시 알림` 같은 고빈도 패턴은 regex/slot parser 로 바로 capability 를 만든다.
- fast path 가 실패한 요청만 Gemma 4 planner 로 넘긴다.
- planner prompt 에는 전체 제품 설명 대신 현재 허용 capability 와 예시 몇 개만 준다.
- JSON only 출력은 유지하고, temperature 는 낮게 유지한다.
- day scope validator 를 둬서 `오늘` 요청에 `내일` action 이 섞이면 잘라낸다.

### 3. capability 확장

- `query_setup_status`
  - `/setup` 내용을 `/bot` 경로에서도 읽기 전용으로 노출한다.
- `preview_inbox_apply`
  - inbox 초안을 요약하고, 적용 전 확인 질문을 만든다.
- `apply_inbox_item`
  - 명시적 대상 ID 또는 `all` 이 확인된 경우만 실행한다.
- `mark_task_done`
  - `task 42 완료` 같은 요청을 기존 done 계약으로 연결한다.
- `reject_unsupported`
  - 수강신청 대행, 전체 삭제, 지원 범위 확대 같은 요청은 즉시 거절 메시지로 처리한다.

### 4. 확인 질문과 안전 경계 정리

- clarification 과 refusal 을 분리한다.
  - clarification: 시간, 대상, 메시지가 빠진 경우
  - refusal: 현재 지원 범위 밖이거나 파괴적 요청인 경우
- write action 은 capability 별로 required slot 이 모두 차지 않으면 실행하지 않는다.
- confirmation 이 필요한 action 은 "확인 질문 전용 planner 출력" 을 허용하되, executor 는 실행하지 않는다.

### 5. Gemma 4 품질 개선

- planner prompt 에 negative examples 를 넣는다.
  - `오늘` 요청에 `내일` action 금지
  - unsupported 요청은 clarification 대신 refusal
  - `설정 상태` 요청을 capability 설명으로 바꾸지 말 것
- capability 별 few-shot 을 최소 1개씩 추가한다.
  - schedule query
  - weather query
  - weather region mutation
  - one-time reminder
  - notification policy
  - inbox preview
  - task done
- planner output 에 optional `reason_code` 를 추가해 오매핑 분류를 쉽게 한다.
- `assistant_runs` 를 평가용 로그로 활용해 prompt revision 전후를 비교한다.

### 6. 평가 체계

- replay prompt fixture 를 만든다.
  - 조회형
  - 단일 write
  - recurring policy
  - clarification 필요
  - unsupported/destructive
  - mixed Korean/English
- 합격 기준:
  - supported request routing accuracy 95%+
  - unsupported refusal precision 100%
  - destructive request execution 0건
  - `오늘`/`내일` day scope leak 0건
- beta smoke:
  - `/bot 오늘 일정 알려줘`
  - `/bot 오늘 날씨랑 미세먼지 요약해줘`
  - `/bot 동대문구로 날씨 지역 바꿔줘`
  - `/bot 내일 오전 8시에 테스트 알림해줘`

### 7. 성능 목표

- fast path hit 요청은 1초 이내 목표
- weather/cache read 는 2초 이내 목표
- Gemma 4 planner fallback 은 8초 이내 목표
- 15초 초과 시 operator warning 또는 timeout fallback 추가

## 구현 순서

1. beta config / doctor / release checklist 정리
2. refusal vs clarification 분리
3. deterministic fast path 추가
4. capability 확장: setup, inbox preview, task done
5. multi-action validator 추가
6. Gemma 4 prompt and eval fixtures 정리
7. beta 실사용 로그 기준으로 2차 prompt tuning
