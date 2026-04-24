# Public Release Security Plan

이 문서는 서비스 종료 후 사용자가 자기 컴퓨터에서 홈서버 형태로 실행할 수 있도록 공개 저장소를 준비하기 위한 보안 작업 계획이다.

## 목표

- 공개 저장소에는 소스코드, 테스트, 예시 설정, 운영 문서만 남긴다.
- 기존 운영자의 런타임 상태, 토큰, DB, 브라우저 프로필, 내부 배포 경로, 개인 식별자는 남기지 않는다.
- 공개 저장소는 기존 비공개 히스토리를 재사용하지 않고 clean initial commit으로 시작한다.
- 공개 사용자는 `.env.example` 또는 `config.example.toml`을 복사해 자기 환경에서 새로 설정한다.

## 공개 차단 기준

아래 항목이 하나라도 staging에 있으면 공개하지 않는다.

- `.env`, `config.toml`, `data/`, `credentials/`, `*.db`, `*.sqlite*`, `*.lock`
- `secret_store/`, 브라우저 프로필, `Cookies`, `Login Data`, Local Storage, IndexedDB
- `export*.json`, 백업 zip, 운영 DB dump
- 내부 배포 메타데이터, 로컬 절대경로, private mesh host, 실제 운영 사용자명
- 실제 Telegram chat id, 학번처럼 보이는 fixture, 운영 onboarding URL
- 과거 비공개 commit history

## 개발 티켓

### PUB-01 Legacy External Auth Surface Removal

- 상태: 완료 대상
- 범위:
  - 쓰지 않는 외부 일정/리마인더 인증 안내와 설정 문구 제거
  - 해당 launchd 재시작 훅 제거
  - DB migration의 과거 통합명 문자열 제거
  - 관련 테스트 fixture 정리
- 완료 조건:
  - `git grep -i`로 관련 서비스명, 토큰 파일명, deprecated env flag가 검색되지 않는다.
  - 전체 테스트가 통과한다.

### PUB-02 Runtime Artifact Quarantine

- 상태: 필수
- 범위:
  - 운영 호스트의 prod/beta 앱 트리에서 `.env.bak*`, `.env.save`, export dump, 브라우저 프로필을 삭제 또는 별도 암호화 보관
  - `.env`, DB, secret-store 파일 권한을 owner-only로 낮춤
  - 서비스 종료 후 더 이상 필요 없는 토큰은 회전 또는 폐기
- 완료 조건:
  - 앱 트리에서 백업 env 파일과 브라우저 프로필이 발견되지 않는다.
  - 운영 DB와 secret-store는 public staging 경로 밖에만 존재한다.

### PUB-03 Public Docs And Fixtures Scrub

- 상태: 필수
- 범위:
  - README, 예시 config, 테스트 fixture에서 내부 host/user/path를 placeholder로 교체
  - 실제처럼 보이는 학번, chat id, public onboarding URL fixture를 더미값으로 교체
  - 오래된 audit/snapshot 산출물의 로컬 경로 제거
- 완료 조건:
  - 내부 절대경로, private host, 실제처럼 보이는 사용자 식별자가 검색되지 않는다.

### PUB-04 Release Sanitizer Hardening

- 상태: 필수
- 범위:
  - sanitizer가 `.codex`, `.DS_Store`, cache, runtime data, credential folders, browser profiles, DB, lock, export dump를 차단
  - staging 검증 명령을 release checklist에 고정
- 완료 조건:
  - `./.venv/bin/python scripts/sanitize_release.py create dist/public --force`
  - `./.venv/bin/python scripts/sanitize_release.py validate dist/public`
  - staging 내부에서 차단 기준 항목이 검색되지 않는다.

### PUB-05 Clean Initial Commit Build

- 상태: 필수
- 범위:
  - sanitized staging을 새 디렉터리로 복사
  - 새 Git 저장소를 초기화하고 기존 private remote/history를 가져오지 않음
  - 공개용 `origin`만 연결
- 완료 조건:
  - `git log --oneline`이 public initial commit 하나에서 시작한다.
  - `git remote -v`에 private deploy remote가 없다.

### PUB-06 Verification Gate

- 상태: 필수
- 범위:
  - default test suite
  - high-confidence secret pattern scan
  - tracked-file path and host scan
  - staging validation
- 완료 조건:
  - 테스트 통과
  - token/key 패턴 없음
  - runtime artifact 없음
  - 내부 경로/host 없음

### PUB-07 Service Shutdown And Token Disposal

- 상태: 공개 직전
- 범위:
  - prod/beta launchd job 중지
  - public onboarding/proxy route 제거
  - Telegram bot token, relay secret, 학교 계정 비밀번호, UOS API key를 사용 종료 정책에 맞게 폐기 또는 회전
  - 운영 DB와 사용자 자료의 보관/삭제 정책 확정
- 완료 조건:
  - 외부에서 `/connect`, relay, ops dashboard에 접근할 수 없다.
  - 폐기 대상 토큰으로 API 호출이 되지 않는다.

## 최종 공개 절차

1. private 작업트리에서 모든 제거/스크럽 티켓을 완료한다.
2. 테스트와 sanitizer 검증을 통과시킨다.
   - `./.venv/bin/python -m pytest -q`
   - `./.venv/bin/python scripts/sanitize_release.py create dist/public --force`
   - `./.venv/bin/python scripts/sanitize_release.py validate dist/public`
3. `dist/public` 을 새 공개 작업 디렉터리로 복사한다.
4. 공개 작업 디렉터리 안에서 새 Git 저장소를 초기화한다.
   - `git init`
   - `git add .`
   - `git commit -m "Public initial commit"`
5. 공개용 `origin` 만 추가한다. 기존 private remote/history 는 가져오지 않는다.
6. 공개 원격에 push하기 전 다시 sanitizer validation과 secret/path scan을 실행한다.
