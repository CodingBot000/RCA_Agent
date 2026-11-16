# v0.9 사용자 로컬 파일럿 실행기

기준선: PR #17, `aa9f99a62e5321064eccfa551fe8bcf151738e26`. 선택 설정은 기존 `configs/pilot-preflight-v0.8.json`을 그대로 사용한다. 코드·오프라인 검증·리뷰·PR은 이번 작업에서 완료하되 실제 키 입력·최종 유료 실행은 사용자의 독립된 로컬 터미널에서만 수행한다. Codex는 실제 key·활성 운영 승인 파일·유료 요청을 사용하지 않는다.

## 계약과 수락 기준

- 기본 check는 main/원격 추적 ref/clean tree, 고정 설정, 개발 manifest/provenance·평가 20건 원본 독립성·세 신호·권한, image source hash, Budget을 확인하고 비공개 ID/해시/원문 없이 집계만 출력한다. 키·승인·journal을 생성하지 않는다.
- run은 실제 대화형 terminal 및 알려진 Codex 세션 부재를 요구한다. 사람의 로컬 터미널임을 OS가 완전히 증명하는 장치는 아니므로 사용자 직접 실행 절차와 결합한다. WAN DNS/TLS만 먼저 검사하며 HTTP 인증 요청은 금지한다.
- 모델/비용/토큰/횟수/timeout, 전송 데이터와 provider 보존, 로컬 수동 보존, 실제 API 미검증·불명확 과금 위험, 계정 확인, 승인 유효기간 제안 1시간을 표시한다. 별도 최종 문구 입력 전에는 활성 승인·키 reader·child API 경로가 없다.
- 승인 후 owner 0700 디렉터리/0600 파일을 O_EXCL/NOFOLLOW로 생성한다. 기존 `approval.approved_profile` 계약은 변경하지 않는다. code/settings/provenance/runtime/retention/expiry를 결합한다. 설정/코드/데이터가 입력 대기 중 바뀌면 차단한다.
- owner lock 및 영속 attempt 기록으로 동시에 2회 또는 실패 후 자동 재실행하지 않는다. 단일 사례의 journal 이름과 결과 디렉터리를 재사용하며 기존 SENT/MANUAL_REVIEW는 물론 이전 운영 시도의 자동 후속 요청도 막는다. status는 같은 journal을 read-only로 검사한다.
- 비에코 getpass는 echo fallback을 거부한다. 실제 key는 환경·인수·일반 파일에 기록하지 않고 메모리/일회용 pipe FD>=3으로만 직접 Python child에 전달한다. child stderr·비정상 stdout·예외 메시지를 공개 로그에 반사하지 않는다. core dump를 끄고 secret pipe/승인 파일을 종료 시 정리한다. 메모리·swap 완전 소거는 보장하지 않는다.
- Proxy만 key를 사용하며 나머지 역할·격리·PreparedRequest·Budget·retries=0을 유지한다. 실제 요청 전에 durable journal, 사용량 불명확 시 MANUAL_REVIEW를 유지한다.

## 영향도와 테스트 설계

변경은 새 host launcher 및 운영 journal 재개 차단·FD 입력 방어, DNS/TLS 사전점검의 독립 resolver timeout이다. macOS sandbox와 Agent/Broker/Worker network=none을 유지한다. 승인 계약 필드를 추가하거나 바꾸지 않는다.

음성 검증: 비TTY/Codex 세션/승인 거부/설정·provenance 변조/unsafe 파일/이중 실행/기존 attempt·journal/키 입력 실패/잘못된 FD/timeout/비정상 child 출력. 합성 키와 임시 승인 fixture만 사용하고 실제 API는 Mock만 허용한다. 기존 321개와 신규 회귀, make check/harness, Docker Mock·TLS·DNS·journal을 실행한다. 실제 인증·schema/count·청구 성공 및 RCA 정확도는 별도 사용자의 실제 실행 전까지 미검증이다.

## 실행·결과

사용자는 Codex가 캡처하는 터미널이 아닌 Terminal/iTerm의 독립 세션에서 다음 한 명령을 직접 실행한다. 아래 명령은 안내이며 Codex는 이번 작업에서 실행하지 않는다.

```sh
/Users/switch/Development/Harness/RCA_agent/RCA_AGENT/.venv/bin/python -m rca_agent.local_pilot --run
```

