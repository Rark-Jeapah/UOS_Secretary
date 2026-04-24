# 주요 대학 LMS 구조 조사 및 확장 계획

기준일: 2026-03-10

## 1. 왜 먼저 구조를 나눠야 하나

현재 구현은 학교 이름 기준이 아니라 사실상 `시립대 UClass = Moodle Web Service + HTML fallback` 구조에 맞춰져 있다.

- 설정도 `uclass_*` 중심이다: `src/sidae_secretary/config.py`
- 인증과 토큰 발급도 Moodle 전제다: `src/sidae_secretary/jobs/pipeline.py`
- HTML 수집도 `login/index.php`, `my/courses.php`, `course/view.php`, `mod/ubboard`, `pluginfile.php` 패턴에 묶여 있다: `src/sidae_secretary/connectors/uclass.py`

즉, 다음 학교를 붙일 때 중요한 기준은 "학교명"이 아니라 아래 3가지다.

1. 어떤 LMS 제품군인가
2. 로그인과 인증 토큰을 프로그램이 얻을 수 있는가
3. 공지/과제/자료 URL이 서버 렌더링 HTML인지, JSON API인지, 브라우저 앱인지

## 2. 주요 대학 현황 요약

아래 표의 "현재 구조 판단"에는 공식 매뉴얼/공지에 제품명이 직접 적힌 경우와, 공개 첫 화면 HTML 구조를 바탕으로 추정한 경우가 함께 포함된다.

### A. Moodle 계열 또는 Moodle 파생 계열

| 학교 | 공식 서비스 | 현재 구조 판단 | 현재 코드 재사용성 |
| --- | --- | --- | --- |
| 서울시립대 | UClass | Moodle + Ubion 계열로 구현되어 있음 | 매우 높음 |
| 연세대 | LearnUs | 공식 페이지 HTML에 `login/index.php`, `theme/styles.php`, `theme=coursemosv2`, `local/ubion`, `mod/ubboard` 노출. Moodle + Coursemos/Ubion 계열로 판단 | 높음 |
| 부산대 | PLATO | 공식 페이지에 `moodle`, `coursemos`, `login/index.php`, `local/ubion`, `mod/ubboard` 노출 | 높음 |
| KAIST | KLMS | 공식 안내에서 "Moodle 기반의 KLMS"라고 명시 | 중간 이상 |

### B. LearningX/XN 계열

| 학교 | 공식 서비스 | 현재 구조 판단 | 현재 코드 재사용성 |
| --- | --- | --- | --- |
| 서울대 | eTL | 공식 eTL 첫 화면이 `/api/v1` 기반이고 `myetl.snu.ac.kr/learningx/...` 링크와 `구 eTL` 링크를 함께 제공. 현재 eTL은 LearningX 계열, 구 eTL은 별도 레거시 | 중간 이하 |
| 고려대 | KULMS | 공식 공지에서 Blackboard 종료 후 LearningX 기반 안내 자료 제공 | 중간 이하 |
| 경희대 | e-campus | 공식 자료가 `LearningX Student 앱` 안내 중심 | 중간 이하 |
| 성균관대 | i-Campus | 공식 자료가 `LearningX Student 앱` 안내 중심 | 중간 이하 |
| 한양대 | HY-ON | 공식 자료가 `LearningX Student` 안내 중심 | 중간 이하 |
| 중앙대 | CAU-ON | 공식 사이트가 `/api/v1`, `xn-sso` 패턴을 사용. 도메인은 `canvas.cau.ac.kr`지만 Instructure Canvas 흔적은 없고 XN/LearningX 계열과 유사 | 중간 이하 |

## 3. 구조적으로 중요한 해석

### 3.1 국내 주요 대학은 생각보다 "완전히 제각각"은 아니다

현재 확인된 학교들은 크게 두 패밀리로 묶인다.

- `Moodle/Ubion/Coursemos`
  - 시립대, 연세대, 부산대
