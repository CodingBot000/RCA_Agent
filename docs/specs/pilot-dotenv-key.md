# 파일럿 .env 키 로딩 변경

사용자는 기존 `.env`의 `OPENAI_API_KEY`를 사용하고 터미널 재입력을 없애되 최종 유료 실행 승인을 유지하도록 요청했다. 이 결정은 기존 비에코 입력 정책을 대체한다. 실제 유료 실행 승인을 대신하지 않으며 Codex는 실제 `.env` 값을 읽거나 실제 API를 호출하지 않는다.

## 계약·영향도·수락 기준

- `local_pilot --run`의 터미널·WAN 검사·정확한 최종 승인 문구·승인 파일·attempt/journal·예산 계약을 유지한다. `.env`의 내용은 최종 승인 이후 기존 key reader 단계에서만 읽는다. 기본 점검·status·승인 거부 경로에서는 키를 읽지 않는다.
- host 저장소 루트의 `.env`만 읽는다. 파일은 owner UID/0600/regular/single link/최대 64KiB여야 하며 symlink·hardlink·FIFO·권한 오류는 고정 오류로 거부한다. 실제 파일은 수정하지 않는다. env 파일을 컨테이너에 마운트하거나 이미지에 복사하지 않는다.
- 단일 `OPENAI_API_KEY=...`에 대해 bare/single quote/double quote, 선택적 export 접두사와 주석을 지원한다. 빈 값·누락·중복·잘못된 인용/다중행은 거부한다. interpolation·escape·shell 실행·os.environ 등록은 하지 않는다. 다른 dotenv 변수와 프로세스의 OPENAI_API_KEY는 키 대체값으로 사용하지 않는다.
- 읽은 키는 기존 ASCII 1~4096 검증 뒤 메모리/일회용 FD로 전달한다. Agent/Broker/Worker·argv·child env·공개 출력·journal·approval에 키를 넣지 않는다. getpass fallback은 제거한다.
- 파일/키 오류도 기존 보수적 attempt 정책을 유지한다. 승인 후 실패하면 자동 새 journal/재시도를 하지 않으며 status와 수동 확인을 따른다. .env의 디스크 저장 자체는 이번 사용자 선택이며 메모리/swap 완전 소거를 보장하지 않는다.

## 테스트 설계

합성 `.env`만 사용해 정상 bare/quoted/export/comment 파싱과 누락·빈 값·중복·인용 오류·형식/크기·권한·소유자·symlink/hardlink/FIFO 거부를 검증한다. 승인 전 key reader 0회, 승인 후 지정 root 1회, 인수/env/출력 누출 부재, 예산/IPC/동시성/재시작 회귀를 유지한다. 실제 키·활성 운영 승인·실제 API 요청 없이 make check/harness와 Mock FD 전달을 검증한다.

## 결과

키 로딩은 합성 파일로만 검증하며 실제 `.env` 내용은 조회·출력하지 않는다. `make check`, `make harness-verify`의 최종 결과는 아래 기록에 반영한다. 최종 유료 실행은 사용자 터미널 승인 전까지 LIVE_BLOCKED다.

검토 중 발견한 독립 결함도 수정했다. JSON Budget의 정수 timeout `30`과 CLI parser의 실수 `30.0`이 같은 의미인데 서로 다른 settings hash를 생성해 승인 검증이 실패했다. 수정 전 실패를 재현하고 timeout을 float로 정규화해 동일 값만 동일 해시가 되도록 했다. 실제 timeout 변경은 여전히 승인을 무효화한다. launcher가 만든 인수를 실제 parser/Budget/plan/approved_profile로 왕복하는 회귀도 추가했다. 승인 schema·Budget 값·한도는 변경하지 않았다.

기존 로컬 attempt/outcome 기록을 확인했으며 결과는 FAILED/INVALID_CONFIGURATION이고 연결된 journal volume은 현재 확인되지 않았다. 실제 키나 원문 telemetry는 읽지 않았다. 과거 시도를 이번 변경에서 삭제·초기화하지 않으며, volume 부재는 `JOURNAL_UNAVAILABLE_MANUAL_REVIEW`로 명시한다. 재실행을 위해 새 journal을 자동 생성하지 않는다. 이 상태와 새 `.env` 지원 기능의 오프라인 검증을 구분한다.

최종 로컬 결과: `make check`와 `make harness-verify`는 377개 PASS/no skips, 로컬 하네스 PASS(전체 판정은 외부 검증 대기 DEFERRED). 정상/오류 dotenv 25개와 승인 후 지정 root 읽기·동의 거부·기존 FD 전달·CLI 왕복·timeout 해시·journal 누락의 회귀를 확인했다. 소스 변경 후 Docker 이미지를 다시 빌드하고 Egress TLS Mock 정상 E2E 1개를 재실행해 PASS를 확인했다. 기존 네트워크/IPC/approval/Budget 계약은 변경하지 않았다. 실제 API 요청·실제 키 사용·운영 승인 파일 생성은 이번 작업에서 0건이다. 임시 Docker 자원은 정리했고 사용자 관리 `.env`와 기존 private attempt/outcome은 보존했다.
