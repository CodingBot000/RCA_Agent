# Phase 4B-3C 공식 모델·가격 확인

확인일: 2026-11-16. [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra)의 공식 문서에서 Responses와 Structured Outputs 지원을 확인했다. 실제 계정 접근·API 수용은 미검증이다.

[공식 가격표](https://developers.openai.com/api/docs/pricing)의 Standard/global 기준, USD/백만 토큰:

| 모델 | Short 입력/출력 | Long 입력/출력 | Short 12h cache write | Long 12h cache write |
| --- | --- | --- | --- | --- |
| gpt-6-luna | 0.10 / 0.50 | 0.20 / 0.75 | 0.25 | 0.50 |
| gpt-6.1-terra | 1.00 / 5.00 | 2.00 / 7.50 | 2.50 | 5.00 |

Long context는 입력 272K 초과다. 두 모델의 문서상 max input은 922,000, max output은 128,000이다. 지역/서비스 tier에 따라 가격이 달라지므로 위 자료를 다른 프로필에 적용하지 않는다. 파일럿 CLI는 명시적으로 `standard-global-conservative` 프로필을 선택해야 하며 cached-read 할인 없이 Long/12h write를 포함한 최대 입력 요율과 Long 출력 요율로 선언된 누적 토큰의 보수적인 모델 비용을 계산한다.

이는 모델·프로필 선택과 Budget의 사용자 승인을 대신하지 않는다. 실제 캐시 정책·토큰 계산 API 과금·추가 요금·계정 정책 및 실행일 가격은 실제 파일럿 전에 재확인해야 한다. 현재 dry-run은 모델 토큰 비용과 요청 개수의 사전 계획만 검증하며 실제 청구 상한이나 취소를 보장하지 않는다. 테스트용 가격은 운영 가격으로 사용하지 않는다.
