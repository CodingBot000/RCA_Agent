# RCA Agent — v0.0 harness

Python 3.12 기반 오프라인 계약 하네스. 제품 기능이나 RCA 알고리즘은 포함하지 않는다. [v0.1 명세](docs/specs/rca-v0.1.md)와 [평가 격리 정책](docs/evaluation-isolation.md)을 먼저 읽는다.

## 설치 및 검증

```sh
uv sync --locked --group dev   # 최초 의존성 설치에는 네트워크가 필요할 수 있음
make check                     # lint, format, 스킬 동기화, 전체 테스트
make test                      # 전체 pytest
UV_OFFLINE=1 make harness-verify # 필수 로컬 검증, skip 발생 시 실패
UV_OFFLINE=1 make check        # 설치 완료 후 네트워크 없이 실행
```

테스트는 가짜 입력만 사용한다. CI 역시 의존성을 설치한 뒤 `UV_OFFLINE=1 make check`를 실행하며 API 키나 모델 호출은 필요 없다. CI의 액션 다운로드와 의존성 설치에는 네트워크가 필요하다.

## 에이전트 지침과 스킬

Codex는 프로젝트의 `AGENTS.md`를 읽고 Claude Code는 `CLAUDE.md`의 `@AGENTS.md` 가져오기를 사용한다. 새 Claude Code 세션을 프로젝트 루트에서 시작하고 `/memory`에서 로드된 파일을 확인한 뒤 작업 순서를 물어 실제 반영 여부를 확인한다. Codex는 새 세션에서 같은 질문을 하거나 `codex --ask-for-approval never exec "Summarize the project workflow from AGENTS.md"`로 확인한다. 설치 버전과 세션 구성에 따라 로딩 결과가 다를 수 있으므로 실제 세션에서 확인한다. 자동 지침은 보안 통제가 아니다.

`skills/`가 원본이며 `.codex/skills/`, `.claude/skills/`는 생성된 복사본이다. 원본을 수정한 뒤 `uv run python scripts/sync_skills.py --write`를 실행한다. `make check`가 누락과 변경 불일치를 거부한다. Claude에 같은 이름의 내장 스킬이 있을 수 있으므로 목록과 실제 호출 대상을 확인한다.

## 선택적 수동 평가

