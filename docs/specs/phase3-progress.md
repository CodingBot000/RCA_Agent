# Phase 3 진행 기록

- SDD 및 영향 분석: 진행 중. Phase 2 registry는 공개 `register`로 임의 성공을 표시할 수 있으므로 그래프는 호출 응답과 registry의 교차 검증이 필요하다.
- 테스트 설계, 그래프 구현, 검증 및 리뷰: 미완료.
- LangGraph는 Python 3.12 호환 1.2.20으로 고정한다. 테스트의 예산 값은 운영 정책이 아니다.
- 2026-11-11 구현: StateGraph의 조사·가설·근거검증·순위·실패 노드, 실행별 명시적 호출/반복 제한과 실제 조회 관측 기록 검증을 작성했다. EvidenceRegistry의 직접 등록은 내부 capability를 요구한다. Python 프로세스 자체는 보안 샌드박스가 아니며 신뢰할 수 있는 Reasoner 주입이 전제다.
- 오프라인 synthetic fixture: 정상·재조사·위조 근거·잘못된 도구·호출/반복 한도·외부 tracing 비활성화 검증을 수행했다. `make check` 69건 통과. 실제 LLM 및 정답 데이터는 사용하지 않았다.
- 영향: Phase 2 외부 직접 `register(status="ok")`는 거부된다. 동일 사례의 실제 조회에서 등록한 근거는 기존 RCAResult 계약과 호환된다. 공식 채점 및 실제 정확도는 미검증이다.
- 2026-11-11 회귀·리뷰: 성공 조회 관측과 연결되지 않은 Registry 레코드, 이전 실행 근거, 데이터 부재, 잘못된 서비스, 잘못된 도구, 호출·반복 한도를 synthetic fixture로 검증했다. 외부 tracing이 활성화된 환경에서도 그래프 실행 중 명시적으로 비활성화한다. `make harness-verify` 74건 통과.
- 잔여 범위: Python Reasoner는 신뢰된 in-process 코드이며 임의 Python에 대한 샌드박스가 아니다. Ground Truth 격리는 기존 컨테이너 설정에 의존한다. 실모델 호출·실제 RCA 정확도·공식 점수는 검증하지 않았다. 실모델 평가 진입 전 모델·비용·호출·보관 정책 및 실제 격리를 결정하고 검증해야 한다.
- GitHub Actions: PR #9의 [push 검증](https://github.com/CodingBot000/RCA_Agent/actions/runs/42863259286)과 [PR 검증](https://github.com/CodingBot000/RCA_Agent/actions/runs/42863303035)이 성공했다. 이 기록의 후속 커밋에도 필수 CI가 다시 실행되어야 한다.
