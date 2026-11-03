# v0.1 SDD 조사·리뷰 기록

2026-11-04. 이 작업은 문서와 설계만 수행하며 기능, 그래프, 도구, 실데이터 다운로드, 모델 호출을 수행하지 않았다. 기존 v0.0 계약·테스트·설정과 공식 RCAEval agent 문서, Hugging Face 데이터 카드, LangGraph 문서를 검토했다. 공식 데이터셋 뷰어 API `splits`와 `rows`를 조회해 `cases/train` 735행을 집계했다. RE2-OB 90건의 메타데이터에는 logs/traces가 있다고 표시되지만 실제 파일의 접근·완전성은 검증하지 않았다. 정답이 담긴 행은 저장소에 저장하거나 에이전트 입력으로 사용하지 않았다.

사용자 결정: RE2-OB 단일 데이터셋 20건, 장애 유형별 가능한 균등 배분; 도구 호출·비용 한도는 나중에 결정. 후속 결정으로 seed=42, 6개 장애 유형 각 3~4건 및 서비스 편중 최소화를 승인했다. 원본 라벨과 경로는 평가자에게만 제공한다.

리뷰: 기존 `RCAResult.parse`는 입력 evidence ID만 허용하므로 도구에서 얻은 evidence를 포함한 별도 최종 검증 입력을 명세했다. 실제 도구 API와 실패 envelope는 신규 설계이며 아직 테스트·구현되지 않았다. 기존 보안 검사는 문자열·경로 보조 검사여서 OS 격리 검증을 별도 필수 조건으로 구분했다. Top-k의 분모와 미제출·실패를 구분하고 malformed 파일에 대한 upstream 거부를 명시했다. 측정되지 않은 품질 목표와 비용 상한은 확정하지 않았다. 기존 RCAEval 휠의 CLI 불일치와 Parquet/export 호환성은 수동 평가 전 해결해야 한다.

자료: [RCAEval agent 가이드](https://github.com/phamquiluan/RCAEval/blob/main/docs/AGENTS.md), [RCAEval 데이터 카드](https://huggingface.co/datasets/phamquiluan/RCAEval), [LangGraph 개요](https://docs.langchain.com/oss/python/langgraph/overview). 최신 upstream 문서는 변할 수 있으므로 실제 실행 전 리비전을 고정한다.

## Phase 1 업데이트

공식 고정 리비전 인덱스의 RE2-OB는 6개 장애 유형별 15건이다. seed 42와 고정 해시 순서 기반 선택은 각 3/3/3/3/4/4건, 5개 서비스에 분산되는 것을 인덱스로 확인했다. 공개 manifest는 선택 알고리즘과 익명 ID만 포함한다. 선택 규칙과 공개 인덱스만으로 매핑을 재구성할 수 있으므로 agent에는 manifest와 네트워크를 제공하지 않는다. 실제 호출·비용 한도와 모델은 미정이고 Phase 1에는 LLM 호출 코드가 없다.

### 실제 Phase 1 확인 — 2026-11-04

명시적 `uv run --extra phase1 python -m rca_agent.phase1 download phase1-data/re2-ob-20`으로 고정 리비전 인덱스와 선택한 20건의 각 네 파일만 내려받았다. `export`로 익명 디렉터리에 복사한 뒤 실제 Parquet 60개에 대해 footer와 비어 있지 않은 행 수를 읽고 20개의 `task.json`을 기존 `RCAInput.parse`로 검증했다. 파일 전체 값이나 upstream 원본 대비 체크섬 및 공식 RCAEval 점수 일치는 검증하지 않았다.

실제 blind 디렉터리만 읽기 전용 bind mount한 로컬 Docker 컨테이너(`--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, node:22-alpine)에서 task 읽기 가능, 알려진 호스트 정답 경로 부재, task 및 루트 파일시스템 쓰기 실패를 확인했다. 이는 해당 컨테이너 구성의 구체적인 접근 확인이며 범용 보안 보증, 텔레메트리의 의미적 정답 누출 방지, 공개 데이터 역매칭 차단 또는 다른 에이전트 환경의 검증을 뜻하지 않는다. 생성된 원본과 정답은 Git 제외 위치에 남는다. 실패한 다운로드·내보내기는 부분 디렉터리를 남길 수 있어 검토 후 사용자가 별도 대상으로 재실행한다.

오프라인 테스트는 mock 기반 데이터 선정·정확히 20건 다운로드 요청·기존 계약 변환·심볼릭 링크 거부를 포함한다. 이 테스트는 실제 네트워크나 유료 호출을 수행하지 않는다. 실제 LLM 연결은 없고 모델명·공급자 구성은 이후 구현 단계에서 주입 가능하도록 설계만 하며, 이 단계에서 기본 모델이나 호출을 설정하지 않는다.