`make eval`은 안내만 출력하며 데이터를 다운로드하거나 비용을 발생시키지 않는다. 선택적 `uv sync --locked --extra eval`은 PyPI의 RCAEval 1.7.0만 설치한다. 확인된 1.7.0 휠에는 현재 upstream 문서의 `rcaeval-agent` CLI가 없으므로 이 설치만으로 blind export와 채점을 실행할 수 없다. 실제 평가 전에는 [upstream 가이드](https://github.com/phamquiluan/RCAEval/blob/main/docs/AGENTS.md)의 해당 CLI가 포함된 버전과 의존성을 별도 평가 환경에 설치하고 버전 및 라이선스를 확인해야 한다. 그 환경에서 사용자가 데이터 이용과 저장 위치를 결정한 후 아래 명령을 직접 실행한다. 데이터가 없으면 export가 다운로드한다.

```sh
rcaeval-agent export --dataset re2-ob --out agent_tasks --copy
# 격리된 에이전트가 생성한 예측을 평가자에게 전달한 후
rcaeval-agent evaluate --tasks agent_tasks/re2-ob --predictions preds.jsonl
```

작업 디렉터리만 격리해 제공한다. 실제 모델 호출은 비용이 발생할 수 있으며 여기에는 실행기가 없다. RCAEval 결과와 내부 계약 사이의 변환기 역시 미구현이다.

참고: [Codex AGENTS.md](https://developers.openai.com/codex/guides/agents-md/), [Claude Code 메모리](https://code.claude.com/docs/en/memory), [Claude Code 스킬](https://code.claude.com/docs/en/skills), [RCAEval 에이전트 가이드](https://github.com/phamquiluan/RCAEval/blob/main/docs/AGENTS.md).

`make harness-verify`는 지침 가져오기·순서와 모든 로컬 검사를 확인한다. 에이전트 CLI 실제 인식과 GitHub 원격 실행은 별도 확인이며, 로컬 명령 성공만으로 전체 준비 완료를 뜻하지 않는다. 상세 기준과 판정은 `docs/self-verification/` 및 `reports/harness-verification.md`를 참고한다.

## Phase 1: RE2-OB 데이터 준비

공개된 seed 42 표본은 과거 이력으로만 유지한다. 새 실행은 고정 데이터 리비전과 평가자 전용 임의 256-bit 키를 이용해 공개된 과거 20건을 제외한 새 표본을 만든다. 아래 두 명령은 평가자만 실행한다. 첫 명령은 명시적인 네트워크 다운로드로 인덱스와 선택한 20개 사례의 telemetry를 받는다. 기존 대상이 있으면 덮어쓰지 않는다. 용량과 데이터 보관·삭제는 실행자 책임이다.

```sh
uv sync --locked --extra phase1
uv run --locked --extra phase1 python -m rca_agent.phase1 download phase1-data/re2-ob-private-v2
uv run --locked --extra phase1 python -m rca_agent.phase1 export phase1-data/re2-ob-private-v2/private/raw phase1-data/blind-private-v2
```

`phase1-data/`는 Git 제외 대상이다. `private/selection.key`, `private/mapping.json`, 인덱스와 원본은 정답을 포함하거나 재구성하므로 에이전트가 접근할 수 없게 별도로 보관한다. `blind`에는 익명 케이스 디렉터리와 기존 `RCAInput` 형태의 `task.json`이 있다. 각 케이스의 Parquet 파일을 pyarrow로 읽을 수 있다. 이는 RCAEval 공식 agent export 및 채점기와 호환됨을 아직 검증하지 않은 자체 형식이다. 경로 또는 이름 변경은 보안 경계가 아니다.

실제 에이전트를 실행하기 전에는 별도의 컨테이너/OS 권한 경계가 필수다. 예를 들어 신뢰할 수 있는 로컬 이미지에 blind 디렉터리만 `--mount type=bind,src=<blind 절대경로>,dst=/task,readonly`로 마운트하고 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`를 적용한다. 원본·정답·평가자 전용 매핑·과거 공개 표본 manifest·부모 디렉터리·환경 변수 및 Docker 소켓은 제공하지 않는다. 공식 blind export 내부의 익명 manifest는 공식 도구 사용에 필요하다. 실행 환경에서 실제 접근 거부를 직접 확인한다. 공개 인덱스와 선택 규칙을 결합하면 답을 유추할 수 있으므로 네트워크 차단도 중요하다. 출력은 비밀 누출을 완전히 방지하거나 의미적 추론을 검증하지 못한다.

재현 가능한 격리 실행은 신뢰할 수 있는 로컬 이미지를 사용해 `scripts/run_blind_container.sh phase1-data/blind-private-v2 <local-image> <command> [args...]`로 시작한다. 실행기는 익명 케이스 구성만 검사하고, blind 디렉터리 외의 호스트 경로·환경 변수·네트워크를 명시적으로 전달하지 않는다. Docker 런타임과 이미지 자체의 신뢰성 및 telemetry에 포함된 의미적 단서는 별도로 검토해야 한다.

## 공식 RCAEval export와 채점기 연결

Phase 1의 v2 `blind` 형식과 공식 RCAEval 형식은 별도로 내보낸다. 공식 도구와 연결하려면 평가자 측에서 [검증한 RCAEval 소스](https://github.com/phamquiluan/RCAEval/tree/90b63a46ac1e969f1fa9015318b51e455c62f8c4) 커밋 `90b63a46ac1e969f1fa9015318b51e455c62f8c4`을 별도 checkout하고, Python 3.12 환경에 `numpy`, `pandas`, `scikit-learn`, `networkx`, `requests`, `tqdm`을 설치한다. 공식 소스 또는 환경 설치는 수동 작업이다. PyPI RCAEval 1.7.0 휠의 CLI에 의존하지 않는다. 정확한 환경 재현이 필요하면 위 의존성의 버전도 평가 실행 기록에 남겨야 한다.

아래 경로는 예시이며 공식 소스는 수정되지 않은 해당 커밋이어야 한다. 기존 출력은 덮어쓰지 않는다. 실행은 로컬 데이터를 사용하고 LLM을 호출하지 않는다.

```sh
uv run --locked --extra phase1 python -m rca_agent.rcaeval_compat export \
  --private phase1-data/re2-ob-private-v2/private \
  --blind phase1-data/official-blind-private-v2 --evaluator phase1-data/official-evaluator-private-v2 \
  --official-source /path/to/RCAEval --official-python /path/to/venv/bin/python

# 에이전트에는 official-blind만 읽기 전용으로 제공한다.
uv run --locked --extra phase1 python -m rca_agent.rcaeval_compat input \
  --blind phase1-data/official-blind-private-v2 --case-id "$CASE_ID"

# 결과는 기존 RCAResult 형식 JSONL. ID 체계를 명시해야 한다.
uv run --locked --extra phase1 python -m rca_agent.rcaeval_compat score \
  --blind phase1-data/official-blind-private-v2 --evaluator phase1-data/official-evaluator-private-v2 \
  --results /path/to/results.jsonl --id-scheme official \
  --official-source /path/to/RCAEval --official-python /path/to/venv/bin/python
```

`CASE_ID`는 공식 blind manifest에서 읽은 v2 익명 ID다. 기존 공개 표본의 4자리 ID는 기본 검증과 컨테이너 실행에서 거부된다. `input`은 공식 task의 서비스 및 정규화된 지표 후보를 합친 `RCAInput`을 반환하며 공식 ID를 사용한다. 같은 비공개 v2 표본의 Phase 1 ID로 생성된 결과를 채점할 때만 `--id-scheme phase1`을 사용한다. 각 결과의 후보 및 evidence ID는 해당 공식 task에서 만들어진 입력과 대조한다. 현재 입력의 `telemetry-metadata`는 데이터 존재에 대한 구조적 근거일 뿐 특정 원인의 실질적 근거가 아니다. 제출 근거의 의미적 타당성과 RCA 분석은 구현·검증되지 않았다.

`official-evaluator`의 answers, `selection.json`, ID 매핑과 채점 결과는 정답이 포함된 평가자 전용 자료다. 컨테이너에는 `official-blind` 디렉터리만 전달한다. `scripts/run_blind_container.sh`는 공식 blind 형식도 검증하고 마운트한다. 자동 변환은 raw 로그와 트레이스의 CSV를 포함하지만 원본의 집계 로그·트레이스 시계열(`logts`, `tracets`)은 만들지 않는다. 공식 원본 CSV와 값이 완전히 같다는 뜻도 아니다. 실패한 export는 일부 디렉터리를 남길 수 있으며 별도 출력 경로에서 다시 실행한다.

실제 20건으로 모델 호출 없이 공식 exporter, toolkit 및 scorer를 재검증하려면 공식 환경에서 다음 명령을 실행한다. 합성 결과를 만드는 데 정답을 사용하므로 이 명령은 평가자 측에서만 실행하며 성능 측정으로 해석하지 않는다.

```sh
PYTHONPATH="$PWD/src" /path/to/venv/bin/python scripts/verify_rcaeval_compat.py \
  --blind phase1-data/official-blind-private-v2 --evaluator phase1-data/official-evaluator-private-v2 \
  --official-source /path/to/RCAEval --official-python /path/to/venv/bin/python
```

### Git 공개 범위

원본·blind telemetry, 정답과 평가자 전용 매핑, 임시 채점 결과는 Git에 추가하지 않는다. `.gitignore`는 기본 데이터 디렉터리와 자주 쓰는 파일명·확장자를 제외한다. 사용자 지정 경로와 임의의 결과 파일명까지 모두 판별할 수는 없으므로 커밋 전에 `git status --short`와 `git diff --cached --name-only`를 확인한다. 기존에 Git에서 추적된 파일은 `.gitignore`를 추가해도 추적이 자동으로 중단되지 않는다.

과거 seed 42 표본은 공개 선정 규칙과 인덱스를 결합하면 정답을 재구성할 수 있다. 새 표본도 보존된 telemetry 값을 공개 데이터와 대조하면 역추적될 수 있으므로 공개 저장소나 네트워크를 평가 에이전트에 제공하지 않는다. 이미 공개된 Git 기록을 `.gitignore`로 비공개화할 수는 없다.

선택 키(`selection.key`)는 평가자만 읽을 수 있게 보관한다. 키를 잃으면 동일한 비공개 표본과 ID를 재구성할 수 없다. 별도 키를 지정한다면 암호학적으로 안전하게 생성한 32바이트 키여야 한다. 새 경로로 같은 표본을 다시 다운로드하려면 `download <새-경로> --key <평가자-전용-selection.key>`를 사용한다. 변환은 알려진 시각·UUID·IPv4·이메일·긴 16진수/숫자 패턴 및 trace ID를 가리지만, 모든 민감정보나 원본 telemetry 지문 역매칭을 막지 못한다. 메트릭 수치는 보존되므로 공개 데이터에 접근하거나 사전 지식이 있는 에이전트의 완전한 블라인드는 보장되지 않는다.

## Phase 2 조회 및 근거

`rca_agent.telemetry.TelemetryTools`는 승인된 Phase 1 blind Parquet 디렉터리와 사례별 `EvidenceRegistry`를 받아 메트릭·로그·트레이스를 조회한다. 범위는 메트릭의 사례 시간 범위 안에서 `[start, end)`이며 호출당 결과는 1~100건이다. 성공·결과 없음·자료 부재·오류를 구분하고 성공 결과만 근거로 등록한다. `finalize`는 조회 근거가 없으면 `insufficient_evidence`를 반환한다. 공식 RCAEval 채점기와 Phase 2 결과의 호환성은 미검증이다.

서비스 이름은 각 데이터 파일의 값을 정확하게 비교한다. 실제 RE2-OB의 trace 서비스에 `frontendservice`가 존재하지만 메트릭 후보에는 `frontend`가 존재하므로 두 이름을 자동으로 동일시하지 않는다. 제공된 사례의 메트릭 시간 범위 밖 데이터는 조회하지 않는다. 이 도구의 경로 검사는 OS 보안 경계가 아니며 blind만 제공하는 읽기 전용 컨테이너가 필요하다.

## Phase 3 오프라인 그래프

`rca_agent.graph.run_agent`에 사례별 `TelemetryTools`, 교체 가능한 `Reasoner`, 실행자가 명시한 `Limits(max_tool_calls, max_iterations)`와 `ObservationLimits(max_records, max_chars)`를 전달한다. `ScriptedReasoner`는 테스트용 질의·가설을 실행하며 `$latest`를 현재 실행에서 성공한 최신 근거로 바꾼다. Phase 4A에서는 Reasoner가 CaseContext와 제한된 실제 Observation을 받고 가설에 실제 전달된 필드·값을 명시해야 한다. ScriptedReasoner는 명시적 claim의 `$latest`와 `$observed`를 치환한다. 관측값 일치는 인과관계를 증명하지 않는다. 실제 모델 사용이나 정확도 측정이 아니다. 반환값은 조사 이력·호출 횟수·가설·결과 또는 실패 envelope를 포함한다. 외부 tracing은 그래프 실행 중 비활성화된다.

직접 `EvidenceRegistry.register(status="ok")`를 호출하는 기존 사용법은 거부된다. 실제 조회로 등록된 근거만 그래프에서 사용하며, 현재 실행에서 관측한 성공 호출과 별도로 대조한다. Python Reasoner는 신뢰된 in-process 코드여야 한다. 이 검사는 임의 Python에 대한 샌드박스가 아니므로 정답 격리 및 네트워크 차단은 별도의 실행 환경으로 강제한다. 실제 모델 평가에 필요한 호출·비용 정책은 미정이다.

## Phase 4B-1: 오프라인 모델 인터페이스

`LLMReasoner`는 `ModelBroker` protocol을 사용하며 기본 제공 구현은 외부 API를 호출하지 않는 `FakeProvider`와 `OfflineBroker`다. 공급자·모델·코드 버전, 가격 및 `Budget`의 모든 항목을 명시해야 한다. 그래프의 `Limits`는 broker의 도구 호출·반복 한도 이하여야 한다. 모델 출력은 엄격한 JSON 스키마, 허용 후보 및 Phase 4A 근거 검증을 통과해야 한다.

입력 토큰 상한과 출력 예약량은 보수적인 신뢰된 계산을 전제한다. 예외·시간 초과로 사용량이 불명확하면 더 이상 호출하지 않는다. `OfflineBroker`의 타임아웃은 호출자에게 중단을 알리지만 Python 스레드 자체를 강제 종료하지 못한다. 동시에 실행되는 실제 요청이나 청구가 끝났다는 의미가 아니다. 동시 Agent 실행과 요청은 공유 broker 인스턴스에서 제한하지만, 예산도 그 인스턴스를 공유하는 실행에 누적된다. 실행별 독립 예산은 별도 인스턴스를 사용한다. 공유 프로세스 밖의 전역 예산 제한은 제공하지 않는다. 실제 외부 API 연결은 구현되어 있지 않으며, 독립된 OS 권한·네트워크 경계와 인증된 Broker, 제공자별 사용량·요금 및 취소 보장이 검증되기 전에는 연결하지 않는다.

`run_agent`의 내부 상태에는 telemetry가 들어갈 수 있으므로 공개 로그로 직렬화하지 않는다. `rca_agent.public_report.public_run_report`는 모델·버전·호출·토큰·비용·실패 사유만 allowlist로 내보낸다. 사용한 식별자와 보관 정책도 공개 전 검토한다. 프롬프트 구분과 JSON Schema는 prompt injection의 완전한 방어가 아니다.

## Phase 4B-2 격리된 Fake IPC 시험

`rca_agent.ipc_runner.run_fixture`는 macOS `sandbox-exec`에서 합성 blind fixture만 사용하는 Agent·Fake Broker를 별도 프로세스로 실행한다. 신뢰된 실행기가 익명 stdin/stdout 파이프를 중계하며 OS 정책은 Agent와 Broker의 네트워크·쓰기·프로세스 생성을 차단한다. 프레임·메시지·요청·사례·시간 한도, 사례별 Budget, 평가 전체 Budget, ObservationLimits와 Fake 가격을 각각 명시한다. 사용 예시는 `tests/test_ipc_runner.py`다.

```sh
UV_OFFLINE=1 uv run --locked pytest -q tests/test_ipc_runner.py tests/test_ipc_protocol.py
```

이 fixture는 실모델 API, 상시 서버 또는 운영용 Broker가 아니다. Agent·Broker는 하나씩 실행되고 세션 내에서만 replay를 제거한다. Linux 등은 격리 없이 실행하지 않고 실패한다. macOS의 접근 제한은 테스트한 경로와 환경에 대한 결과이며 임의 위치의 모든 비밀에 대한 완전한 파일시스템 allowlist가 아니다. 실제 외부 API에 필요한 최소 권한 격리, 인증·네트워크 정책, timeout 후 외부 청구 정산과 재시작 시 중복 방지는 별도 구현·검증이 필요하다. IPC 메시지에는 telemetry가 포함될 수 있으므로 출력이나 공개 로그에 기록하지 않는다.

## Phase 4B-3A 오프라인 Provider 시험

`OpenAIProvider`는 공식 SDK와 Mock HTTP transport로 Responses 요청과 사용량 집계를 시험한다. 실제 키를 읽지 않으며 일반 네트워크 transport를 거부한다. 실제 API 접근, 입력 토큰 상한의 보수성, 실모델 스키마 호환성 및 OS 네트워크·파일 격리는 검증되지 않았다. 이 구성은 운영용 Broker가 아니며 Phase 4B-3B의 격리 검증 없이 실데이터에 사용하지 않는다.

Phase 4B-3B-1은 진행 중이다. `uv run --locked --no-sync python scripts/docker_agent_probe.py`는 로컬에 이미 존재하는 `python:3.12.13-slim` 이미지를 사용해 합성 Agent 컨테이너의 기초 접근 제한을 시험한다. Broker·Proxy 네트워크 격리와 IPC E2E는 아직 구현·검증되지 않았으며 실데이터나 실제 API에 사용할 수 없다. 세부 상태는 `docs/specs/phase4b3b1-progress.md`를 참조한다.

Phase 4B-3B-1A의 Broker/Mock 검사는 `uv run --locked --no-sync python -m rca_agent.docker_probe`로 실행한다. 두 컨테이너의 internal 네트워크 통신 및 파일 미마운트를 검사하며, 접근 가능한 비허용 경로나 미검증 경로가 있으면 `BLOCKED`와 종료 코드 2를 반환한다. 로컬 Docker 실험에서 Mock 통신은 성공했지만 gateway와 비허용 포트 경로가 발견됐다. 세부 결과는 `docs/specs/phase4b3b1a-progress.md`에 기록했다. Docker 명령은 수동 실행하며 일반 회귀 테스트와 CI는 Docker 없이 오프라인으로 실행한다.

Gateway 추가 검사는 `uv run --locked --no-sync python -m rca_agent.gateway_probe`로 실행한다. 실제 합성 수신 서비스에 대한 일반 internal 대조군과 isolated gateway 네트워크를 비교한다. 기존 Gateway 경로 차단과 Mock 통신을 확인했지만 비허용 포트 접근은 계속 BLOCKED다. 근거와 검증 한계는 `docs/specs/phase4b3b-gateway-progress.md`에 기록했다.

Unix Socket 후속 검사는 `uv run --locked --no-sync python -m rca_agent.uds_probe`로 실행한다. Broker와 합성 Worker는 network none 상태에서 두 컨테이너만 공유하는 임시 Volume의 소켓으로 통신한다. Agent에는 Volume을 제공하지 않는다. 합성 UDS 통신·TCP 접속 실패·잘못된 메시지 거부를 검사하며 결과와 남은 인증 경계는 `docs/specs/phase4b3b-uds-progress.md`에 기록했다. 실제 외부 Worker와 전체 실모델 보안 검증은 포함하지 않는다.

## Phase 4B-3B 통합 오프라인 실행

현재 통합 실행은 실제 LangGraph/LLMReasoner, 사례별·전체 Budget, 입력 토큰 계산 Mock, 인증 UDS Worker 및 Mock HTTP 응답을 연결한다. Agent/Broker/Worker 모두 TCP 네트워크가 없고, Agent에는 승인된 한 사례만, Broker에는 읽기 전용 UDS Volume만 제공한다. 서로 다른 UID와 SO_PEERCRED, 세션 HMAC, 소켓 소유권/권한 및 재전송 캐시를 검증한다. 위의 단계별 Probe는 이 통합의 이전 실험 기록으로 유지한다.

```sh
# 명시적 수동 빌드: 잠금 파일의 Linux 의존성을 설치한다.
uv run --locked --no-sync python scripts/build_offline_image.py
# 정상 Synthetic RCA 흐름
uv run --locked --no-sync python -m rca_agent.docker_integration
# 인증·소켓 위장·예산·Worker 장애를 포함한 13개 오프라인 시나리오
uv run --locked --no-sync python -m rca_agent.docker_integration --suite
```

이미지가 현재 코드와 다르면 재빌드를 요구한다. 공개 결과에는 계수와 고정 상태만 출력하며 사례 키·telemetry·인증값은 출력하지 않는다. 실행 후 컨테이너와 임시 Volume을 제거한다. 실제 모델/외부 Worker는 `LIVE_BLOCKED`이며 활성화할 수 없다. [통합 진행 기록](docs/specs/phase4b3b-progress.md)에 실행 근거, 역사적 Probe 결과, 남은 외부 네트워크/TLS 조건 및 별도 승인이 필요한 첫 유료 1건 파일럿 준비 절차를 정리했다. Fake 결과는 RCA 정확도 평가가 아니다.


## Phase 4B-3C 로컬 TLS Egress와 파일럿 준비

Agent/Broker/Worker의 network none을 유지하고 Worker만 별도 인증 UDS로 Proxy에 요청한다. Proxy netns의 iptables는 로컬 TLS Mock IP/포트만 허용한다. hostname·경로·TLS·redirect 검사는 애플리케이션 계층에서 추가한다. 실제 OpenAI 목적지의 운영 allowlist는 검증되지 않아 LIVE_BLOCKED다.

```sh
# 명시적 수동 빌드: 잠금 패키지 및 승인된 iptables 설치
uv run --locked --no-sync python scripts/build_offline_image.py --egress
uv run --locked --no-sync python -m rca_agent.mock_egress --suite
uv run --locked --no-sync python -m rca_agent.pilot --help
```

파일럿 CLI는 기본 dry-run으로 개발 manifest/평가셋 분리, 명시된 모든 한도와 가격 프로필을 검증한다. `--live`는 호출 없이 차단한다. 실제 유료 backend·자격 증명은 활성화하지 않았다. 보고서의 미실행 비용·정확도는 null이며 Fake 결과를 성능으로 표현하지 않는다. [3C 진행 기록](docs/specs/phase4b3c-progress.md)에 실제 Docker 근거, [공식 가격 확인](docs/specs/phase4b3c-pricing.md), 보고 schema 및 첫 개발용 1건의 기술적·사용자 결정 조건을 정리했다.


## Phase 4B-3D 개발용 1건 Readiness

영속 request journal과 원본 identity 기반 개발/평가 분리, 세 신호 telemetry 실행을 추가했다. DNS/IP 집합은 시간 제한 lease로 교체하고 Proxy netns에서 이전 주소와 만료된 주소를 차단한다. 실제 OpenAI에는 IPv4 DNS/TLS handshake만 검증했고 HTTP/API key를 사용하지 않았다. 현재 유료 실행은 LIVE_BLOCKED다.

```sh
uv run --locked --no-sync python -m rca_agent.journal_probe
uv run --locked --no-sync python -m rca_agent.dns_rotation_probe
uv run --locked --no-sync python -m rca_agent.pilot --help
```

`pilot`은 기본 dry-run이며 개발 manifest뿐 아니라 evaluator-only provenance를 검사할 수 있다. `--live`는 provenance·명시적 private approval·모든 운영 설정·credential FD가 있어야 환경을 준비하고, 실제 OS/TLS 검증 뒤에만 Proxy에 key를 전달한다. `.env` 자동 사용이나 API key 생성은 없다. 이 작업에서 승인 파일을 활성화하거나 유료 경로를 실행하지 않았다. [3D 기록](docs/specs/phase4b3d-progress.md)과 [미승인 운영 제안](docs/specs/phase4b3d-operator-proposal.md)에 근거·실행 절차·필요 결정을 정리했다. 실제 journal은 자동 삭제하지 않으며 불명확 요청은 수동 확인한다.

v0.8 최종 사전점검은 [선택 설정](configs/pilot-preflight-v0.8.json)과 [READY/BLOCKED 기록·승인/FD 준비 절차](docs/specs/pilot-v08-preflight.md)를 따른다. 설정 선택은 유료 실행 승인이 아니며 API key·활성 승인 파일·`--live` 없이 검증한다.

## v0.9 사용자 직접 실행

기본 `python -m rca_agent.local_pilot`은 키 없는 점검이다. `.venv/bin/python -m rca_agent.local_pilot --run`은 사용자가 Codex 밖의 독립 로컬 터미널에서만 실행한다. 전송·보존·예산·미검증 API/청구 조건을 읽고 최종 문구를 입력한 뒤 저장소 루트 `.env`의 `OPENAI_API_KEY`를 읽는다. 키 재입력은 없다. 승인 전에는 키를 읽거나 API 요청을 하지 않고, 실제 키는 일회용 pipe FD로 Proxy에만 전달한다. [실행 절차·검증·잔여 위험](docs/specs/pilot-v09-local-launcher.md)과 [.env 키 로딩 계약](docs/specs/pilot-dotenv-key.md)을 먼저 확인한다. `.env`는 Git 제외·소유자 전용 0600이어야 하며 셸 실행·변수 확장·프로세스 환경변수 등록은 하지 않는다.

실패/중단 후 명령을 반복하거나 private attempt/journal을 지워 새 요청으로 우회하지 않는다. `python -m rca_agent.local_pilot --status`로 동일 journal의 집계만 읽고 수동 확인한다. 이번 구현 작업에서는 실제 키·운영 승인·유료 요청을 사용하지 않았고 LIVE_BLOCKED를 유지했다.

Journal 누락 조사는 `uv run --locked --no-sync python scripts/audit_pilot_attempt.py`로 읽기 전용 수행한다. `--record`는 별도 owner-only 진단 receipt만 저장하며 복구를 승인하거나 실행하지 않는다. [조사 근거·수동 복구 설계](docs/specs/pilot-journal-investigation.md)를 따른다. 과거 binding·코드 경계가 입증되지 않거나 journal이 상충하면 BLOCKED를 유지한다. 누락 journal의 건수를 0으로 간주하지 않는다.
