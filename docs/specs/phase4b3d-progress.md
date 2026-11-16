# Phase 4B-3D Live Pilot Readiness

기준선: PR #15. 오프라인 구현·검증은 PASS이며, 유료 실행은 사용자 승인 전 **LIVE_BLOCKED**다. 실제 API HTTP 요청·API 키 사용/생성·유료 모델 실행은 0건이다. 실제 OpenAI에 대해 수행한 것은 DNS 및 시스템 CA TLS 핸드셰이크뿐이다. 실제 계정 인증·schema/count·billing 동작은 별도 승인된 최초 1건 전까지 미검증이다.

## 실행 경계

Agent/Broker/Worker의 network none, 별도 UID·HMAC·SO_PEERCRED·readonly Root/Volume/cap-drop/no-new-privileges 및 기존 macOS 하네스를 유지했다. ProductionProvider와 ProductionProxy를 Mock 구현과 분리하고, 실제 경로가 Mock으로 fallback하면 거부한다. Worker의 SDK formatter는 여전히 직접 네트워크 transport나 실제 API key를 갖지 않는다. Proxy만 실제 HTTP Authorization을 구성할 수 있다.

실제 운영 경로는 명시적 `--live`, private operator approval, 개발 provenance, 모든 Budget/runtime 값과 credential FD가 있어야 시작한다. 승인 파일은 host owner-only 0700 디렉터리/0600 파일이며 code/settings/provenance/runtime/기한과 결합한다. 운영 API credential은 명시적 FD(3 이상)를 통해서만 받고 환경이나 `.env`를 자동으로 읽지 않는다. 승인·WAN/OS/TLS 준비가 실패하면 credential FD도 읽지 않는다. Host trusted launcher는 stdin으로 Proxy에만 전달하고 Agent/Broker/Worker·CLI 인수·이미지·journal·로그에 key를 넣지 않는다. 이 작업에서 유효한 운영 승인 파일을 만들지 않았다.

## DNS/IP와 OS 제한

`dns_guard`는 고정 api.openai.com의 A/AAAA 집합만 system resolver로 가져오고 loopback/private/link-local/multicast/mapped IPv6 등 부적절한 운영 주소를 거부한다. 로컬 Mock은 별도 명시적 mock lease다. 실제 DNS TTL은 system getaddrinfo로 알 수 없으므로 operator가 승인한 최대 lease 기간으로 취급한다. DNSSEC 검증이나 authoritative TTL 확인을 주장하지 않는다.

별도 guardian이 Proxy netns의 IPv4/IPv6 filter table을 갱신한다. 두 family를 먼저 default-deny로 닫고 각각 atomic restore한다. 적용 실패 시 닫힌 table로 돌아간다. 허용된 TCP IP/port의 INPUT/OUTPUT 규칙 모두 UTC datestop을 갖고, 무제한 ESTABLISHED 허용은 없다. guardian이 종료돼도 커널 규칙이 만료된다. 새 요청은 readonly root-owned lease 파일의 소유권·권한·유효시간을 검사한다. DNS 갱신 실패는 가능한 경우 즉시 만료시키고 실행을 중단하며, control plane 장애에서도 기존 lease를 무기한 연장하지 않는다.

최소 IPv6 ND 및 로컬 ICMP 오류 전달은 데이터 TCP 허용과 구분한다. 다른 주소·포트·Gateway·DNS 경로와 환경 프록시는 허용하지 않는다. URI/hostname/path, redirect 및 TLS 검사는 애플리케이션 정책이다. Proxy는 lease에 든 숫자 주소로만 연결하고 TLS hostname/SNI는 api.openai.com 고정이다. 운영 context는 시스템 CA만 사용하며 Mock CA 옵션을 받지 않는다. Worker 헤더는 전달하지 않고 API 오류 본문/키 반사·불필요한 count 진단 필드는 Proxy가 제거한다.

실제 DNS 해석과 TLS bootstrap을 매 운영 시작 때 credential 주입 전에 확인한다. `ipv4-only` 정책은 IPv6 TCP를 허용하지 않으며 dual-stack 선택 시 IPv6 namespace 및 모든 선택 주소 TLS가 준비되지 않으면 실패한다. 현재 실제 WAN TLS 증거는 IPv4 범위다.

## Durable journal

Proxy만 쓰는 persistent on-disk Docker Volume의 owner10005/0700 디렉터리와 0600 SQLite/owner lock을 사용한다. SQLite DELETE/EXTRA commit과 별도 flock을 사용해 singleton 및 atomic 상태 전이를 강제한다. 동일 DB에 직접 flock하면 macOS SQLite lock과 충돌할 수 있어 별도 owner 파일로 분리했다.

