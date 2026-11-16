# Phase 4B-3B-1A 검증 기록

상태: 구현 및 Mock 통신 검증 완료, **격리 판정 BLOCKED**. Docker internal 네트워크만으로 모든 목적지 제한이 보장되지는 않았다.

## 구현과 실행

`uv run --locked --no-sync python -m rca_agent.docker_probe`로 수동 실행한다. Docker를 사용할 수 없거나 이미지가 없으면 실패한다. 이미 보유한 Python 이미지를 ID로 고정해 사용하며 pull/build를 수행하지 않는다. 별도 Broker Probe와 합성 Mock HTTP 서버를 단일 internal 네트워크에 연결한다. stdin/stdout IPC, 기존 Fake Provider, OpenAIProvider 및 macOS 격리 코드는 변경하지 않았다. 이번 HTTP 통신은 SDK나 실제 Provider 호출이 아니다.

컨테이너는 UID/GID 65534, 읽기 전용 root, capability 전부 제거, no-new-privileges, PID/메모리/CPU 제한 및 Docker 로그 비저장을 사용한다. 포트 공개와 볼륨 마운트는 없다. inspect 결과가 해당 설정과 다르면 검사를 중단한다. 명령별 30초 제한을 적용하고 실패·시간 초과에도 생성한 컨테이너와 네트워크를 제거한다. 제거 실패는 BLOCKED로 보고한다. 테스트의 제한값은 운영 Budget 정책이 아니다.

## 실제 Docker 결과

Docker Desktop Engine 29.1.3, linux/arm64, 로컬 `python:3.12.13-slim` 이미지 ID `sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36`에서 확인했다.

| 항목 | 실제 관측 | 판정 |
| --- | --- | --- |
| Broker → Mock:8080 | HTTP 200, 합성 본문 일치 | PASS |
| 비루트·capability·no-new-privileges | 내부 UID/CapEff/NoNewPrivs 및 inspect 확인 | PASS |
| root 쓰기 | EROFS | PASS |
| blind·정답·mapping·key | 합성 파일은 호스트에만 존재, Broker에서 FileNotFound | 미마운트 PASS |
| Docker 소켓 | 컨테이너에 없음 | PASS |
| 외부 IP 1.1.1.1:443 | ENETUNREACH | 해당 접속 차단 PASS |
| internal bridge gateway:80 | ECONNREFUSED | 경로 존재, BLOCKED |
| 같은 Mock 서버의 비허용 포트 8081 | 연결 성공 | BLOCKED |
| host.docker.internal의 합성 호스트 listener | DNS 실패 | 미검증/BLOCKED |

ECONNREFUSED는 OS가 연결을 차단했다는 증거로 취급하지 않는다. DNS 실패나 timeout도 차단 PASS가 아니다. Linux errno는 컨테이너에서 이름으로 변환해 macOS errno 숫자와 혼동하지 않는다. 임의 경로의 파일 접근 거부, 모든 인터넷 주소·IPv6·DNS 우회를 검증한 것은 아니다. 호스트 합성 listener는 일시적으로만 실행하고 종료한다. 실제 키·평가자 데이터는 읽지 않았다. 실제 OpenAI API 호출과 키 사용은 0건이다.

## 남은 범위

목적지별 강제는 현재 internal 네트워크에서 구현되지 않았다. 향후 Egress Proxy와 네트워크 정책의 별도 구현·음성 검증이 필요하다. Agent/Broker Docker IPC E2E, 실제 API와 유료 평가, PR·병합은 이번 단계에서 제외했다. 전체 보안 격리 완료로 판단하지 않는다.

## 회귀 검증과 리뷰

- `python scripts/docker_agent_probe.py`: 기존 합성 Agent 검사 PASS.
- `make check`: 기존 172개와 신규 18개를 포함해 190개 PASS. ruff 및 스킬 동기화 검사 PASS.
- `make harness-verify`: 190개 PASS, 로컬 검사 PASS. 전체 하네스 결정은 외부 검증 대기로 DEFERRED.
- Broker Probe: Mock 통신과 설정·미마운트 검사 PASS. 목적지 검사에서 위의 경로를 발견해 종료 코드 2, BLOCKED를 반환한다.
- Docker 조회에서 해당 Probe 이름의 잔여 컨테이너와 네트워크가 없음을 확인했다.
- 회귀 테스트는 위험한 컨테이너 설정(추가 capability, root, 쓰기 허용, 마운트, 공개 포트, 추가 네트워크 등)을 거부하고, 시간 초과 시 소유한 자원을 제거하는지 검사한다. Docker 없이 실행되며 실제 Docker Probe를 대신하지 않는다.
- 코드·보안 리뷰에서 macOS/Linux errno 차이로 오판할 수 있던 부분을 오류 이름 전달로 수정했다. DNS 실패·timeout을 PASS로 판단하지 않으며, connection refused는 도달 가능한 경로로 분류한다. 자원은 무작위 이름으로 생성하고 실패 시에도 cleanup을 수행하며 cleanup 오류는 숨기지 않는다. 실제 키나 프로젝트 디렉터리를 컨테이너에 전달하지 않는다. 기존 OpenAIProvider 및 macOS IPC 파일 변경은 없다.
