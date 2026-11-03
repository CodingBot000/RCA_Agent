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

`manifests/re2-ob-20.json`의 고정 리비전과 seed 42를 이용한다. 아래 두 명령은 평가자만 실행한다. 첫 명령은 명시적인 네트워크 다운로드로 인덱스와 선택한 20개 사례의 telemetry를 받는다. 기존 대상이 있으면 덮어쓰지 않는다. 용량과 데이터 보관·삭제는 실행자 책임이다.

```sh
uv sync --locked --extra phase1
uv run --locked --extra phase1 python -m rca_agent.phase1 download phase1-data/re2-ob-20
uv run --locked --extra phase1 python -m rca_agent.phase1 export phase1-data/re2-ob-20/private/raw phase1-data/blind
```

`phase1-data/`는 Git 제외 대상이다. `private/mapping.json`, 인덱스와 원본은 정답을 포함하므로 에이전트가 접근할 수 없게 별도로 보관한다. `blind`에는 익명 케이스 디렉터리와 기존 `RCAInput` 형태의 `task.json`이 있다. 각 케이스의 Parquet 파일을 pyarrow로 읽을 수 있다. 이는 RCAEval 공식 agent export 및 채점기와 호환됨을 아직 검증하지 않은 자체 형식이다. 경로 또는 이름 변경은 보안 경계가 아니다.

실제 에이전트를 실행하기 전에는 별도의 컨테이너/OS 권한 경계가 필수다. 예를 들어 신뢰할 수 있는 로컬 이미지에 blind 디렉터리만 `--mount type=bind,src=<blind 절대경로>,dst=/task,readonly`로 마운트하고 `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`를 적용한다. 원본·정답·manifest·부모 디렉터리·환경 변수 및 Docker 소켓은 제공하지 않는다. 실행 환경에서 실제 접근 거부를 직접 확인한다. 공개 인덱스와 선택 규칙을 결합하면 답을 유추할 수 있으므로 네트워크 차단도 중요하다. 출력은 비밀 누출을 완전히 방지하거나 의미적 추론을 검증하지 못한다.

재현 가능한 격리 실행은 신뢰할 수 있는 로컬 이미지를 사용해 `scripts/run_blind_container.sh phase1-data/blind <local-image> <command> [args...]`로 시작한다. 실행기는 익명 케이스 구성만 검사하고, blind 디렉터리 외의 호스트 경로·환경 변수·네트워크를 명시적으로 전달하지 않는다. Docker 런타임과 이미지 자체의 신뢰성 및 telemetry에 포함된 의미적 단서는 별도로 검토해야 한다.
