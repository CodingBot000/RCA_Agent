# Phase 4B-3C 오프라인 Egress·파일럿 준비

**오프라인 구현·검증 PASS**, 실제 실행은 **LIVE_BLOCKED**다. 실제 API/키 사용·생성, 유료 과금 및 비공개 20건 실모델 분석은 수행하지 않았다. Mock 결과는 RCA 정확도가 아니다.

## 재현

```sh
# 명시적 수동 빌드: 사용자 승인된 Linux 잠금 패키지 및 iptables 설치
uv run --locked --no-sync python scripts/build_offline_image.py --egress
uv run --locked --no-sync python -m rca_agent.mock_egress
uv run --locked --no-sync python -m rca_agent.mock_egress --suite
uv run --locked --no-sync python -m rca_agent.pilot --help
make check
make harness-verify
```

기존 3B 경로와 Probe를 유지하고 `docker_integration.run_session`의 선택적 extension으로 Proxy를 연결했다. 빌드 context에는 잠금 requirements와 Python 소스만 포함한다. 런타임 소스 hash와 이미지 label이 다르면 재빌드를 요구한다.

## OS와 애플리케이션 경계

Agent/Broker/Worker는 모두 network none을 유지한다. Worker만 별도 Proxy Volume을 read-only로 본다. Proxy UID10005, Worker UID10001, GID10002와 별도 세션 HMAC/SO_PEERCRED로 확인한다. Volume은 owner10005/group10002/0750, socket은 0660이며 Agent/Broker에는 Volume과 인증값이 없다. 인증값은 익명 stdin으로만 주입한다.

Proxy와 TLS Mock API는 isolated internal 네트워크를 사용한다. 단기 root 초기화 컨테이너에 NET_ADMIN만 추가해 **Proxy netns만** 공유하고 iptables/ip6tables를 설정한다. Host network나 Docker 소켓을 제공하지 않는다. Proxy 런타임은 비루트·read-only root·cap-drop ALL·no-new-privileges다. 방화벽 설정 전에 Proxy는 stdin 설정을 기다리며 데이터·인증값은 아직 주입하지 않는다.

INPUT/OUTPUT 기본 DROP, 응답 트래픽과 승인 Mock IPv4:8443 TCP만 허용한다. 나머지 새 연결은 admin-prohibited REJECT다. IPv6 새 연결과 UDP DNS도 허용하지 않는다. 실제 수신 중인 다른 Mock IP 및 같은 서버 8444로 거부를 검사한다. HTTPS_PROXY/ALL_PROXY를 비허용 Mock 서버로 설정해도 정상 경로가 성공한다.

이것은 **로컬 Mock IP/포트 OS allowlist**다. 실제 OpenAI DNS/IP 집합의 운영 허용 정책은 미검증이다. hostname·경로·TLS·redirect는 애플리케이션 검증과 구분한다. Proxy는 고정 숫자 endpoint로만 연결하며 DNS·환경 프록시·CONNECT를 사용하지 않는다. HTTPS api.openai.com:443의 Responses/count 경로만 논리적으로 승인하고, 임의 URL·IP URL·다른 포트·userinfo·query·경로·도구·저장·stream·스키마 변경은 거부한다.

TLS는 CA chain/hostname 검증 및 TLS 1.2 이상을 요구한다. 임시 Mock CA/서버 키는 Git·이미지에 넣지 않는다. leaf private key는 TLS Mock 서버에만, CA 공개 인증서는 Proxy에만 read-only로 제공한다. Mock CA는 운영 신뢰 설정에 사용하면 안 된다. Proxy에 blind·정답·mapping·표본 키를 마운트하지 않는다. 승인된 관측 요청은 메모리에서 처리하며 원본 요청을 파일로 남기지 않는다.

## Provider·예산·오류

기존 OpenAIProvider의 MockTransport 전용 제약과 PreparedRequest를 유지한다. Worker의 SDK count/Responses 요청을 인증 UDS로 Proxy에 전달하고 로컬 TLS HTTP로 연결한다. Worker Authorization 헤더는 전달하지 않는다. 현재 Proxy에 API 자격 증명 입력은 없으며 향후 실제 자격 증명은 Proxy만 보유·주입해야 한다.

Broker의 사례별·전체 원장 외에 Proxy에도 선언된 Budget의 독립 OfflineBroker를 둔다. count 결과 및 세션 model/output 한도를 확인한 뒤 모델 비용·토큰을 예약한다. Proxy 독립 예산 초과의 네트워크 전 차단도 회귀 검사했다. Mock 요금은 시험 설정이며 운영 기본값이 아니다. [공식 가격 확인](phase4b3c-pricing.md)은 별도 자료다.

