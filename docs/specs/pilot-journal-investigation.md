# PR #19 이후 journal 누락 조사

범위: 기존 `attempt.json`·`outcome.json`과 Git 이력·Docker metadata·존재하는 journal만 읽기 전용으로 조사한다. `.env`·API key·원문 telemetry는 읽지 않고 API 요청/유료 실행을 하지 않는다. 기존 파일·volume·청구 상태를 삭제·초기화·정산하지 않는다.

## 수락 기준·영향도·테스트 설계

- private owner 0700/0600 기록의 무결성과 실행 binding을 대조한다. 공개 보고에는 case/source ID·volume 이름·private hash·key를 넣지 않는다. 새 진단 receipt만 별도 ignored owner-only 경로에 보존할 수 있다.
- 과거 commit의 소스 hash와 당시 image ID 후보(잔존 캐시·로컬 build log)를 사용해 attempt.binding을 재계산한다. 일치하지 않으면 당시 코드 버전을 입증했다고 판정하지 않는다.
- parent key acquisition, child 승인 검증, LiveDeployment/Proxy 생성, Proxy credential FD read, count/model HTTP 단계를 구분한다. FAILED/INVALID_CONFIGURATION은 과거 코드의 예외 경계와 대조하며 broad 오류만으로 정확한 exception 원문을 복원했다고 주장하지 않는다.
- 현재 모든 local Unix Docker context를 변경 없이 조회한다. engine alias, 기록된 volume, 다른 managed journal의 존재를 집계한다. missing/unreachable/skipped context를 빈 journal로 간주하지 않는다. 존재할 때만 volume inspect 후 network=none/read-only helper로 SENT/COMPLETED/MANUAL_REVIEW를 조사한다. 누락 volume을 mount해 새로 생성하지 않는다.
- 재현은 합성 승인/데이터와 차단 대역만 사용한다. 현재/과거 CLI의 사전 승인 실패와 실행 경계 진입 후 실패의 분류, binding 실패·상충 journal·unknown 상태·기록 불변성·진단 로그 누출을 테스트한다. runtime 승인/키 로딩·재시도·Budget 계약은 변경하지 않는다.
- 충분한 사전 전송 실패 근거가 있을 때만 별도 승인·감사 기록을 요구하는 수동 복구 **설계**를 제시한다. 이 작업에서는 복구를 실행하지 않고 기존 차단을 유지한다.

## 관측과 결론

기록된 시도는 2026-11-17 03:06:49 KST에 생성됐고, 결과 파일 mtime은 같은 날 03:30:01 KST다. 둘 다 owner 0600/단일 regular file, 디렉터리는 owner 0700이다. 실제 키·`.env`·원문 telemetry는 읽지 않았다. 원본 내용 hash를 진단 전후 비교해 불변을 확인했다.

### 코드/데이터 binding과 실패 단계

- 과거 build log의 이미지 ID 2개와 Git `cf28490085e79de24a6803a4a9d329f4f2699b2f`의 source hash, 현재 보존된 config/manifest/provenance/lock으로 snapshot을 재구성했다. attempt.binding과 정확히 일치하는 순서쌍은 1개다. private hash·case ID·volume 이름은 공개하지 않는다.
- 해당 commit의 `pilot.main`은 child의 `approved_profile` 및 total Budget 검사 뒤에야 LiveDeployment를 감싼 try에 진입한다. 이 try 내부의 Exception은 `MANUAL_REVIEW_REQUIRED`로 변환된다. `FAILED/INVALID_CONFIGURATION`과 기존 결과의 정확한 필드 집합은 이 경계 **이전** 실패와 일치한다. 구조도 AST로 검사했다.
- JSON timeout `30`과 CLI float `30.0`의 settings hash 불일치를 합성 데이터로 다시 재현했다. 이는 child approved_profile에서 결정적으로 반려되는 결함이며 PR #19에서 수정됐다. 다만 원래 승인 파일과 exception 원문은 남아 있지 않으므로 과거의 단일 exception 메시지/다른 선행 오류까지 복원했다고 단정하지 않는다. 입증되는 정확한 범위는 **child pre-dispatch 승인/설정 검증 단계**다.