실제 모델 side effect 전에 PreparedRequest fingerprint·요청 ID·model·counted input·output cap 및 SENT를 durable commit한다. prompt·원본 telemetry·키·GT를 journal에 저장하지 않는다. 완료 후 response ID와 사용량·token pricing estimate를 기록한다. 비용은 실제 invoice가 아니며 billing_status는 UNRECONCILED다. 프로세스가 중단된 SENT는 MANUAL_REVIEW로 복구하고 같은 digest/ID를 새 세션에서도 실행하지 않는다. 승인된 private model/tool artifact는 별도 0700/0600 경로에만 저장한다.

응답/청구 미확인, Token count 불일치·초과, 로컬 종료 및 취소 미확인은 자동 재시도하지 않는다. remote cancel·정확한 청구 상한·Docker VM factory reset/디스크 손실의 복구를 보장하지 않는다. 실제 persistent journal은 cleanup에서 삭제하지 않는다. 미확인 row를 우회하려고 새 journal을 사용하면 안 되며 수동 계정/청구 확인이 필요하다.

## 개발 사례

`development.prepare`는 evaluator-only mapping + official ID binding + pinned dataset revision으로 원본 source_case 집합을 대조한다. 익명 ID가 다르더라도 같은 원본이면 거부한다. 기존 로컬 legacy source 중 최종 private v2 평가 20건에 없는 원본 1건을 준비했다. 추가 다운로드는 없었다.

준비 위치는 ignored `development-data/pilot-v1`이다. cases에는 task/metrics/logs/traces만 있고, private provenance는 host에서만 읽는다. Source identity·file hashes와 평가 mapping digest를 검사하므로 telemetry 변경이나 원본 중복을 거부한다. 공개 결과에는 case/source ID나 fingerprint가 없다. GT 필드는 task/provenance 전달 데이터에 복사하지 않으며 selection key도 모델 역할에 제공하지 않는다.

시간/trace ID 변환과 기존 로그 마스킹을 적용했다. 마스킹은 PII 제거 보장이 아니며 외부 전송 전 사용자 검토가 필요하다. 세 신호가 있는 service 선택은 관측 존재 여부로 했고 GT를 선택 기준으로 사용하지 않았다. 데이터 원본은 수정하지 않았다.

실행기는 세 Parquet 신호를 staging하고 실제 LangGraph/LLMReasoner에서 queries/evidence를 검증한다. Mock reasoner는 세 신호를 관측할 때까지 모의 가설을 비워 재조사를 유도한다. 이는 RCA 알고리즘/정확도 개선이 아니다. private artifact에는 model result·도구 이력·관측·실패·model metadata가 있으며 공개 요약에는 신호 종류·계수·상태만 있다.

## 검증 명령과 관측

```sh
uv run --locked --no-sync python scripts/build_offline_image.py --egress
uv run --locked --no-sync python -m rca_agent.journal_probe
uv run --locked --no-sync python -m rca_agent.dns_rotation_probe
# 아래 명령은 실제 OpenAI TLS 핸드셰이크만 하며 HTTP·키를 사용하지 않는다.
uv run --locked --no-sync python -m rca_agent.wan_tls_probe --family-policy ipv4-only --lease-seconds 30 --timeout-seconds 5
uv run --locked --no-sync python -m rca_agent.mock_egress --suite
uv run --locked --no-sync python -m rca_agent.docker_integration --suite
make check
make harness-verify
```

- 실제 Docker journal: 정상 완료 2개 row를 재시작 후 재실행하지 않았다. 전송 전·응답 수신 직후 Proxy 강제 종료는 각각 MANUAL_REVIEW 1개로 복구됐다. 세 경우 모두 재시작 모델 HTTP 0건.
- 실제 kernel lease: 승인 IPv4/IPv6 수신 서비스는 정상 연결, 다른 IP/port 거부. 새 집합으로 회전하면 이전 주소가 차단됐고 guardian 없이도 만료 후 차단됐다. IPv6 일부 connect가 timeout이어서 단순 성공 판정하지 않고 단일 idle namespace에서 해당 요청 전후 kernel REJECT 카운터 증가를 확인했다. 원래 errno는 UNVERIFIED로 보존하고 DENIED_BY_KERNEL_COUNTER로 별도 표시했다.
- 실제 WAN: 선택된 OpenAI IPv4 주소 2개가 시스템 CA/hostname으로 TLS 1.3 handshake 성공. 다른 IP/port EHOSTUNREACH. HTTP 요청 0, key 사용 false. 실제 WAN IPv6/TLS, 계정 인증, 모델 schema/count·usage·billing 동작은 미검증.
- 개발용 실제 telemetry: 원본 source disjoint 확인 후 Agent→Broker→Worker→Proxy→TLS Mock 전체 흐름에서 세 신호와 evidence 검증 PASS. 모델 Mock 6회/count 6회, tool/iteration 3회, private artifact 저장 확인. input60/output300/cost0.660은 합성 값이며 실제 API 사용량/비용/정확도가 아니다.
- 기존 Egress TLS/오류 16개 및 3B 통합/장애 13개 시나리오 PASS. 실제 모델 출력·정확도는 측정하지 않았다.
- 단위 회귀는 journal crash/ownership/중복/초과 사용량, DNS 바뀜/만료/잘못된 주소, 원본 identity 중복·file 변조·GT 미전달, 승인 metadata·retention·권한·기한·설정 변경, production→Mock fallback 거부, Proxy 전용 synthetic credential guard를 검증했다.