동일 인증 ID 재전송은 캐시 응답을 사용한다. Proxy는 모델 side effect 전에 binding을 소진하고, 성공·불명확 결과를 새 ID/count로 재승인하지 않는다. 불명확 실패는 전체 세션을 차단한다. SDK 및 TLS client는 자동 모델 재시도하지 않는다. 영속 재시작 중복 방지는 미구현이다.

제한된 크기의 JSON만 수용하고 중복 키·NaN을 거부한다. inactivity timeout 외에 전체 watchdog으로 TCP를 shutdown한다. HTTP/1.0 응답 파일이 연결 소유권을 가져가도 별도 descriptor로 중단한다. 로컬 shutdown은 원격 모델 취소나 청구 취소의 보장이 아니며, 취소 미확인을 재시도 허가로 취급하지 않는다.

channel_error와 provider error를 구분해 알려진 거부 사용량도 정산한다. 사용량 누락·불명확 HTTP 실패·timeout은 UNAVAILABLE다. settled cost 0을 실제 청구 0으로 해석하면 안 된다. 공개 출력에는 고정 오류 코드와 검증된 계수만 포함하고 TLS 예외·요청 본문·인증값은 출력하지 않는다.

## Docker 실행 근거

Docker Desktop Engine 29.1.3 linux/arm64에서 실제 실행했다.

- 정상 Agent→Broker→Worker→Proxy→TLS Mock API: PASS. 입력 count 2/모델 2회, IPC 3회(중복 1), 도구/반복 1, Mock 원장 input20/output100/cost0.220. 비용과 토큰은 합성 값이다.
- Broker→Worker 및 Worker→Proxy 양쪽 UDS 명시적 재전송에서도 HTTP 4/모델 2회 유지.
- 다른 수신 IP/8444 포트: EHOSTUNREACH. Docker DNS 127.0.0.11 UDP: EPERM. 외부 IPv4/IPv6·외부 DNS: ENETUNREACH. IPv6 활성 WAN 경로까지 검증한 것은 아니다.
- TLS chain·hostname·version 오류는 TLS_CERTIFICATE/TLS_HOSTNAME/TLS_PROTOCOL로 거부했고 모델 호출 0회.
- redirect는 REDIRECT_DENIED로 거부했다. 429·5xx는 HTTP_429/HTTP_5XX를 기록하고 미확인 사용량을 차단했다.
- 거부 REFUSAL은 알려진 input10/output50/cost0.110을 정산했다. 누락 사용량은 USAGE_UNAVAILABLE이며 실제 비용 미확인으로 남긴다.
- 계산 실패·비용 초과·일반 지연·느린 본문·Worker 중단을 포함해 `--suite`의 16개 시나리오가 기대한 정상/실패 결과를 냈다.
- Agent/Broker/Worker TCP 거부 및 사적 파일 미마운트, Proxy의 사적 파일 미마운트 검사를 유지했다. 임의 파일 전체 allowlist 검증으로 표현하지 않는다.

## 개발 1건 CLI와 보고 구조

기본 dry-run이고 `--live`는 항상 LIVE_BLOCKED로 실패한다. 현재 CLI에 유료 backend가 활성화되어 있지 않으며 별도 승인만으로 자동 활성화되지 않는다.

개발 root/case/manifest, model, price profile 및 Budget의 모든 필드를 명시해야 한다. 현재 계획 프로필은 `standard-global-conservative`다. cached-read 할인 없이 공개된 Standard/global Long/12h write 최대 입력 요율과 Long 출력 요율로 선언된 누적 토큰의 모델 비용과 count/모델 최대 요청 수를 계산한다. 기타 요금·실제 계정 tier는 미검증이며 청구 보장은 아니다.

개발 manifest는 split/case_id/directory만 포함하고 split은 development다. directory는 root 안의 단일 사례와 일치해야 한다. 내부 task 계약과 metrics CSV/Parquet이 필요하다. 평가 20건 디렉터리 또는 같은 case ID는 거부하며, 분리 확인용 평가 registry가 없으면 fail closed다. 평가 정답·mapping은 읽지 않는다. development-data/와 pilot-private-results/는 Git에서 제외한다.

