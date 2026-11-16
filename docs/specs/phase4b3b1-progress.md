# Phase 4B-3B-1 진행 기록

상태: **BLOCKED — Broker·Proxy 격리 및 E2E 미구현·미검증**.

위 상태는 초기 실험 당시 기록이다. 이후 network none 및 인증 UDS 기반 오프라인 통합을 추가했으며 최신 구현·LIVE_BLOCKED 조건은 [통합 기록](phase4b3b-progress.md)을 참조한다.

후속 1A에서 Broker internal 네트워크와 Mock HTTP Probe를 추가했다. 통신 및 제한 설정은 확인했으나 gateway와 비허용 포트 경로가 발견되어 전체 격리는 계속 BLOCKED다. 최신 근거는 `phase4b3b1a-progress.md`를 참조한다.

Docker Desktop 로컬 arm64 환경에서 이미 보유한 `python:3.12.13-slim` 이미지를 사용해 합성 Agent 기초 검사를 수행했다. `scripts/docker_agent_probe.py`는 network none, 읽기 전용 root와 합성 blind bind mount, 비루트 UID, capability 전부 제거 및 no-new-privileges를 적용한다. blind 읽기와 blind/root 쓰기 거부, Docker 소켓 부재, `192.0.2.1:443` 연결 실패를 확인했다. 평가 데이터·키는 마운트하지 않았으며 컨테이너의 지정 경로가 없는 것만 확인했다. 이는 모든 파일 접근이나 다른 네트워크 경로의 차단을 증명하지 않는다.

Broker 전용 internal 네트워크, egress proxy, 허용 목적지 제한, 우회·리다이렉트 음성 시험, 신뢰된 IPC 중계 및 Fake Provider Mock E2E, timeout·Budget에 따른 전체 컨테이너 종료는 아직 구현하거나 검증하지 않았다. OS 네트워크 격리와 애플리케이션 계층의 목적지 검증을 혼동하지 않는다. 실제 API 연결, 키 및 외부 이미지 다운로드는 사용하지 않았다. GitHub Actions와 PR/병합은 미수행이다.
