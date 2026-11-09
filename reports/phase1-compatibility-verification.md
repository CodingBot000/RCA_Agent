# Phase 1 RCAEval 호환성 및 무결성 검증

검증일: 2026-11-08. 이 문서는 검증 결과이며 RCA Agent의 실제 분석 성능을 의미하지 않는다. 원본 정답·케이스 매핑은 평가자 전용 위치에 두고 이 보고서에는 기록하지 않았다. 별도 명시 요청 없는 LLM 호출이나 모델 기반 평가는 하지 않았다.

## 대상과 근거

- Phase 1 코드: `main`의 `src/rca_agent/phase1.py`, RE2-OB 20건과 `manifests/re2-ob-20.json`.
- 데이터: Hugging Face [RCAEval](https://huggingface.co/datasets/phamquiluan/RCAEval) 고정 리비전 `afeacb11bcc94dadfd1c8f483ee4377b2b8b614e`.
- 공식 코드: [RCAEval](https://github.com/phamquiluan/RCAEval/tree/90b63a46ac1e969f1fa9015318b51e455c62f8c4), Git commit `90b63a46ac1e969f1fa9015318b51e455c62f8c4`. 별도 임시 Python 3.12 환경에서 소스의 `RCAEval.agent`를 직접 실행했다. 이는 기존 PyPI 1.7.0 휠이 해당 CLI를 제공한다는 뜻이 아니다.

## 데이터 무결성 — PASS (검사한 고정 리비전 기준)

고정 리비전의 Hugging Face 파일 메타데이터를 API로 조회하고 평가자 전용 매핑을 사용해 20건 × 네 파일을 확인했다.

- 80/80개 원본 파일이 upstream의 크기 및 해시와 일치했다. LFS 파일은 API에 명시된 SHA-256, 일반 Git 파일은 Git blob SHA-1을 비교했다. 인덱스 `cases.parquet`도 같은 리비전에서 직접 받은 바이트와 SHA-256이 일치했다.
- 원본 80/80개와 blind 복사본의 SHA-256이 일치했다. 로컬 복사 과정에서 바이트가 변하지 않았음을 뜻한다.
- Parquet 60/60개를 row batch로 끝까지 디코딩했고 총 11,202,540행을 읽었다. footer의 행 수와 읽은 행 수가 일치했다. 의미적 정확성이나 원래 CSV와 변환 이전 값의 동등성까지 증명하지 않는다.
- 고정 인덱스에서 현행 선택 함수를 다시 실행했을 때 선택된 20개의 순서와 평가자 전용 매핑이 일치했다. 공개 manifest와 선택 알고리즘을 공개 인덱스에 대조하면 정답을 재구성할 수 있다. 따라서 manifest, 인덱스와 네트워크를 에이전트에 제공하지 않는다.

## 공식 blind export 호환성 — FAIL

다음은 공식 exporter를 직접 실행하거나 공식 코드와 실제 20건의 스키마를 비교한 결과다.

1. `PYTHONPATH=<공식 소스> python -m RCAEval.agent export --dataset re2-ob --out <임시경로> --data-root phase1-data/re2-ob-20/private/raw --copy`는 `FileNotFoundError: no cases (data.csv or simple_metrics.csv)`로 실패했다. 현행 원본은 Parquet이며 공식 exporter는 `data.csv` 또는 `simple_metrics.csv`를 찾는다.
2. 현행 blind 루트에는 공식 exporter가 제공하는 `manifest.json`과 `cases/<case-id>/` 구조가 없고 각 case의 task JSON 필드도 다르다. 공식 채점기 `run(..., tasks_dir=<phase1 blind>, answers_path=<올바른 공식 answers>)`는 `manifest.json`을 찾지 못해 실패했다.
3. 평가자 영역의 20건 metrics Parquet를 **실험용 CSV로 변환한 fixture**를 사용해 공식 exporter를 실행하면 공식 형식의 20건과 별도 answers 파일을 만들 수 있었다. 이 실험은 upstream 원본 CSV를 재현한 검증이나 로그·트레이스 변환 검증이 아니다.
4. 위 fixture에서 공식 exporter가 새로 부여한 익명 ID는 현행 Phase 1 ID와 20/20건에서 달랐다. 이 결과는 이 20건 fixture에 대한 것이며 다른 표본에 일반화하지 않는다. 번호 문자열이 같은 ID라도 같은 사건을 뜻한다고 가정하거나 다른 매핑을 결합하면 잘못된 정답과 점수가 연결될 수 있다. 평가자 전용 명시 매핑과 일대일 검증이 필요하다.
5. 20/20건에서 현행 후보 집합과 공식 `candidate_indicators` 집합이 달랐다. 현행 입력에는 20건 모두 `_latency-50`, `_latency-90`이 들어가지만 공식 exporter는 p50을 제외하고 p90을 `_latency`로 바꾼다. 공식 task에는 별도의 `candidate_services`가 있으나 현행 후보 목록에는 별도 서비스명 후보가 없다. 의미·단위·필터링이 같은지를 추가로 검증할 필요가 있다.

## 공식 채점기 — 제한적 동작 확인, Phase 1 연결은 FAIL

위 공식 exporter가 만든 **임시 fixture와 공식 answers**를 사용했다. 예측은 실제 에이전트 결과가 아니라 테스트를 위해 평가자 정답으로 만든 oracle이다.

- 20건의 정답 서비스만 순위 1로 제출했을 때 서비스 AC@1과 AC@3은 각각 1.00이었다. indicator 점수는 서비스만 제출해 0.00이었다. 이것은 채점기의 양성 대조 실험으로, RCA 정확도를 측정한 결과가 아니다.
- 1건을 빼면 coverage 19/20, 서비스 AC@1 0.95로 누락이 miss로 계산됐다. 잘못된 JSON, 중복 case ID, 알 수 없는 case ID는 공식 `PredictionsError`로 거부됐다.
- 현행 Phase 1 출력에는 이 공식 answers에 대응하는 파일·ID·후보 변환기가 없으므로 실제 Phase 1 → 공식 채점기의 종단 간 점수 동등성은 확인하지 못했다. 공식 채점기의 서비스 순위는 동일 서비스의 중복 지표를 제거한 후 AC@k를 계산하므로, 단순 후보 순위에서 상위 k개를 자르는 자체 계산과 다를 수 있다.
- 공식 코드는 정답과 후보를 분리하지만, 이 검증은 실험용 fixture로 exporter·채점기 동작을 확인한 것이다. 실제 운영 격리나 의미적 정답 누출 방지의 전면 검증은 아니다.

## 리뷰와 다음 조치

현행 Phase 1의 무결성은 이 고정 리비전에 대해 확인됐으나 **공식 blind export 및 채점기와 직접 호환되지 않는다**. 수정 구현 없이 완료라고 판정하지 않는다. 다음 작업으로 평가자 전용 일대일 ID 매핑, 공식 후보 정규화 및 서비스 후보, 공식 manifest/answers 생성, Parquet에서 공식 도구가 기대하는 신호 형식으로의 변환, 최소 권한 실행 및 20건의 평가 결과 parity 테스트가 필요하다. 20건을 정답 oracle로만 평가해 정확도 목표나 분석 성능을 결정하지 않는다.

## 수정 및 종단 간 재검증 — 2026-11-08

위 FAIL은 수정 전의 기록이다. 평가자 전용 호환 계층은 SHA-256을 고정한 인덱스와 원래 매핑을 검증하고, 20건의 세 Parquet를 임시 CSV로 스트리밍 변환해 고정 커밋의 **공식 exporter**를 실행한다. 공식 `manifest.json`, `cases/`, metrics/logs/traces CSV는 blind 출력에 두고, 원본 사례·정답 및 Phase 1 ID ↔ 공식 ID 매핑은 별도 평가자 디렉터리에 둔다. 채점 전 매핑과 정답의 source를 일대일로 대조해 ID 두 개를 바꾸는 변조도 거부한다. 기존 RCAInput에는 공식 `candidate_services`와 정규화된 `candidate_indicators`를 합쳐 제공하고, 기존 RCAResult의 후보·evidence 검사를 통과한 JSONL을 공식 scorer에 넘긴다. `phase1`과 `official` ID 체계는 명시적으로 선택한다.

- 실제 보유한 20건 전체를 변환하여 공식 exporter를 실행하고, 각 공식 task에 대해 toolkit의 `describe_case`와 `list_metrics`를 확인했다. 별도로 1건의 `logs()` 161,224행과 `traces()` 383,583행을 공식 toolkit으로 읽었다. 공식 변환은 CSV 입력과 20건 부분집합을 이용한 것이며 upstream의 원래 CSV export와 바이트 동일함을 의미하지 않는다.
- 20건 메트릭 전체의 Parquet↔CSV 값을 부동소수점 허용 오차 `1e-12`로 비교했고, 로그와 트레이스는 각각 한 건의 모든 행을 문자열 표현으로 비교했다. 다른 19건의 로그·트레이스 값 전체, 공식 원본 CSV와의 동등성, 누락된 logts/tracets 집계 데이터는 검증하지 않았다.
- 정답으로 만든 합성 제어 예측에서 Phase 1 ID 및 공식 ID 경로 모두 공식 evaluator의 서비스와 지표 AC@1/AC@3와 일치했다. 서비스 순위를 섞은 제어 예측은 AC@1=0.50, AC@3=0.75, 지표 정답을 이용한 별도 양성 대조는 지표 AC@1=1.00이었다. 이는 연동 시험이며 RCA 분석 정확도가 아니다. 재검증 스크립트는 `scripts/verify_rcaeval_compat.py`다.
- 공식 blind만 읽기 전용·네트워크 차단 컨테이너에 마운트해 task 읽기와 쓰기 거부, 정답 영역 부재를 확인했다. 텔레메트리 자체의 정답 단서나 공개 데이터 역매칭의 완전한 차단은 보장하지 않는다.
- 오프라인 회귀는 정규화된 후보, ID 스왑, 유효하지 않은 후보·근거, 가짜 ID, 파일 추가·심볼릭 링크, 모의 exporter→scorer 흐름을 다룬다. `make harness-verify` 전체 테스트 46개가 통과했다. 공식 scorer/toolkit 실행은 선택적 수동 통합 검사이며 기본 CI는 네트워크·실데이터 없이 모의 객체로 동작한다.

제약: 정답과 매핑, 합성 예측은 Git에서 제외되는 평가자 영역에 보관한다. 실제 모델·RCA 품질 및 증거의 의미적 타당성은 미검증이다. `telemetry-metadata`는 자료 존재만 표현하며 근본 원인을 실질적으로 뒷받침하는 근거가 아니다.