- `LearningX/XN`
  - 서울대, 고려대, 경희대, 성균관대, 한양대, 중앙대

이 말은 "학교마다 새로 만든다"보다 "패밀리별 어댑터를 만든다"가 맞다는 뜻이다.

### 3.2 다만 Moodle이라고 바로 붙지는 않는다

현재 코드는 Moodle 자체뿐 아니라 Ubion 계열 게시판/자료 구조까지 일부 가정한다.

- `mod/ubboard`
- `mod/ubfile`
- `pluginfile.php`
- `course/view.php?id=...`

그래서 같은 Moodle이라도 학교별 차이를 다음처럼 나눠야 한다.

- `Moodle Core만 사용`
- `Moodle + Ubion 확장`
- `Moodle지만 WS 토큰 발급 제한`
- `Moodle지만 SSO 때문에 로그인 흐름이 다름`

### 3.3 LearningX는 별도 어댑터로 보는 게 맞다

LearningX 계열 학교들은 첫 화면부터 공통적으로 아래 성격이 보인다.

- `/api/v1` 기반 프런트
- `redirect/lms` 또는 별도 `mylms` 이동
- `xn-sso` 계열 로그인
- 공식 안내가 `LearningX Student` 앱 중심

즉, 지금의 Moodle 전용 토큰 발급기와 HTML regex 수집기를 억지로 늘리기보다 별도 `LearningXAdapter`를 두는 편이 맞다.

## 4. 우리 프로젝트에 맞는 지원 전략

### 4.1 우선순위

1. `Moodle 계열 확장`
2. `LearningX 계열 공통 어댑터`
3. `학교별 예외 처리`

이 순서가 좋은 이유는 명확하다.

- 현재 코드 자산을 가장 많이 재사용할 수 있다.
- 빠르게 2~4개 학교를 늘릴 수 있다.
- LearningX는 묶어서 해결해야 이후 유지보수가 줄어든다.

## 5. 구현 계획

### Phase 0. UClass 전용 이름부터 일반화

목표: "시립대 전용 커넥터"를 "학교별 LMS 커넥터 프레임"으로 바꾼다.

- `uclass_*` 설정을 유지하되 내부적으로는 `lms_*` 추상 레이어를 추가
- DB `source` 값도 `uclass_ws`, `uclass_html` 같은 이름에서 `lms_ws`, `lms_html` 또는 `school_slug:lms_kind` 형태로 확장 가능하게 설계
- `sync_uclass()`는 내부에서 `sync_lms(adapter=...)`를 호출하도록 분리

### Phase 1. Moodle 공통 어댑터 분리

목표: 시립대 구현에서 "학교 특화 부분"을 떼어낸다.

- `MoodleBaseAdapter`
  - 로그인
  - WS token 발급
  - site info / courses / assignments / contents / forums
  - generic `pluginfile.php` 다운로드
- `MoodleUbionAdapter`
  - `ubboard`, `ubfile`, `local/ubion` 같은 확장 HTML 수집
- `SchoolPreset`
  - base URL
  - auth mode
  - wsfunction 이름
  - html selectors/regex

Phase 1의 1차 대상:

- 연세대 LearnUs
- 부산대 PLATO
- KAIST KLMS

이 셋은 현재 코드와 가장 가까워서 성공 확률이 높다.

### Phase 2. LearningX 공통 어댑터 추가

목표: 서울대/고려대/경희대/성균관대/한양대/중앙대를 하나의 패밀리로 묶는다.

- `LearningXAdapter`
  - 로그인 흐름 조사
  - course list / notices / assignments / materials 접근 경로 파악
  - 파일 다운로드 URL 규칙 정리
- 브라우저 의존이 필요하면 requests-only와 browser-assisted를 분리
- `probe` 명령으로 학교별 응답 shape를 먼저 저장

Phase 2의 1차 대상:

