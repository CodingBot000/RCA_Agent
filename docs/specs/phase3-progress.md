# Phase 3 진행 기록

- SDD 및 영향 분석: 진행 중. Phase 2 registry는 공개 `register`로 임의 성공을 표시할 수 있으므로 그래프는 호출 응답과 registry의 교차 검증이 필요하다.
- 테스트 설계, 그래프 구현, 검증 및 리뷰: 미완료.
- LangGraph는 Python 3.12 호환 1.2.20으로 고정한다. 테스트의 예산 값은 운영 정책이 아니다.
- 2026-11-11 구현: StateGraph의 조사·가설·근거검증·순위·실패 노드, 실행별 명시적 호출/반복 제한과 실제 조회 관측 기록 검증을 작성했다. EvidenceRegistry의 직접 등록은 내부 capability를 요구한다. Python 프로세스 자체는 보안 샌드박스가 아니며 신뢰할 수 있는 Reasoner 주입이 전제다.
- 오프라인 synthetic fixture: 정상·재조사·위조 근거·잘못된 도구·호출/반복 한도·외부 tracing 비활성화 검증을 수행했다. `make check` 69건 통과. 실제 LLM 및 정답 데이터는 사용하지 않았다.
- 영향: Phase 2 외부 직접 `register(status="ok")`는 거부된다. 동일 사례의 실제 조회에서 등록한 근거는 기존 RCAResult 계약과 호환된다. 공식 채점 및 실제 정확도는 미검증이다.
