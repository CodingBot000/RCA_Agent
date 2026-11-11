# Phase 4B-3 진행 기록

상태: **BLOCKED — 실모델 실행 경계 미구현 및 미검증**.

기존 macOS 실행기는 합성 fixture 전용이며 Agent와 Broker 모두의 네트워크를 차단한다. Broker의 특정 외부 API만 허용하는 네트워크 강제, Agent의 실제 인증 정보 접근 차단, Broker의 원본·정답·평가자 매핑·비공개 표본 키 접근 차단은 아직 구현하거나 검증하지 않았다. 현재 deny-list 프로필을 그대로 사용해 실모델 연결을 허용하지 않는다.

기존 IPC는 Fake Provider의 ID와 응답 수에 고정되어 있다. 실제 Provider를 위한 일반화, OpenAI SDK와 Structured Outputs, 토큰 상한 및 요금 검증, 불완전 응답·거부·오류·시간 초과 처리와 오프라인 HTTP 검증은 미완료다. 사례별·전체 예산의 운영 값과 데이터 보존 정책도 미정이며 테스트 값은 운영 승인 값이 아니다.

기준선: macOS에서 `make check` 통과(144개 테스트), `make harness-verify`의 로컬 검사는 통과했으나 외부 검사가 남아 전체 판정은 DEFERRED다. 실제 API 호출은 하지 않았다. Phase 4B-3 자체의 검증, 보안 리뷰, PR과 CI 및 병합은 완료되지 않았다.
