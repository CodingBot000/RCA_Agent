# Phase 4B-3B 통합 오프라인 검증

상태: **오프라인 통합·보안 검증 PASS**. 실제 외부 실행은 **LIVE_BLOCKED**이며 활성화 경로가 없다. 아래 실행 근거는 Synthetic/Fake 통합 검증이며 RCA 정확도가 아니다.

## 재현

의존성 설치를 위한 외부 다운로드는 사용자 승인 후 잠금 파일의 Linux 패키지에 한해 수행했다. 이미지 빌드는 명시적 수동 작업이다.

```sh
uv run --locked --no-sync python scripts/build_offline_image.py
uv run --locked --no-sync python -m rca_agent.docker_integration
uv run --locked --no-sync python -m rca_agent.docker_integration --suite
make check
make harness-verify
```

빌드 context는 임시 디렉터리의 잠금 requirements와 `src/rca_agent/*.py`만 포함한다. 저장소 전체, `.env`, 데이터나 인증정보는 COPY하지 않는다. 런타임은 로컬 이미지 ID를 고정해 사용하고 소스 hash label이 현재 코드와 다르면 재빌드를 요구한다. 자동 pull은 하지 않는다. Docker가 없으면 우회 실행하지 않는다.

비공개 blind 실행은 `--blind-source <cases-directory> --case-id <approved-case-id>`로 한 사례만 선택한다. `task.json`에서 case ID와 허용 후보만 내부 계약으로 변환하고 metrics CSV는 임시 Parquet으로 변환한다. 추가 파일·매핑·주입 시점 등은 전달하지 않는다. symlink·hardlink 파일은 거부한다. 원본은 수정하지 않고 임시 복사본을 종료 시 삭제한다. 사례 ID와 원본 경로는 공개 결과에 출력하지 않는다.

## 실행 경계

Agent(UID10003), Broker(UID10002), Worker(UID10001)는 모두 network none, read-only root, cap-drop ALL, no-new-privileges 및 제한된 PID/메모리/CPU로 실행한다. Agent에는 승인된 한 사례의 task/metrics 복사본만 read-only 제공한다. Worker만 쓰기 가능한 tmpfs Volume(0750, owner10001/group10002)을 생성하고 Broker는 같은 Volume을 read-only로 본다. 소켓은 0660이며 Agent에는 Volume과 세션 인증정보가 없다. 실제 API 키·Docker 소켓·평가자 저장소는 마운트하지 않는다.

호스트의 익명 stdin 파이프는 역할별 초기 설정을 전달한다. 세션별 256-bit 인증값은 Broker/Worker에만 전달하며 CLI 인수·환경·Volume·로그에 저장하지 않는다. UDS 양쪽은 Linux SO_PEERCRED로 UID/GID를 확인하고 클라이언트는 디렉터리/소켓 소유권·권한·symlink·inode를 검사한다. 연결 전후 inode가 달라지면 거부한다. 방향과 세션·요청 ID·순번·본문을 HMAC-SHA256으로 인증한다. 동일 ID/내용의 명시적 재전송은 캐시 응답을 사용하고 내용 변경·순서 위반은 거부한다. 불명확한 응답은 자동 재전송하지 않는다. 재시작 복구나 세션 밖의 멱등성은 제공하지 않는다.

UID는 컨테이너/사용자 namespace의 매핑에 영향을 받는다. 현재 Docker Engine에서 실제 SO_PEERCRED로 확인했으며 다른 매핑 환경에서 일치하지 않으면 실패한다. 동일 UID인 임의 프로세스를 UID만으로 구분할 수 없으므로 역할별 UID 분리, Broker read-only mount 및 세션 인증을 함께 사용한다. Docker 관리자/호스트 root나 인증값을 보유한 프로세스의 완전한 장악에 대한 방어는 보장하지 않는다.

## 실제 데이터 흐름

