# Phase 4A 진행 기록

- SDD 및 영향 분석: 완료. 기존 그래프는 evidence ID만 전달했고 실제 레코드와 사례 범위를 전달하지 않았다. 사용자는 실행별 관측 글자·건수 제한을 필수로 지정하도록 결정했다.
- 구현·synthetic 테스트·20건 격리 smoke·로컬 리뷰: 완료. 원격 CI는 PR 실행 후 확인한다. 실제 LLM 및 정답은 사용하지 않았다.
- 계약 설계: Reasoner는 사례 ID·허용 후보와 서비스·시간 범위를 담은 CaseContext, 각 성공 호출의 case·출처·call ID·evidence ID·조회 인수·제한된 레코드 및 잘림 표시가 있는 Observation을 받는다. 미관측 값·타 사례·실패 호출을 참조하는 가설은 거부한다. 구조적 값 일치가 인과적 설명의 정확성을 뜻하지 않는다.
- 기존 도구의 `QueryResponse.records`는 실제 데이터지만 그래프는 이전에 이를 버렸다. Registry 원본과 응답의 연결·해시를 확인한 뒤 복사한 관측만 Reasoner에 전달해야 한다. 격리 실행은 아래 smoke 기록을 따른다.

- 사용자 결정: `ObservationLimits(max_records, max_chars)`를 매 실행에 지정한다. `max_records`는 기존 조회당 승인 한도 100 이하이며 `max_chars`는 레코드 JSON 문자열의 Unicode 문자 수다. 부분 결과는 잘림 표시하고 첫 레코드도 담을 수 없으면 실패한다. 숫자는 테스트 예시이며 운영 정책이 아니다.
- 구현: Reasoner가 CaseContext와 Observation을 읽고, 가설마다 실제 전달된 필드·값을 인용해야 한다. 기존 RCAResult에는 검증 후 해당 주장 메타데이터를 제외한다. `$latest`와 `$observed`도 명시된 claim에서만 해석되며 ID만 제출한 가설은 거부한다. 구조적 일치는 인과적 뒷받침 검증이 아니다. 기존 `run_agent` 호출자는 ObservationLimits를 추가로 전달해야 한다.
- Synthetic 테스트: 관측값 변화에 따른 후보 변경, 미전달 레코드·허위 값·ID만 제시한 가설·변조 응답·크기 초과를 확인했다. 오프라인 `make check` 및 `make harness-verify` 83건 통과. 실제 모델이나 RCA 정확도는 평가하지 않았다.
- 실데이터 smoke: `scripts/phase4a_smoke.py`를 Python 3.12.13, LangGraph 1.2.20 환경의 macOS `sandbox-exec`에서 실행했다. 네트워크와 파일 쓰기를 거부하고 사용자 디렉터리는 기본적으로 읽기 거부한 뒤 프로젝트 소스·의존성과 v2 blind 경로만 허용했다. 별도 사전 검사에서 비공개 키 읽기, blind 쓰기, 네트워크 연결이 거부됨을 확인했다. 결과: 20건 중 성공 20, 실패 0, 미검증 0. CPU 메트릭 한 건을 조회하는 연결 시험이며 모든 텔레메트리 종류·분석 정확도를 뜻하지 않는다. Ground Truth는 제공하지 않았다.
- 리뷰 및 남은 위험: 관측 레코드에 실제와 같은 필드·값이 있다는 구조적 확인은 설명의 인과적 타당성을 증명하지 않는다. Reasoner는 신뢰된 in-process 코드다. 공개 원본의 telemetry 지문 역매칭 및 패턴 기반 마스킹 한계가 남는다. Phase 4B 전 모델·비용·호출·보관 정책과 실제 격리를 결정하고 검증해야 한다.
- GitHub Actions: [PR 검증](https://github.com/CodingBot000/RCA_Agent/actions/runs/42867524643)과 [push 검증](https://github.com/CodingBot000/RCA_Agent/actions/runs/42867513065)이 성공했다. 이 기록을 반영한 후속 커밋에서도 필수 CI를 확인한다.
