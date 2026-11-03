##  Harness Self-Verification 및 완료 판정 자동화

v0.0 Harness Bootstrap 구현이 끝나면 반드시 자체 검증을 실행한다.

### A. 자동 검증 체계

- `make harness-verify` 명령을 제공한다.
- 모든 필수 계약·보안·회귀 검증을 실행한다.
- 정상 입력과 잘못된 입력을 모두 테스트한다.
- 테스트가 의도대로 실패하는지 Negative Test를 포함한다.
- 정답 데이터 격리 및 Evidence ID 유효성 검증을 포함한다.
- 검증 스크립트 자체에도 테스트를 작성한다.

### B. Codex 전용 개발 환경 검증

- AGENTS.md와 CLAUDE.md의 핵심 규칙 일관성을 검사한다.
- Codex CLI의 지침 인식을 실제 실행 결과로 확인한다. 실행할 수 없다면 BLOCKED로 기록한다.
- Claude Code 실행은 v0.0 완료 기준에서 제외(N/A)한다. CLAUDE.md, 공통 스킬 및 관련 호환성 테스트는 유지한다. 실제 Claude 동작은 별도로 검증하지 않는 한 미검증이다.

### C. CI 검증

- GitHub Actions에 Harness Verification 작업을 추가한다.
- API 키 없이 실행 가능한 검증을 필수 검사로 설정한다.
- 정상 코드에서는 CI가 성공해야 한다.
- 테스트 실패를 의도적으로 발생시켜 CI가 실패하는지 확인한다.
- 검증 이후 의도적인 오류는 반드시 원상 복구한다.
- 실제 GitHub Actions 실행 결과를 확인할 수 없으면 BLOCKED로 기록한다.

### D. 검증 결과 보고서

`reports/harness-verification.md`를 생성한다.

각 완료 조건에 대해 다음 항목을 기록한다.

- 검증 대상
- 검증 명령
- 실제 실행 결과
- 증거 파일 및 실행 로그
- PASS / FAIL / BLOCKED / N/A
- 실패 원인 및 남은 작업

### E. 최종 완료 판정

- Codex 전용 개발 환경의 모든 필수 조건이 PASS일 때만 `HARNESS_READY (Codex-only)`를 선언한다. N/A 항목은 필수 조건에 포함하지 않는다.
- 하나라도 FAIL이면 `HARNESS_NOT_READY`로 처리한다.
- 실행하지 못한 검증이 있다면 BLOCKED로 기록하고 전체 완료 판정을 보류한다.
- 검증 결과를 임의로 생성하거나 테스트를 건너뛰어 PASS 처리하지 않는다.
- 실패한 항목은 원인 분석 → 수정 → 재검증을 수행한다.
- 실제 RCA 분석 정확도와 OS 수준 정답 데이터 격리는 별도 검증 대상이며, 하네스 완료가 이를 증명하지 않는다.
- v0.1 기능 개발은 별도의 사용자 결정 및 승인 전에는 시작하지 않는다.