Agent의 기존 LangGraph·LLMReasoner·TelemetryTools → stdin/stdout IPC → Broker의 기존 사례별/전체 OfflineBroker → 인증 UDS → Worker의 OpenAIProvider/Mock HTTP → 역방향 응답 순서다. 기존 PreparedRequest로 입력 토큰 계산 API를 Mock하고 두 원장의 입력·출력 예약과 사용량 정산을 수행한다. Mock 입력 10/출력 50은 시험 응답일 뿐 실제 토큰 수가 아니다. 비용과 한도는 `fixture_budget`의 합성 시험 설정으로 운영 승인 값이 아니다. Worker는 준비된 요청과 계산 결과가 일치해야 모델 Mock을 실행한다.

호스트는 승인된 CaseContext, 프레임 크기·메시지 수·도구/반복 한도와 응답 계약을 검증한다. Agent와 Broker의 응답 ID를 대조하고 기존 관측 claim 검증을 수행한다. IPC/UDS에는 telemetry가 포함될 수 있어 공개 로그로 남기지 않는다. Docker log driver는 none이며 오류는 고정된 코드·단계와 계수만 출력한다. Mock HTTP 카운터에는 본문이나 토큰을 저장하지 않는다.

## 검증 항목과 실제 근거

- 정상 Synthetic E2E: 모델 요청 2회, IPC 전송 3회(중복 1회), Mock HTTP 4회(계산 2/모델 2). 그래프 결과는 기존 evidence 계약으로 검증했다.
- 명시적 UDS 응답 재전송: 같은 요청을 두 번 전송해도 Mock HTTP/모델 호출 수가 증가하지 않았다. 불확실한 교환의 자동 재시도 기능은 추가하지 않았다.
- 비공개 v2 blind 1건: 위의 제한된 변환 후 Fake 전체 흐름 성공. 원본 정답·매핑은 사용하지 않았으며 사례 키와 telemetry를 공개 결과에 기록하지 않았다. 정확도 측정은 하지 않았다.
- 실제 역할 검사: Agent 인증/채널 부재, Agent/Broker/Worker의 합성 정답·mapping·key 부재, Broker/Worker blind 부재 및 외부 TCP 접속 ENETUNREACH 확인. 미마운트 검사는 임의 파일 전체 allowlist 검증과 구분한다.
- 정상 실행 전에 Broker의 소켓 rename이 read-only/권한 오류로 거부됨을 검사했다. 같은 GID지만 UID10004인 별도 테스트 프로세스의 유효 MAC 요청도 Worker가 거부했다.
- `--suite`: 정상, UDS 재전송, 계산 실패, 토큰/비용 한도, 사용량 누락, 응답 지연, Worker 중단, 잘못된 인증값/IPC, symlink, 잘못된 소켓 권한, 위장 소켓의 13개 시나리오를 실행한다. preflight 실패 시 Mock 모델 호출 0건을 확인한다. 중단된 Worker의 카운터는 읽을 수 없으므로 계수 성공을 주장하지 않는다.
- 위장 소켓은 Worker UID로 만들어 peer UID 검사만 통과하도록 한 후 잘못된 MAC 응답으로 인증 실패를 확인했다. 실제 파일 교체 race와 세션/내용 재전송 거부는 오프라인 회귀로도 검사한다.
- 이전 [internal 네트워크](phase4b3b1a-progress.md), [isolated Gateway](phase4b3b-gateway-progress.md), [합성 UDS](phase4b3b-uds-progress.md) 결과는 역사적 근거로 보존한다. 기존 macOS 하네스와 Agent Docker Probe를 유지한다.

## Egress 및 LIVE_BLOCKED

Worker는 network none이며 승인된 외부 연결을 제공하지 않는다. 애플리케이션 정책은 HTTPS api.openai.com:443의 Responses/count 경로만 허용하고 userinfo·query·다른 호스트/포트와 경로를 거부한다. SDK는 환경 프록시를 사용하지 않고 redirect를 따르지 않는다. TLS 설계는 기본 CA chain 검증과 hostname 검증, TLS 1.2 이상을 요구한다. 로컬 Mock 목적지/포트·redirect 거부와 TLS 기본 설정은 테스트했지만 실제 TLS handshake·실제 인증서 수용 및 네트워크 allowlist는 미검증이다.

