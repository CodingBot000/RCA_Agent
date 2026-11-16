# Phase 4B-3B Gateway 추가 검증

상태: Gateway 추가 검사 구현 및 실행 완료. **전체 격리 판정은 BLOCKED**다.

## 실행과 근거

`uv run --locked --no-sync python -m rca_agent.gateway_probe`로 수동 실행한다. 기존 Agent/Broker Probe와 OpenAIProvider는 변경하지 않는다. Docker Desktop Engine 29.1.3 / API 1.52 / linux arm64에서 로컬 Python 이미지 ID `sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36`를 사용했다. 이미지 pull과 실제 API 호출은 없다.

[Docker 공식 gateway mode 문서](https://docs.docker.com/engine/network/port-publishing/#gateway-modes)는 isolated를 internal 네트워크에 한정하고 bridge에 주소를 부여하지 않는다고 설명한다. 실제 생성 후 `Internal=true`, bridge driver, `com.docker.network.bridge.gateway_mode_ipv4=isolated`를 inspect로 확인했다. isolated IPAM Gateway 값은 비어 있었다. 옵션이 적용되지 않으면 검사를 실패시킨다.

Engine host 네트워크에 합성 HTTP 수신 컨테이너를 두었다. Broker와는 별도 역할이며, 임의의 실제 호스트 서비스를 사용하지 않는다. host namespace의 loopback과 기본 bridge 주소에서 HTTP 200 및 합성 본문을 확인했다. 일반 internal 대조군도 Gateway 주소에서 같은 서비스에 연결했다. 이 대조군과 서비스는 isolated 검사 완료까지 유지한다. Gateway가 없는 isolated 네트워크에서 주소를 임의로 추정하지 않고, 대조군에서 검증한 동일 Gateway 수신 주소를 사용한다.

| 검사 | 일반 internal | isolated | 판정 범위 |
| --- | --- | --- | --- |
| Broker → Mock HTTP | 성공 | 성공 | PASS |
| 실제 수신 서비스의 기존 Bridge Gateway | 연결 성공 | ENETUNREACH | 해당 경로 차단 PASS |
| 같은 Gateway의 IPv4-mapped IPv6 | 연결 성공 | ENETUNREACH | 해당 우회 경로 차단 PASS |
| 수신 서비스의 Engine 기본 bridge 숫자 주소 | ENETUNREACH | ENETUNREACH | 해당 Host 경로 차단 PASS |
| host-gateway 특수 매핑 | ENETUNREACH | ENETUNREACH | 해당 주소 경로에서 연결 실패 |
| host.docker.internal | DNS 실패 | DNS 실패 | 미검증/BLOCKED |
| 외부 IPv4 1.1.1.1:443 | ENETUNREACH | ENETUNREACH | 해당 경로 차단 PASS |
| 외부 IPv6 2606:4700:4700::1111:443 | ENETUNREACH | ENETUNREACH | IPv6 비활성 환경의 해당 접속 차단 |
| 같은 Mock의 비허용 포트 8081 | 연결 성공 | 연결 성공 | BLOCKED 유지 |

실제 수신 서비스로 확인한 Gateway 결과만 접근 차단 근거로 삼았다. Connection Refused·timeout·DNS 실패는 차단 성공으로 취급하지 않는다. IPv6 활성 네트워크, 모든 Host 주소, 임의 DNS/UDP 경로나 다른 Docker 설정까지 검증한 것은 아니다. `files_unavailable`의 빈 목록은 이 Probe가 파일 검사를 수행했다는 뜻이 아니며 파일 접근 결과는 기존 1A 기록을 참조한다.

대조군과 isolated 네트워크는 서로 다른 서브넷이다. 기존 Gateway 주소에 대한 경로 차단은 실제로 확인했지만, 이 비교만으로 차단의 원인을 isolated 옵션 하나로 확정하지 않는다. isolated 네트워크 자체의 Gateway 주소는 존재하지 않으므로 별도 수신 주소가 있다고 꾸며 검사하지 않았다. 옵션 적용과 Gateway 미설정은 inspect로, 실제 기존 서비스에 대한 연결 차단은 별도 접속 시험으로 구분한다.

## 제한과 정리

Probe는 비루트·읽기 전용·capability 제거·no-new-privileges를 유지한다. 테스트용 Host 수신 컨테이너를 제외한 Broker/Mock은 단일 internal 네트워크에만 연결한다. 테스트 컨테이너와 네트워크는 finally에서 강제 제거하고 제거 실패는 BLOCKED로 보고한다. 전체 종료 코드는 비허용 포트와 미검증 경로 때문에 2다. Egress Proxy, 포트별 제한, 실제 API·키 사용, PR·병합은 수행하지 않았다.

## 최종 검증

- 기존 Agent Docker 검사: PASS.
- `make check`: 기존 190개 및 옵션 적용 거부·실패 시 정리 등 신규 6개를 포함해 196개 PASS.
- `make harness-verify`: 196개 PASS, 로컬 검사 PASS, 전체 판정 DEFERRED(외부 검증 대기).
- 실제 Gateway Probe: 수신 서비스 대조군·Mock 통신 성공, 옵션 적용 확인, 비허용 포트 및 DNS 미검증 때문에 전체 BLOCKED(종료 코드 2).
- 실행 종료 후 `rca-gateway-` 이름의 컨테이너와 네트워크가 없는 것을 Docker 조회로 확인했다.
- 코드·보안 리뷰: root/추가 mount/공개 포트 없이 Probe를 생성하고 기존 container inspect 검증을 재사용한다. 오류 본문과 호스트 경로를 공개 결과에 출력하지 않는다. 기존 macOS/IPC/OpenAIProvider 변경은 없으며 DNS 실패나 connection refused를 차단 PASS로 승격하지 않는다.