1. 실행기는 깨끗한 main과 origin/main 추적 ref 일치, 로컬 Unix socket Docker context와 소스에 맞는 이미지, 고정 개발 사례/provenance/세 신호를 검사한다. 원격 ref를 자동 fetch하거나 외부 패키지를 다운로드하지 않는다. 실행 전 main 동기화와 이미지 준비가 필요하다.
2. DNS 5초·lease 90초·TLS 20초로 시스템 CA와 고정 호스트를 검증한다. 이 단계는 HTTP 0/키 0이며 account 인증 검증이 아니다.
3. 선택한 모델·사례·최대 모델/count 8회씩·토큰·soft 비용 예산, timeout/DNS/관측, 외부 전송, provider 보존·cache, 로컬 수동 보존 및 승인 유효기간 제안 1시간을 표시한다. 계정 권한·global Standard 가격/부가 요금·잔액·data policy를 직접 확인하지 못했거나 가정과 다르면 승인하지 않는다.
4. `동의하고 개발용 1건 유료 실행 승인`을 정확히 입력할 때만 활성 승인 파일을 만든다. 입력 대기 중 code/settings/provenance/이미지/manifest가 바뀌면 차단한다. 이 최종 문구는 1시간 유효기간·외부 전송·보존·키 사용·불명확 과금 위험을 포함한 별도 승인이다.
5. 비에코 key 입력 후 한 번의 파일럿 child만 시작한다. key를 프롬프트/채팅/파일/env/CLI 값에 붙여넣지 않는다. `.env`는 읽지 않는다. stdout은 허용한 상태·계수·비용 추정치·신호 종류만 전달하고 실제 분석 결과/관측은 private artifact에 저장한다.
6. 완료·실패·취소 모두 활성 승인 파일을 제거한다. SIGKILL/호스트 종료로 남으면 유효기한이 지나도 자동 재개하지 않는다. 별도 child process session을 사용하고 timeout/interrupt 때 SIGINT로 정리하며 실패하면 종료한다. 원격 취소·환불은 보장하지 않는다.

Private 저장 위치는 `pilot-private-results/local-pilot-v09`다. `attempt.json`에는 첫 시도의 code/data binding과 고정 journal 이름·출력 경로만, `outcome.json`에는 안전한 공개 계수만 저장한다. `artifacts/`에 model 결과·도구 이력·관측·실패 metadata가 저장된다. owner 디렉터리 0700/파일 0600이며 Git ignore 대상이다. API key는 일반 파일·journal에 기록하지 않는다. journal은 Docker disk volume으로 유지하며 자동 삭제하지 않는다.

재실행 대신 아래 명령으로 같은 volume을 network=none/read-only helper에서 검사한다. Docker volume 자체가 없거나 손상되면 상태를 추정하지 않고 수동 확인으로 남긴다. 이 명령은 key를 읽거나 API 요청을 보내지 않는다.

```sh
.venv/bin/python -m rca_agent.local_pilot --status
```

이전 attempt가 있으면 성공 여부와 무관하게 자동 두 번째 실행을 거부한다. 기존 SENT/MANUAL_REVIEW뿐 아니라 완료 기록이 있는 운영 journal도 수동 조정 전 새 세션으로 시작하지 않는다. 서로 다른 request를 생성해 세션 Budget을 다시 시작하는 우회를 막기 위한 보수적 단일 파일럿 정책이다. attempt를 삭제하거나 새 journal을 지정해 우회하지 않는다.

로컬 결과 7일/개발 telemetry 14일/journal 청구 조정 후 90일은 수동 보존 정책이다. 삭제 시점의 기준과 미확인 청구 여부를 운영자가 확인한다. 실제 provider의 보존을 이 로컬 정책으로 변경할 수 없다. host 관리자·동일 UID 침해·Docker daemon·system clock·로컬 이미지·evaluator metadata는 신뢰 기반이며 echo fallback 거부나 Codex 환경 marker가 사용자 터미널의 비기록 상태를 완전히 증명하지 않는다.

## 검증 근거 및 실모델 보고 상태