`egress_policy.live_client()`는 무조건 LIVE_BLOCKED로 실패한다. network none의 전면 차단은 api.openai.com만 허용하는 OS allowlist와 다르다. 향후 Worker만 네트워크를 여는 단계에서는 목적지/DNS/IPv4/IPv6/프록시 우회 및 TLS chain/hostname 오류·redirect를 실제 OS 정책과 로컬 TLS Mock으로 검증해야 한다. 아직 수행하지 않은 외부 API·토큰 계산 정확도·스키마 수용을 성공으로 표현하지 않는다.

## 최종 로컬 검증 및 리뷰

- `make check`: 기존 218개를 유지하고 신규 45개를 추가해 263개 PASS.
- `make harness-verify`: 263개 PASS, 로컬 검사 PASS. 전체 하네스 결정은 외부 검증 대기로 DEFERRED이며 오프라인 통합 완료와 구분한다.
- 최종 소스로 빌드한 Docker 이미지 `sha256:42f6982cc105894341aa9fd03c2fdbcf42390aa3108ad7dffd55461fbea5f407`, Engine 29.1.3 linux/arm64에서 13개 통합/장애 시나리오 PASS. 비공개 v2 고유 blind 1건의 Fake 전체 흐름도 반복 확인했다.
- 기존 Agent Docker Probe 및 macOS IPC 회귀 검사 PASS. 기존 macOS 격리 프로필과 실행기 파일은 변경하지 않았다.
- 코드·보안 리뷰에서 정상 `error:null` 응답을 오류로 취급하던 결함, 프레임의 inactivity timeout만 적용되던 부분, PreparedRequest 내부 JSON 중복 키/NaN 허용, 예상 밖 CLI 예외와 컨테이너 요약의 로그 반사 위험, 생성 중 timeout의 자원 등록 시점을 보완하고 회귀 테스트를 추가했다. 인증값은 객체 repr에서도 숨긴다.
- 기존 budget retry/동시성 테스트를 유지했다. 통합 실행은 명시적 concurrency 1/retries 0이며 사용량 불명확 시 차단한다. UDS 재전송 시험은 성공 응답을 받은 뒤에만 명시적으로 반복하며 불확실한 요청을 자동 재실행하지 않는다.
- 단계별 과거 Probe는 목적지 접근을 발견하면 계속 BLOCKED를 반환한다. 이것을 실제 network-none 통합 실행의 실패 또는 외부 격리 완료로 바꾸어 표현하지 않는다.
- Git 대상 28개 파일에서 데이터/키/로그 금지 경로 0건 및 API 키·토큰·개인키 패턴 0건을 확인했다. `.env`와 phase1-data는 계속 ignored다. 이 패턴 검사는 전체 비밀정보 격리의 증명이 아니라 공개 patch 검토의 보조 검사다.

## 첫 유료 1건 파일럿 진입 조건

현재 실행 명령으로 실모델을 연결할 수 없다. 별도 사용자 승인 전에 실행할 수 있는 유료 경로는 없다. 다음 조건을 먼저 충족해야 한다.

1. Worker 전용 OS egress allowlist 및 우회/TLS 검증, Broker/Agent network none과 파일 경계 재검증.
2. 외부 Provider·세션 복구/불명확 청구·재시작 시 재전송 정책과 키 주입 경계 구현·리뷰.
3. 승인 모델·현재 가격·사례/전체 Budget·출력/시간/동시성(초기 1)·보존/삭제 정책의 사용자 결정 및 문서/설정 반영.
4. 비공개 blind 1건의 승인된 입력만 준비하고 실제 API 스키마/토큰 검증의 미확인 상태를 명시. 실제 요청의 취소나 청구 방지를 보장하지 않는다.
5. 별도 명시적 승인 후에만 1건 파일럿 실행. 20건 평가와 자동 결제/키 생성은 포함하지 않는다.