## 코드·보안 리뷰 및 활성화 조건

네트워크 lease와 public DNS/hostname/TLS의 신뢰 범위를 구분했다. kernel UTC/host·Docker daemon·로컬 이미지와 evaluator metadata는 신뢰 기반이다. Host 관리자, clock rollback, 커널 취약점에 대한 완전한 격리는 주장하지 않는다. lease 기간과 DNS 정책, IPv4-only/dual-stack은 사용자 선택이다.

형식/Body/store=false/service_tier=default/Structured Outputs 및 SDK retry 0을 유지했다. 실제 운영 provider는 승인 없는 count/모델 요청과 key reader 호출을 거부한다. 원문 API 인증 오류를 Worker로 반사하지 않는다. Journal request ID는 background callback에서 바뀌지 않게 binding에 고정했다. Known usage와 billing reconciliation은 구분한다.

유료 실행은 default LIVE_BLOCKED다. [운영 제안](phase4b3d-operator-proposal.md)의 모델·비용·token·호출/반복/출력/timeout, 개발 사례·외부 처리, key 사용, 지역/tier/cache와 수동 보존·공개 정책을 사용자가 확정하고 private approval과 credential FD를 명시해야 한다. 현재 유효한 승인 파일이 없으므로 실제 경로를 실행하지 않았다. 실제 API contract를 최초 1건에서 검증할 수밖에 없는 점과 미확인 청구/취소의 수동 확인 위험도 승인 항목이다. 평가용 20건은 승인 대상이 아니다.

## 최종 로컬 결과

- `make check`: 기존 291개를 유지하고 319개 PASS. `make harness-verify`: 319개 및 로컬 검사 PASS, 전체 결정 DEFERRED(외부 검증 대기).
- Journal Docker 재시작 3개 시나리오 PASS, Egress/TLS Mock 16개 PASS, 이전 3B 통합/장애 13개 PASS. source changes 뒤 다시 빌드한 최종 이미지로 검증했다.
- 개발용 실제 telemetry의 세 신호 TLS Mock 전체 흐름: PASS, Proxy HTTP 12/모델 6회, 세 신호 관측과 private artifact 확인. 이는 Fake 실행이며 모델 정확도·실제 비용 측정이 아니다.
- 실제 WAN IPv4 DNS/TLS handshake-only: 서로 다른 선택 주소 2개 TLS 1.3/시스템 chain/hostname 검증, 다른 IP/port 거부, HTTP 0/keys false. 실제 WAN dual-stack은 미검증이며 초기 운영 제안은 ipv4-only다.
- 실제 private provenance를 포함한 CLI dry-run: 원본 독립성 true, 제안 모델 토큰 상한 비용 0.038 USD 계획. 같은 인수의 승인 없는 --live는 FD/키/API 호출 없이 LIVE_BLOCKED로 거부됐다.
- 최종 Egress 이미지 `sha256:0f6b41c0f431a7186124818707f359f2aa3422e2e34468c3697eb1f4292bb74f`, Docker Desktop Engine 29.1.3 linux/arm64에서 실행했다.
- Code/security review: SQLite/owner-lock 충돌을 분리하고 durable commit 뒤 side effect를 수행했다. background Provider callback의 request ID를 binding에 고정했다. 운영 context에 Mock CA를 전달하지 않고, provider 인증 오류의 key 반사/진단 본문을 Worker에 전달하지 않는다. 승인 없는 key reader/전송, 다른 역할 UID의 key reader 및 production→Mock fallback을 회귀 검증했다. 실제 API 키 검증이나 API 성공으로 표현하지 않는다.
- 임시 컨테이너·network·Volume·CA/socket/test artifact를 제거했다. 개발 cases/private provenance만 준비 결과로 ignored 디렉터리에 남겼다. 실제 운영 journal은 생성/사용하지 않았다.
- 커밋 대상 29개 파일의 경로·내용을 검사했다. 데이터/키/로그/DB 파일 0개, credential 패턴 탐지 0개이며 `.env`, 원본·개발 데이터와 private 결과 경로의 Git ignore를 확인했다. 패턴 검사는 임의의 비밀 탐지를 보장하지 않으며 실제 파일 diff 리뷰와 함께 수행했다. 기존 macOS 격리·IPC runner·graph 파일은 변경하지 않았다.