`pilot-report-1`에는 model/설정·가격 확인일, commit과 실제 source hash/dirty 상태, prompt version, 성공·실패와 오류 유형, tool/iteration, token/실제 비용, evidence validity, evaluator-only Top-1/Top-3 항목이 있다. 미실행 값은 null이다. 실제 CLI에서 dry-run 성공과 live 차단을 확인했다. 공식 채점 연결 지점은 `rca_agent.rcaeval_compat.score_results`이며 이번에 실행하지 않았다. case ID·경로·telemetry fingerprint는 공개 보고서에 없다.

`pilot.compare`는 같은 development experiment ID의 전후 source/prompt/설정 및 측정값 delta를 비교한다. 동일 사례 매칭은 운영자가 비공개 기록으로 관리해야 한다. 측정 전 delta는 null이며 성능 회귀 결과가 아니다.

## 실제 1건 실행 전 남은 조건

1. 실제 OpenAI WAN DNS/IP/IPv6/직접 연결 OS allowlist 및 Mock CA와 분리된 운영 TLS 검증. 현재 local kernel PASS는 이를 대신하지 않는다.
2. Proxy 전용 실제 키 주입, 승인된 paid backend, 영속 journal·재시작·불명확 청구 조정 절차를 구현·리뷰해야 한다. 실제 count 정확도와 schema 수용은 미검증이다.
3. 사용자 결정: 개발 사례/manifest, 모델과 처리 프로필, 사례/전체 비용·token·call/iteration·output·timeout·동시성 1, 보존/삭제와 공개 보고 범위. fixture 숫자는 승인이 아니다.
4. 실행일 공식 가격·count API 기타 요금·계정 모델 접근과 tier/cache/지역 파라미터 바인딩을 확인해야 한다.
5. 위 조건 이후 별도 명시적 승인으로 개발용 고유 1건만 실행한다. 평가 20건과 실제 정확도 측정은 이번 범위가 아니다.

## 최종 검증과 리뷰

- `make check`: 기존 263개를 포함한 291개 PASS. `make harness-verify`: 291개 및 로컬 검사 PASS, 전체 결정 DEFERRED(외부 검사 대기).
- 최종 `mock_egress --suite`: 실제 Docker에서 16개 정상/보안 음성/장애 시나리오 모두 기대 결과 PASS. 이전 `docker_integration --suite`도 13개 PASS, 기존 Agent Docker Probe PASS, macOS IPC 회귀 유지.
- 실제 별도 CLI 프로세스의 개발용 Synthetic dry-run: DRY_RUN_VALIDATED. 같은 인수에 --live를 추가하면 API 없이 LIVE_BLOCKED/종료 코드 2. 테스트 선언 모델 비용 계획은 0.00230000 USD였고 실제 비용은 null이다.
- 이미지: 기본 runtime `sha256:f701b73f38dc432e2795c89aef365e2f326509a3f31f36f2f3dbe0924875f584`, Egress `sha256:952809fcb4a3b983e5f57771612856896eb5a8f7b4c2eb2320bf31c79dc69676`. Engine 29.1.3 / iptables 1.8.11(nf_tables)에서 실행했다.
- 코드·보안 리뷰에서 Docker start와 exec의 경합, 시작 전 빈 container IP의 사용, 정상 provider error와 채널 오류의 구분, 공개 실패 요약의 사용량 타입/미확인 상태, 불명확 요청의 새 ID 재승인, HTTP 응답이 소켓을 분리하는 watchdog 경계, dirty source 버전 보고를 보완했다. 대응하는 단위 회귀와 실제 Docker 시나리오를 실행했다.
- SDK 내부 스키마 헬퍼를 계속 사용하므로 lock/SDK 갱신 시 projection/schema와 TLS Mock 회귀를 다시 검증해야 한다. 실제 API 동작 차이·취소·청구·키 주입은 LIVE_BLOCKED 조건으로 유지한다.
- 음성 시나리오의 HTTP 계수는 전송 시도다. TLS 실패의 count 시도 1회를 서버 수신/모델 실행 성공으로 해석하지 않는다. Remote cancel은 구현·검증하지 않았고 local shutdown 이후 미확인 상태의 재호출을 거부한다.
- 임시 컨테이너·네트워크·Volume과 테스트 인증서/소켓을 제거했다. 최종 Docker 조회에서 해당 rca 리소스가 남지 않았다. 기존 macOS 실행기·격리 프로필 및 실제 키/비공개 데이터는 변경하지 않았다.
- Git 대상 19개 파일에서 금지된 데이터/키/로그 경로 0건, API 키·토큰·PEM 개인키 패턴 0건을 확인했다. `.env`, phase1-data, development-data 및 pilot-private-results 제외 규칙을 확인했다. 패턴 검사는 보조 검사이며 OS·프로토콜 격리의 대체 수단이 아니다.
