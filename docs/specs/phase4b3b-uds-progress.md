# Phase 4B-3B Unix Socket Mock 검증

상태: **합성 UDS Probe 범위 PASS**. 전체 실모델 보안 격리 완료 판정은 하지 않는다.

이 문서는 초기 동일 UID Probe의 기록이다. UID 분리·HMAC·실제 Agent IPC 통합의 최신 결과는 [통합 기록](phase4b3b-progress.md)을 참조한다. 초기 구현과 실행 명령은 회귀용으로 유지한다.

## 구성과 실행

수동 명령: `uv run --locked --no-sync python -m rca_agent.uds_probe`.

Broker·Mock Worker·Agent 모두 `network=none`, 비루트 UID/GID 65534, read-only root, cap-drop ALL, no-new-privileges로 실행한다. 컨테이너 inspect에서 이를 확인하며 추가 마운트나 네트워크는 거부한다. 실제 OpenAIProvider·PreparedRequest·macOS IPC 및 기존 Probe는 변경하지 않는다.

Broker와 Worker에만 임시 local-driver tmpfs Docker Volume을 `/channel`에 마운트한다. tmpfs는 크기 1MiB, UID/GID 65534, 모드 0700이며 root 초기화 컨테이너는 사용하지 않는다. Worker는 `/channel/worker.sock`을 0600으로 생성한다. Agent에는 Volume이 없다. 키·소스·호스트 평가자 디렉터리와 Docker 소켓은 마운트하지 않는다. 파일 검사는 호스트에 생성한 합성 blind·정답·mapping·key 파일만 사용한다.

Worker는 제한된 HTTP/Unix Socket Mock 서비스다. `POST /mock HTTP/1.1`과 `{version: 1, probe: synthetic}` 구조만 받는다. 헤더 4096바이트, 본문 2048바이트와 연결별 2초 제한을 두고 JSON 중복 키·NaN·추가 필드·잘못된 version·절대 URL·transfer encoding·중복 HTTP 헤더를 거부한다. 실제 HTTP 외부 전송이나 Provider 연결은 구현하지 않는다.

## 실제 Docker 관측

Docker Desktop Engine 29.1.3 / linux arm64, 로컬 Python 3.12.13 이미지로 실행했다. 이미지 pull 및 실제 API·키 사용은 0건이다.

- Broker → UDS → Worker HTTP 200 및 합성 응답 본문 일치: PASS.
- 소켓 0600/UID65534, 디렉터리 0700: PASS.
- Agent의 소켓 부재 및 connect 실패: PASS(Volume 미마운트).
- Agent/Broker/Worker의 합성 정답·mapping·key·blind 파일: 모두 NOT_MOUNTED. 파일시스템 전체 allowlist 검증으로 해석하지 않는다.
- Broker 및 Agent → 외부 IPv4/IPv6, 이전 실험 Gateway 주소, 보조 internal 네트워크 Gateway, 별도 컨테이너 IP의 8080/8081: 모두 ENETUNREACH.
- 별도 보조 컨테이너의 합성 8080/8081 수신 서비스는 컨테이너 내부에서 HTTP 200을 확인했다. 이 보조 수신 컨테이너만 internal 네트워크에 있으며 Worker는 network none이다.
- Broker에서 보낸 잘못된 JSON/중복 JSON 키/추가 필드/불리언 version/절대 대상 URL/transfer encoding은 400, 과대 본문은 413으로 거부됐다.

## 남은 위험과 범위

Broker와 Worker는 같은 UID와 쓰기 가능한 Volume을 공유하므로 악의적인 Broker의 소켓 교체나 위장 서버를 막는 별도 인증 경계는 구현하지 않았다. 현재 Worker는 외부 네트워크가 없고 합성 응답만 반환한다. 다음 외부 Worker 단계 전에 역할별 UID/소켓 접근 제어, peer 인증, 권한과 프로토콜, 외부 목적지 및 비용 정책을 별도로 설계·검증해야 한다. 단일 연결의 timeout은 동시성·부하 방어를 증명하지 않는다. 실제 API 스키마 수용과 토큰 계산 정확도는 여전히 미검증이다.

Probe 종료 시 컨테이너를 강제 제거하고 Volume과 보조 네트워크를 삭제한다. Worker가 강제 종료되더라도 소켓은 임시 Volume 삭제로 제거된다. cleanup 실패는 BLOCKED다. PR 생성·병합은 수행하지 않는다.

## 최종 검증과 리뷰

- `uv run --offline --locked python -m rca_agent.uds_probe`: 실제 Docker에서 합성 범위 PASS, 종료 코드 0.
- `uv run --offline --locked python scripts/docker_agent_probe.py`: 기존 Agent 검사 PASS.
- `make check`: 기존 196개 및 신규 22개를 포함한 218개 PASS.
- `make harness-verify`: 218개 PASS, 로컬 검사 PASS, 전체 하네스 결정 DEFERRED(외부 검사 대기).
- `docker ps -a`, `docker volume ls`, `docker network ls`의 `rca-uds-` 필터에서 남은 자원 없음. Socket이 들어 있던 Volume도 제거됐다.
- 신규 오프라인 회귀는 UDS HTTP 정상 응답, 헤더/본문 한도·잘못된 프레이밍·중복/불리언/NaN JSON 거부, 네트워크·Volume 노출 설정 거부, Worker 생성 timeout 시 소유 컨테이너·Volume·네트워크 정리를 검사한다.
- 첫 전체 검사에서 조기 오류 응답 뒤 서버가 먼저 연결을 닫는 경우 테스트 클라이언트 `shutdown`이 ENOTCONN으로 실패하는 경쟁 조건을 발견했다. 해당 종료만 허용하도록 테스트를 수정한 뒤 위의 두 전체 명령을 다시 실행해 통과했다.
- 코드·보안 리뷰에서 크기 확인 전 본문 할당을 피하고, Transfer-Encoding 및 중복 헤더를 거부하며, Worker의 외부 전송 코드가 없음을 확인했다. 인증·소켓 교체 방어와 실제 외부 Worker 권한 경계는 위의 남은 위험으로 유지한다.
