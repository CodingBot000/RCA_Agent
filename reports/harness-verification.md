# Harness 자체검증 보고서

검증일: 2026-11-04 (Asia/Seoul)  
종합 판정: **보류** — 필수 외부 검증이 BLOCKED이므로 `HARNESS_READY`를 선언하지 않는다. 현재 확인된 로컬 검증의 미해결 FAIL은 없다. 로컬 명령의 성공은 전체 완료 판정이 아니다.

| 검증 대상 | 검증 명령 및 실제 결과 | 상태 | 증거 및 남은 작업 |
|---|---|---|---|
| 필수 로컬 검증 실행기 | `UV_OFFLINE=1 make harness-verify`: lint·format·동기화·전체 테스트 성공, 30 passed | PASS | [실행 로그](logs/local-verification.log), [검증기](../scripts/harness_verify.py) |
| 정상·음성 계약 및 회귀 | 위 명령의 pytest: 정상 입력, 없는 evidence ID, 미허용 후보, JSONL 오류, 중복 등 테스트 통과 | PASS | [계약 테스트](../tests/test_contracts.py), 실행 로그. 예시 기반 검증이며 RCA 품질 검증은 아님 |
| 정답 격리 보조 검사 | 위 명령의 pytest: 정답 필드·표식·파일·심볼릭 링크 거부 테스트 통과 | PASS | [보안 테스트](../tests/test_isolation.py). OS 격리 자체 및 모든 누출 경로의 보장은 미검증 |
| 검증 스크립트 자체 | 위 명령의 pytest: 명령별 실패, 지침 불일치, 파일 부재, 실행 불가 및 정상 경로 확인 | PASS | [검증기 테스트](../tests/test_harness_verify.py) |
| AGENTS/CLAUDE 일관성 | `make harness-verify`: `CLAUDE.md`가 오직 `@AGENTS.md`를 가져오고 공통 작업 순서가 존재함을 확인 | PASS | [실행 로그](logs/local-verification.log). 구조 검사이며 실제 모델의 준수 보장은 아님 |
| Codex 실제 지침 인식 | `codex exec --skip-git-repo-check --ephemeral --sandbox read-only`로 도구 사용 없이 로드된 순서를 질문. 기본 모델과 `--model gpt-5.3-codex` 재시도 모두 종료 코드 1, 계정에서 모델 미지원 오류 | BLOCKED | [기본 실행](logs/codex-smoke.log), [재시도](logs/codex-smoke-retry.log). 사용 가능한 계정/모델 설정 후 재실행하여 실제 응답 확인 |
| Claude Code 실제 지침 인식 | `claude -p --tools '' --no-session-persistence`로 같은 질문: 종료 코드 1, `Invalid API key` | BLOCKED | [실행 로그](logs/claude-smoke.log). 유효한 Claude 인증 후 재실행하여 실제 응답 확인 |
| CI 설정 | `.github/workflows/ci.yml`의 `harness-verification` job에서 설치 후 `UV_OFFLINE=1 make harness-verify` 실행하도록 설정 | PASS | [workflow](../.github/workflows/ci.yml). API 키나 모델 호출이 검증 단계에 필요하지 않음. 의존성 설치는 네트워크 필요 |
| 실패 전파 및 원상 복구 | 임시 프로젝트 복사본에 실패 테스트를 추가해 `UV_OFFLINE=1 make harness-verify` 실행: 실패 1건, make 종료 코드 2. 별도 skip 주입도 종료 코드 2. 임시 디렉터리는 자동 삭제 | PASS | [실패 주입](logs/negative-ci-probe.log), [skip 주입](logs/skip-probe.log), [복구 후 실행](logs/local-verification.log). 실제 원격 CI의 실패 여부는 별도 조건 |
| GitHub Actions 정상·실패 실행 및 필수 검사 지정 | 현재 프로젝트에 Git 저장소 및 연결된 원격 실행이 없어 실제 Actions 결과와 브랜치 보호 필수 검사 설정 확인 불가 | BLOCKED | Git 저장소 및 GitHub 연결 후 정상 커밋과 격리된 실패 주입의 실제 job 결과를 확인하고, 저장소 설정에서 해당 검사를 필수로 지정해야 함. workflow 파일만으로 브랜치 보호를 설정할 수 없음 |
| 기존 검증 | `UV_OFFLINE=1 make check`: Ruff, 동기화 및 pytest 30건 통과 | PASS | [실행 로그](logs/make-check.log) |

최초 구현 후 lint가 `Callable` import 문제를 검출해 수정하고 전체 검증을 재실행했다. 실패 주입은 원본 프로젝트에 영향을 주지 않았다. 관련 명세와 README를 업데이트했다. `harness-verify`는 로컬 실패 시 `HARNESS_NOT_READY`를 출력하고 로컬 성공 시 외부 확인 대기로 표시한다. CLI·GitHub 검사는 별도이며 이 명령으로 `HARNESS_READY`를 자동 주장하지 않는다.

남은 BLOCKED 항목을 모두 실행해 결과를 기록한 뒤에만 최종 판정을 내릴 수 있다. 실패가 확인되면 `HARNESS_NOT_READY`, 모든 필수 항목이 PASS인 경우에만 `HARNESS_READY`다. v0.1 기능 구현은 아직 시작하지 않는다.


## GitHub Ruleset 검증 갱신 — 2026-11-04

- 저장소: https://github.com/CodingBot000/RCA_Agent; 기본 브랜치 `main`. 아래 결과는 이전 표의 GitHub 관련 BLOCKED 기록을 대체한다. CLI 인증 검증의 BLOCKED 상태는 그대로다.
- **PASS — 설정:** [main-ci-protection](https://github.com/CodingBot000/RCA_Agent/rules/26465287) (ID `26465287`)는 Active이며 기본 브랜치에 적용된다. PR 필수, 승인 0, GitHub Actions 앱(15368)의 `harness-verification` 필수, 최신 브랜치 요구 없음, 강제 푸시 및 삭제 금지, bypass 없음. GitHub가 기본 설정한 미기여 변경 추가 승인 옵션은 검증 후 비활성화했다.
- **PASS — 정상 CI:** 기존 [main 실행](https://github.com/CodingBot000/RCA_Agent/actions/runs/41661176656)은 성공. Ruleset 적용 후 임시 PR #2의 [성공 실행](https://github.com/CodingBot000/RCA_Agent/actions/runs/41673064529)도 성공했고 PR 상태는 `CLEAN`, `MERGEABLE`이었다.
- **PASS — 실패 및 차단:** 임시 PR #2에서 의도적인 실패를 포함한 [실행](https://github.com/CodingBot000/RCA_Agent/actions/runs/41673155644)이 실패했고 GitHub GraphQL의 `mergeStateStatus`는 `BLOCKED`였다. 병합 API 호출은 하지 않았다. `mergeable`은 충돌 여부를 나타내므로 `MERGEABLE`이었다. 테스트 후 PR은 병합 없이 종료하고 원격 브랜치를 삭제했다. `main` 커밋은 `4aa5caf` 그대로다.
- **남은 확인:** 강제 푸시·삭제 제한은 설정 및 적용 API로 확인했으며 실제 파괴적 시도는 하지 않았다. Codex/Claude CLI 지침 인식은 기존과 같이 BLOCKED이므로 전체 `HARNESS_READY`는 보류한다.