- 기존 321개를 유지한 `make check`, `make harness-verify`: 349개 PASS/no skips. 전체 하네스는 실제 외부 검증 대기로 DEFERRED다.
- 새 회귀 28개는 승인 거부/입력 변경/동시·중복 시도, non-TTY·Codex 환경, 비에코 fallback, 키 없는 기본 준비, FD 형식/길이/EOF/deadline, child 인수/env·출력 정제·timeout, private 파일·same-volume read-only status 및 SENT/MANUAL_REVIEW/COMPLETED의 운영 재시작 거부를 검증했다. 승인/credential은 모두 임시 합성 fixture다.
- 공식 [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [가격](https://developers.openai.com/api/docs/pricing), [Count](https://developers.openai.com/api/reference/resources/responses/subresources/input_tokens/methods/count), [데이터 정책](https://developers.openai.com/api/docs/guides/your-data)을 재확인했다. 기존 Standard global conservative 입력 USD 0.50/M·출력 0.75/M 및 USD 0.038 계획은 유지한다. 계정 모델 접근·부가/count 과금·프로젝트 data policy는 미검증이며 운영자가 확인해야 한다. 가격 가정이 다르면 실행하지 않는다.
- SDK 내부 schema helper 의존성과 고정 lock 회귀를 유지한다. Structured Outputs 지원 문서/Mock 성공은 실제 schema 수용으로 판정하지 않는다. 실제 유료 실행은 사용자 직접 승인 전까지 LIVE_BLOCKED다.
- 실제 인증/schema/count·반환 usage·청구·RCA 정확도·원인 후보 결과는 이번 Codex 작업에서 미실행/미검증이며 비용/성능 수치를 만들지 않는다. 실제 1건의 관측 근거와 실패를 숨기지 않고 private artifact로 보존하며, 공개 결과는 집계만 사용한다. Ground Truth 비교는 자동 실행하지 않는다. 평가용 20건의 실모델 진입은 아직 승인되지 않았고 이 파일럿 결과·청구·보안 검토 뒤 별도 결정이다.

최종 로컬 Docker 관측:

- 기존 통합/장애 suite 13개, Egress TLS/오류 suite 16개, journal crash/restart 3개 PASS. journal 재시작 모델 HTTP는 모두 0건이다.
- evaluator-only 20개 원본 mapping/official ID binding과 private provenance·파일 hash를 재검사하고 세 Parquet 신호 전체를 로드했다. 실제 개발 데이터의 TLS Mock E2E에서 3개 신호, 모델 Mock 6회/count 6회, private artifact 0600 및 역할 격리를 확인했다. GT·매핑·선택 키를 모델 역할에 전달하지 않았다.
- WAN ipv4-only/DNS 5초/lease 90초/TLS timeout 20초: 선택 주소 2개 TLS 1.3 시스템 CA/hostname PASS, 다른 IP/port EHOSTUNREACH. 실제 API HTTP 0/keys false. DNS rotation/expiry의 local IPv4/IPv6도 PASS이며 일부 IPv6 raw timeout은 별도 kernel REJECT 카운터 증가로 판정해 raw UNVERIFIED를 보존했다. WAN IPv6는 미검증이다.
- source hash `433072afd8b25dd54e823d4427e4f9c3ae9d0ff38983de72e8768abe325004bc`, 최종 Egress image `sha256:1f000c56aac5cab5f80a93d562655152f15294a0b58a0ad962771825120c974f`. Docker Desktop Engine 29.1.3 linux/arm64에서 source label 일치를 확인했다. 공식 [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)·[Responses](https://developers.openai.com/api/reference/resources/responses/methods/create)·[rate limits](https://developers.openai.com/api/docs/guides/rate-limits)도 재확인했으며 실제 조직/project/model quota는 사용자 확인 대상이다.

리뷰에서는 기존 journal을 다른 요청으로 재개해 세션 예산을 초기화할 가능성을 차단했고, 인수 parser의 accidental key echo·비에코 fallback·child 진단 반사·main/source 변경·pipe 형식 및 무기한 read를 방어했다. 분석 실패여도 이미 확인한 사용량을 버리지 않고 모델 호출 수/신호 종류/고정 오류 분류를 집계에 보존한다. `approval.approved_profile` schema·macOS IPC 격리·network=none·PreparedRequest 계약은 변경하지 않았다. 최종 사용자 승인이 없으므로 실제 실행은 LIVE_BLOCKED로 종료한다.
