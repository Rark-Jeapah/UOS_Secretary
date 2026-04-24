# 학교 계정 기반 LMS/포털 조사 메모

기준일: 2026-03-12

## 목적

`/connect` 한 번으로 학교 계정을 받아 LMS를 연결하고, 같은 계정을 쓰는 공식 포털까지 함께 다루기 위한 학교별 조사 결과다.

## 현재 구현 상태

- 서울시립대학교: 포털 세션 생성과 강의시간표 파싱까지 구현됨
- 그 외 학교: 공식 LMS/포털 링크와 "같은 학교 계정 사용" 메타데이터를 등록함
- 제약: 포털 세션 자동 로그인과 시간표 수집은 학교별 SSO, 2차 인증, 시간표 조회 화면 구조 때문에 별도 구현이 필요함

## 학교별 현황

| 학교 | LMS | 포털/학사 | 현재 판단 | 제약 |
| --- | --- | --- | --- | --- |
| 연세대 | [LearnUs](https://ys.learnus.org/) | [연세포털서비스](https://portal.yonsei.ac.kr/main/index.jsp) | LearnUs와 포털이 같은 학교 계정을 쓰는 것으로 확인 | 포털 시간표 엔드포인트 미확인 |
| 부산대 | [PLATO](https://plato.pusan.ac.kr/) | [학생지원시스템](https://onestop.pusan.ac.kr/) | PLATO와 학사 시스템이 같은 계정 흐름을 공유 | 학생지원시스템 시간표 API 미확인 |
| GIST | [GIST LMS](https://lms.gist.ac.kr/) | [GIST Portal](https://portal.gist.ac.kr/gateway/login.jsp) | 공식 안내에서 LMS/포털이 같은 portal account/password 사용 | 학사/시간표 메뉴 자동화 미구현 |
| 전북대 | [JBNU LXP](https://lms.jbnu.ac.kr/) | [포털](https://portal.jbnu.ac.kr/web/index.do), [OASIS](https://oasis.jbnu.ac.kr/) | LMS와 포털/OASIS가 같은 계정 체계 | OASIS 별도 시스템 검증 필요 |
| 동국대 | [이클래스](https://eclass.dongguk.edu/) | [nPortal](https://nportal.dongguk.edu/), [nDRIMS](https://ndrims.dongguk.edu/) | 포털과 학사 시스템 연동 구조 확인 | 2차 인증 개입 가능 |
| 인천대 | [INU LMS](https://cyber.inu.ac.kr/) | [포털](https://portal.inu.ac.kr/login.jsp) | 포털 계정 기반 흐름 존재 | 수강시간표 조회 화면 미확인 |
| 가천대 | [Cyber Campus](https://cyber.gachon.ac.kr/) | [포털](https://portal.gachon.ac.kr/) | 포털과 사이버캠퍼스가 같은 학교 계정 흐름 | 시간표/강의실 데이터 경로 미확인 |
| 상명대 | [e-Campus](https://ecampus.smu.ac.kr/) | [샘물포털](https://portal.smu.ac.kr/) | 포털과 e-Campus 통합 계정 사용 | 포털 시간표 조회 구조 미확인 |
| 덕성여대 | [U-Class](https://lms.duksung.ac.kr/) | [포털](https://portal.duksung.ac.kr/) | 포털과 U-Class 공용 계정으로 판단 | 포털 시간표 자동화 미구현 |
| 대진대 | [e-Class](https://eclass.daejin.ac.kr/) | [포털대진 안내](https://www.daejin.ac.kr/daejin/intro/intro_04.do) | 포털대진 존재 확인 | 직접 로그인 URL과 런타임 흐름 미확인 |
| 서울시립대 | [온라인강의실](https://uclass.uos.ac.kr/) | [포털/대학행정](https://portal.uos.ac.kr/p/STUD/) | LMS + 포털 세션 + 시간표 수집 구현 완료 | 운영 중 |
| KAIST | [KLMS](https://klms.kaist.ac.kr/) | [KAIST Portal](https://portal.kaist.ac.kr/) | 공식 안내에서 KLMS가 portal ID 사용 | 포털 시간표 API 미구현 |
| 한림대 | [SmartLEAD](https://smartlead.hallym.ac.kr/) | [포털](https://portal.hallym.ac.kr/), [통합정보시스템](https://was1.hallym.ac.kr:8087/) | LMS와 통합정보시스템 계정 연동 | 시간표 조회 URL 미확인 |

## 구현상 해석

- 이번 반영으로 `/connect`는 학교별 공식 LMS를 기본 대상으로 사용한다.
- 학교 디렉터리에는 공식 포털 링크와 제약을 함께 저장한다.
- 시립대를 제외한 학교는 아직 "공식 포털 링크를 알고 같은 계정을 쓴다" 단계이고, 실제 포털 세션/시간표 수집은 학교별 후속 구현이 필요하다.

## 후속 우선순위

1. 연세대 LearnUs + 연세포털
2. 부산대 PLATO + 학생지원시스템
3. GIST LMS + GIST Portal
4. 전북대 JBNU LXP + OASIS

위 4개는 공식 링크와 계정 체계 단서가 비교적 명확해서 다음 자동화 대상로 적합하다.
