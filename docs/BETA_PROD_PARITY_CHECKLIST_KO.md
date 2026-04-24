# beta/prod parity 체크리스트

이 문서는 "beta 에서 검증한 내용이 prod 와 정말 같은가"를 빠르게 확인하기 위한 점검표다. 배포 게이트는 `docs/BETA_RELEASE_CHECKLIST_KO.md`를 따르고, 이 문서는 beta/prod 설정 차이로 생기는 운영 편차를 줄이는 데 집중한다.

## 1. 반드시 분리할 것

- [ ] app tree 가 다르다: prod `/path/to/apps/UOS_secretary`, beta `/path/to/apps/UOS_secretary_beta`
- [ ] `INSTANCE_NAME` 이 다르다: prod 빈 값, beta `"beta"`
- [ ] `DATABASE_PATH`, `STORAGE_ROOT_DIR`, Telegram bot token 이 다르다
- [ ] `ONBOARDING_PUBLIC_BASE_URL` 이 다르다
- [ ] launchd label 이 분리돼 있다: prod unsuffixed, beta `.beta`

## 2. beta 가 prod 검증 역할을 하려면 맞춰야 할 것

- [ ] beta 와 prod 가 같은 git commit 을 가리킨다
- [ ] bare repo hook 이 `beta -> /path/to/apps/UOS_secretary_beta`, `deploy -> /path/to/apps/UOS_secretary` 로 매핑돼 있다
- [ ] `UCLASS_DOWNLOAD_MATERIALS` 값이 같다
- [ ] `MATERIAL_EXTRACTION_ENABLED` 값이 같다
- [ ] `MATERIAL_BRIEFING_ENABLED` 값이 같다
- [ ] `SCHEDULED_BRIEFINGS_ENABLED` 기본값/사용자 preference 기대치가 같다
- [ ] beta 에서 실제로 쓰는 onboarding URL 과 prod onboarding URL 이 서로 다른 인스턴스를 가리킨다

## 3. 이번 장애에서 확인된 실제 함정

- beta/prod 코드 분리는 정상이어도, 알림 관련 feature flag parity 가 다르면 beta 는 prod 의 실제 사용자 경험을 재현하지 못한다.
- UClass `mod/*/view.php` 는 파일 본문이 아니라 container page 인 경우가 많아서 material candidate 로 취급하면 로그인 HTML 또는 `requireloginerror` 로 실패할 수 있다.
- owner 계정만 global `UCLASS_USERNAME/UCLASS_PASSWORD` fallback 을 타고, 다른 사용자는 token-only 경로를 타면 prod 안에서도 사용자별 성공/실패가 갈릴 수 있다.

## 4. 이번 패치 이후 운영 메모

- school account onboarding 은 per-user UClass HTML fallback 을 위해 비밀번호를 해당 사용자용 secret store 에도 저장한다.
- 기존 연결 사용자는 `login_secret_kind/login_secret_ref` 가 비어 있으므로, 이 패치를 배포한 뒤 한 번 `/connect` 로 다시 연결해야 per-user fallback 이 활성화된다.
- remote browser onboarding 으로 token 만 저장된 기존 연결은 자동으로 password secret 이 생기지 않는다.

## 5. 빠른 확인 명령

- `git ls-remote --heads <deploy-remote>`
- `./.venv/bin/python -m sidae_secretary.cli doctor --config-file <beta-config>`
- `./.venv/bin/python -m sidae_secretary.cli doctor --config-file <prod-config>`
- `./.venv/bin/python -m sidae_secretary.cli status --config-file <beta-config>`
- `./.venv/bin/python -m sidae_secretary.cli status --config-file <prod-config>`
