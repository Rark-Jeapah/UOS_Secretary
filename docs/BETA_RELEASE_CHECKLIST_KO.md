# 닫힌 베타 릴리스 게이트 및 배포 체크리스트

이 문서는 배포 ref push 전에 보는 운영용 체크리스트다. 모든 명령은 저장소 루트에서 `./.venv/bin/python` 기준으로 실행한다.

## 0. 대상 확인

- [ ] 이번 작업이 beta 배포인지, beta 검증을 끝낸 뒤 prod 승격인지 먼저 결정했다.
- [ ] 대상 인스턴스의 실제 설정 소스를 확인했다: `config.toml`, `.env`, 또는 둘 다.
- [ ] beta 는 prod 와 다른 app tree 를 사용하고 `INSTANCE_NAME = "beta"` 로 분리되어 있다.
- [ ] beta 와 prod 가 DB, `STORAGE_ROOT_DIR`, Telegram bot token, public onboarding URL 을 공유하지 않는다.
- [ ] beta 에서 `/bot` 을 실제로 검증할 계획이면 `TELEGRAM_ASSISTANT_ENABLED=true`, `TELEGRAM_ASSISTANT_WRITE_ENABLED=true`, `LLM_ENABLED=true` 로 맞췄다.
- [ ] beta/prod parity 는 `docs/BETA_PROD_PARITY_CHECKLIST_KO.md` 기준으로 다시 확인했다.

## 1. 릴리스 게이트

### 1-1. Release sanitization

- [ ] `./.venv/bin/python scripts/sanitize_release.py create dist/beta-release --force`
- [ ] `./.venv/bin/python scripts/sanitize_release.py validate dist/beta-release`
- [ ] 배포용 확인은 작업 트리 대신 `dist/beta-release` 기준으로 했다.

### 1-1-public. Public release sanitization

- [ ] `./.venv/bin/python scripts/sanitize_release.py create dist/public --force`
- [ ] `./.venv/bin/python scripts/sanitize_release.py validate dist/public`
- [ ] 공개용 확인은 작업 트리 대신 `dist/public` 기준으로 했다.

### 1-2. Beta-critical tests

- [ ] `./.venv/bin/python -m pytest -q -m beta_critical`
- [ ] `./.venv/bin/python -m sidae_secretary.cli uclass probe --config-file <beta-config>`

### 1-3. Health / status

- [ ] `./.venv/bin/python -m sidae_secretary.cli doctor --config-file <beta-config>`
- [ ] `./.venv/bin/python -m sidae_secretary.cli status --config-file <beta-config>`
- [ ] `./.venv/bin/python -m sidae_secretary.cli ops snapshot --config-file <beta-config>`
- [ ] `status` JSON 에서 `health.overall_ready = true` 이고 beta-critical surface 에 `error` 가 없다.
- [ ] `status` JSON 에서 `runtime.python.ok = true` 이다.
- [ ] `ops snapshot` JSON 에서 `headline.tone != "err"` 이고 `instances[*].load_error` 가 없다.
- [ ] `ops snapshot` JSON 에서 대상 인스턴스의 `health_summary.not_ready_count` 와 `services.counts` 가 예상과 크게 다르지 않다.

### 1-4. Operator smoke checks

- [ ] `/connect` 또는 onboarding 설정을 건드렸다면 먼저 local/public onboarding 경로를 비교했다.
  - local: `http://127.0.0.1:8791/...`
  - public: `ONBOARDING_PUBLIC_BASE_URL/...`
- [ ] beta 허용 채팅에서 `/setup` 을 먼저 확인했다.
- [ ] beta 허용 채팅에서 Telegram 명령 1개만 추가 확인했다: `/today` 또는 `/status`
- [ ] beta 허용 채팅에서 `/bot 오늘 일정 알려줘` 를 확인했다.
- [ ] beta assistant 쓰기 경로를 열어 둔 경우 되돌리기 쉬운 `/bot` write smoke 1개를 확인했다.
  - 예: `/bot 동대문구로 날씨 지역 바꿔줘`
- [ ] `./.venv/bin/python -m sidae_secretary.cli publish --config-file <beta-config>` 를 실행했다.
- [ ] 위 출력의 `telegram_briefing_files.items.*.text_path` 중 하나를 열어 브리핑 문구를 수동 미리보기했다.
- [ ] ops dashboard 를 켜 둔 인스턴스면 `/api/snapshot` 또는 `sidae ops open-remote` 로 한 번 더 operator view 를 확인했다.

## 2. Beta deploy

1. 위 릴리스 게이트가 모두 통과한 뒤에만 beta 로 올린다.
2. beta 배포는 `git push <deploy-remote> HEAD:beta` 만 사용한다.
3. push 후 beta 인스턴스에서 다시 Telegram 명령 1회와 브리핑 미리보기 1회를 확인한다.
4. onboarding 설정을 바꿨으면 beta `telegram-listener` 와 `onboarding` 잡을 재시작한다.
5. ops dashboard 설정이나 bind 정보를 바꿨으면 beta `ops-dashboard` 잡도 재시작한다.
6. beta 검증 중 `/setup`, `/today`, ops snapshot 세 결과가 같은 인스턴스를 보고 있는지 다시 확인한다.
7. beta 검증이 끝나기 전에는 `deploy` 브랜치로 올리지 않는다.

## 3. Prod promotion

1. prod 는 beta 에서 검증된 동일 커밋만 승격한다.
2. `git rev-parse HEAD` 로 현재 `HEAD` 가 beta 에서 검증한 커밋인지 확인한 뒤 `git push <deploy-remote> HEAD:deploy` 로 승격한다.
3. prod 설정이 바뀌었다면 prod 설정 파일로 `doctor` 와 `status` 를 다시 확인한다.
4. prod 설정이 바뀌었다면 prod 설정 파일로 `ops snapshot` 도 다시 확인한다.
5. onboarding 설정을 바꿨으면 prod `telegram-listener` 와 `onboarding` 잡을 재시작한다.
6. ops dashboard 설정이나 bind 정보를 바꿨으면 prod `ops-dashboard` 잡도 재시작한다.
7. 승격 후 prod bot 에서 `/setup` 과 Telegram 명령 1회를 확인하고, beta bot 이 아닌 prod bot/URL 인지 재확인한다.