| 단계 | 기존 기록에 결합한 코드 경로 판단 |
| --- | --- |
| parent WAN DNS/TLS 사전검사 | parent 흐름에서 선행. 이 TLS 검사에 모델 HTTP·인증은 없음 |
| parent 터미널 최종 승인 및 getpass | outcome 저장에 도달하려면 이 단계를 통과해야 함. 당시 버전은 `.env`를 사용하지 않았음 |
| child CLI 파싱/계획/개발 사례/승인 검증 | 실패 범위. timeout settings 불일치 재현 |
| LiveDeployment 생성/운영 journal volume 생성 | 미도달 |
| 운영 Egress Proxy 생성 및 Proxy용 credential FD 읽기 | 미도달 |
| count/Responses HTTP 전송 | 확인된 bound 코드 경로에서 미도달 |

parent가 키를 메모리/pipe로 전달한 단계와 **운영 Proxy가 키를 읽어 인증 요청을 보내는 단계**는 다르다. 따라서 “키를 전혀 입력하지 않았다”는 결론이 아니며, “parent의 DNS/TLS용 임시 컨테이너도 없었다”는 결론도 아니다. 이 판단은 보호된 로컬 기록·Git 이력·정상 host/code 실행이라는 프로젝트 신뢰 범위에서의 코드 경로 근거다. provider의 실제 청구 내역을 조회해 0달러를 증명한 결과는 아니다.

### Docker와 journal

현재 `default`, `desktop-linux`는 모두 접근 가능한 local Unix context이며 **같은 Docker engine**이다. 두 context 모두 기록된 이름의 volume이 없고 다른 `rca-pilot-journal-` 이름도 없다. 발견된 journal DB가 없어 SENT/COMPLETED/MANUAL_REVIEW 건수는 **산출 불가(null)**이며 0으로 대체하지 않았다. 누락 volume을 mount/run해 생성하지 않았다.

당시 engine ID는 attempt에 기록되지 않았다. 현재 alias 불일치나 다른 managed volume으로 이름이 바뀌었다는 증거는 없지만, 과거 Docker VM reset/prune/volume 삭제 이력을 이 inventory만으로 배제할 수는 없다. 당시 source label의 image cache도 남아 있지 않아 별도 build log로 binding을 재구성했다. 다만 이 **기록된 시도**의 bound 코드 경로는 운영 volume 생성 자체보다 앞서 반려되므로, 생성 전 실패가 volume 부재를 설명한다. volume 부재만을 API 미전송 근거로 삼지 않았다.

### 위험 판정

`CHILD_PRE_DISPATCH_FAILURE / manual_recovery_plan_eligible=true`로 판정한다. 이 시도의 정상 코드 경로에서 중복 모델 요청/과금이 발생할 경로는 확인되지 않았다. 다른 시도·계정 활동·로컬 기록 위변조까지 배제하는 provider 측 확증은 아니다. `billing_status=UNRECONCILED`, `recovery_authorized=false`, `execution_state=LIVE_BLOCKED`는 그대로 유지한다.

binding 불일치, 과거 예외 경계 불명, 다른 outcome/usage 값, 접근 불가/remote context, 상충하는 journal/다른 managed journal이 있으면 수동 복구 적합 판정을 하지 않는다. 코드/기록 신뢰를 확인하지 못하거나 계정 사용량이 상충하면 BLOCKED를 유지한다.

## 재현·진단 감사 기록

```sh
uv run --locked --no-sync python scripts/audit_pilot_attempt.py
uv run --locked --no-sync python scripts/audit_pilot_attempt.py --record
```

두 명령은 키·API·복구를 사용하지 않는다. `--record`만 새 `pilot-private-results/journal-audits/<random>.json` 진단 receipt를 owner 0700/0600으로 만든다. 원본 파일 hash·recorded journal identity·일치한 image ID·과거 commit과 새 진단 코드 버전/해시를 비공개로 보존한다. 이는 **복구 승인 파일이 아니며** `recovery_approval=false`다. 공개 결과는 case/source ID·journal 이름·private fingerprint를 제외한다. `/tmp` build log나 데이터가 없어 binding을 재검증하지 못하면 새 진단은 BLOCKED이며 이전 receipt를 자동 실행 허가로 사용하지 않는다.