- 서울대 eTL
- 고려대 KULMS

이 둘은 공식 자료와 공개 구조가 가장 많이 보이고, 다른 LearningX 학교에도 재사용될 가능성이 크다.

### Phase 3. 학교별 운영 검증

- 학교별 테스트 계정 1개 이상 확보
- 다음 항목을 실제로 검증
  - 로그인 성공
  - 수강 과목 목록 수집
  - 공지 수집
  - 과제/일정 수집
  - 강의자료 다운로드
  - 중복 다운로드 회피

지원 상태는 아래처럼 나누는 게 좋다.

- `Tier 1`: 과목/공지/과제/자료까지 자동 수집
- `Tier 2`: 과목/공지/과제만 지원
- `Tier 3`: 브라우저 보조가 있을 때만 지원

## 6. 바로 해야 할 실무 액션

1. 설정/코드에서 `uclass`라는 이름을 인터페이스 뒤로 숨긴다.
2. `lms probe --preset <school>` 형태의 범용 진단 명령을 만든다.
3. 학교 preset 파일을 만든다.
4. 연세대와 부산대를 첫 번째 외부 학교로 붙인다.
5. 그 다음 서울대 또는 고려대를 골라 LearningX 어댑터를 시작한다.

## 7. 추천 지원 순서

1. 연세대 LearnUs
2. 부산대 PLATO
3. KAIST KLMS
4. 서울대 eTL
5. 고려대 KULMS
6. 경희대 / 성균관대 / 한양대 / 중앙대

이 순서를 추천하는 이유:

- 1~3은 현재 Moodle 자산 재사용성이 높다.
- 4~6은 LearningX 공통 어댑터가 생긴 뒤 급격히 쉬워질 가능성이 높다.

## 8. 조사 근거

- 연세대 LearnUs 공식 페이지: [ys.learnus.org](https://ys.learnus.org/)
- 부산대 PLATO 공식 페이지: [plato.pusan.ac.kr](https://plato.pusan.ac.kr/)
- KAIST 공식 안내: [KLMS 소개](https://ctl.kaist.ac.kr/pages/sub/sub02_01.php)
- 서울대 eTL 공식 페이지: [etl.snu.ac.kr](https://etl.snu.ac.kr/)
- 서울대 공식 안내: [LearningX Student 앱 사용 방법](https://brain.snu.ac.kr/ko/notice/all?bm=v&bbsidx=308&cidx=)
- 고려대 공식 안내: [KULMS 도입 공지](https://kfrs.korea.ac.kr/bbs/kfrs/298/256495/artclView.do)
- 고려대 공식 매뉴얼: [LearningX Guide PDF](https://cwms.korea.ac.kr/bbs/cwms/505/165409/download.do)
- 경희대 공식 안내: [LearningX Student 앱 안내 PDF](https://info21.khu.ac.kr/com/KHU_DOWNLOAD.khu?loc=info21/resource/upload/2024/02/21623877332674338620.pdf&fileNm=LearningX%20Student%20%EC%95%B1%20%EC%95%88%EB%82%B4.pdf)
- 성균관대 공식 안내: [LearningX Student 앱 사용 안내](https://webzine.skku.edu/skkuzine/section/coverStory.do?mode=view&articleNo=81853)
- 한양대 공식 안내: [HY-ON LearningX Student 앱 사용 안내](https://gspp.hanyang.ac.kr/front/information/download/read?id=nX50XNivTRC_29dnJPBBkA&sd=12&so=CREATED_AT_DESC&sp=1)
- 중앙대 공식 페이지: [CAU-ON](https://canvas.cau.ac.kr/)

## 9. 한 줄 결론

학교별 대응으로 가면 금방 복잡해진다. `Moodle 계열 먼저 공통화 -> LearningX 계열 어댑터 추가 -> 학교 preset 확장` 순서로 가는 게 가장 빠르고 오래간다.