## 별도 승인이 필요한 수동 복구 설계 — 이번에는 미실행

이 작업의 진단기에는 reset/recover/재실행 기능이 없다. 다음은 후속 수동 복구의 수락 조건이며, 사용자 승인 없이 runtime gate를 열지 않는다.

1. 사용자는 이 시도의 pre-dispatch 근거와 한계를 검토하고, 기록을 보존한 **복구 준비만** 별도 승인한다. 이는 유료 실행 승인이 아니다. 계정 측 상충이 있거나 확신이 없으면 승인하지 않는다.
2. 기록된 시간대를 포함하는 **2026-11-17 03:00~03:45 KST**(UTC 2026-11-16 18:00~18:45)의 OpenAI Usage/Billing에서 올바른 조직·project·gpt-6-luna, count/Responses 활동과 비용 항목을 사용자가 확인한다. 집계/청구 반영 지연·다른 활동을 구분하고 불명확하면 보류한다. 로컬 기록에는 provider request ID가 없으므로 지출을 특정 요청에 자동 귀속하지 않는다. 이 작업은 계정에 접속하지 않았다.
3. 승인 시각/운영자 UID/명시 동의/진단 receipt hash/원본 attempt·outcome hash/현재 코드·설정·provenance·Docker context/engine baseline/기존 journal 이름을 포함하는 새로운 owner-only 감사 기록을 O_EXCL로 남긴다. 원본 attempt/outcome/Journal은 **현재 위치에서도 그대로 보존**하며 초기화·삭제하지 않는다. billing 필드를 임의로 settled/0으로 바꾸지 않는다.
4. 후속 복구 구현이 필요하면 별도 리뷰/회귀/CI를 거친 명시적 수동 gate만 추가한다. 기존 기록 hash와 pre-dispatch 판정을 다시 대조하고 owner lock을 잡아 **별도 one-shot recovery-attempt 기록**을 HTTP·키 읽기 전에 durable claim한다. 원본을 지우거나 기존 상태 문자열을 READY로 덮어쓰지 않는다. user 승인과 이 감사 기록이 없으면 기존 차단이 계속 적용돼야 한다.
5. journal은 **기존 attempt에 기록된 동일 이름**만 사용한다. 새 UUID/새 journal로 우회하지 않는다. absent를 다시 확인한 경우에도 이 이름의 최초 생성만 별도 승인된 재실행에서 허용한다. 중간에 volume이나 SENT/COMPLETED/MANUAL_REVIEW가 나타나면 중단하고 수동 확인한다. 다른/원격 engine으로 이동해 차단을 피하지 않는다.
6. 실제 실행은 수정된 최신 code/settings/provenance에 결합한 **새로운 최종 터미널 유료 실행 승인**과 기존 Budget/retries=0을 다시 요구한다. 복구 승인만으로 `.env`를 읽거나 count/모델 요청을 보내지 않는다. 실패·취소 뒤 두 번째 recovery claim을 자동 생성하지 않는다.

## 검증·코드/보안 리뷰

`make check`, `make harness-verify`: 기존 377개 포함 **398개 PASS/no skips**. 로컬 검사 PASS이고 전체 하네스는 외부 검증 대기로 DEFERRED다. 새 21개는 예외 경계(AST와 합성 CLI 실행), pre-dispatch/deployment/모의 dispatch 실패 분류, binding/image 순서/설정 변조, 누락·상충 journal/context, 원본 byte 불변·receipt 비승인·권한·symlink·비공개 오류 반사 방지를 검증한다. 실제 API·키는 사용하지 않았다.

Runtime/Provider/승인 schema·키 로딩·Budget·IPC·macOS/컨테이너 격리 코드는 변경하지 않았다. scripts의 host 진단기만 추가해 `src/rca_agent` source hash와 기존 image 계약도 유지한다. Docker 조회는 context를 변경하지 않고 local Unix endpoint만 사용한다. 기존 기록/volume/청구 상태의 변경 명령은 없으며 실제 복구는 별도 사용자 승인까지 BLOCKED다.
