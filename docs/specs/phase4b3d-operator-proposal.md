# Phase 4B-3D 운영 설정 제안 — 실행 미승인

v0.8 요청에서 아래 표의 운영값을 최종 사전점검 기준으로 선택했다. 모델·한도·DNS·관측·수동 보존값을 다시 미정으로 취급하지 않는다. 이 선택은 API 키 사용·외부 데이터 전송·유료 실행 승인이나 활성 승인 파일 생성을 허용하지 않는다. 현재 점검과 남은 조건은 [v0.8 사전점검 기록](pilot-v08-preflight.md)을 따른다.

아래 값은 실행 승인이 아니며 자동 적용하지 않는다. 실제 API/키 사용은 별도 사용자 승인 전까지 금지다. [공식 모델/요금 확인](phase4b3c-pricing.md)을 작업 중 다시 확인했으며 두 모델의 Responses/Structured Outputs 지원과 Standard 요금이 기존 기록과 일치했다. 실제 계정 접근·schema/count 동작은 미검증이다.

| 항목 | 첫 개발용 1건 제안 | 의미 |
| --- | --- | --- |
| 모델 | gpt-6-luna | gpt-6.1-terra는 별도 비용 승인 필요 |
| 처리/가격 | Standard global/default, conservative | cache read 할인 없이 Long/12h write 최대 요율로 모델 토큰 비용 예약 |
| 사례/전체 비용 | 각각 USD 0.05 | 다른 수수료·실제 청구 상한 보장은 아님 |
| 누적 input/output/total | 64,000 / 8,000 / 72,000 | 사례와 전체 동일 값 제안 |
| 모델 요청/도구/반복 | 8 / 3 / 3 | 구현상 세션 모델 요청 최대 8, 제안값이지 승인 값 아님 |
| 출력/호출 | 1,000 | reasoning 포함 출력 한도; 충분하지 않으면 실패 기록 |
| Broker timeout/HTTP timeout | 30초 / 20초 | count와 model을 포함한 Broker 시간 제한, 개별 네트워크 제한 |
| 전체 session | 300초 | timeout 시 로컬 역할 종료; 원격 취소 보장 아님 |
| concurrency/retries | 1 / 0 | 불명확 과금 자동 재시도 없음 |
| DNS lease/refresh/query timeout | 90초 / 20초 / 5초 | system resolver TTL은 알 수 없으므로 운영자가 승인한 최대 주소 유효 기간 |
| 주소 정책 | ipv4-only | IPv6 TCP/UDP는 거부. dual-stack은 해당 환경 TLS 검증 후 별도 선택 |
| observation records/chars | 5 / 12,000 | 각 관측 및 기존 전체 길이 검사 유지 |
| journal 보존 | 청구 조정 완료 후 90일 | 미확인 요청은 자동 삭제하지 않음 |
| 비공개 출력/개발 telemetry | 7일 / 14일 | 수동 삭제 제안. 자동 삭제는 구현하지 않았음 |
| 공개 보고 | 집계/버전/오류만 | raw telemetry·case/source ID·fingerprint·GT·키·원문 출력 제외 |

이 모델/토큰 제안의 보수적인 모델 비용 계획은 USD 0.038이다. 입력 토큰 계산 API 요금·계정/지역/tier·cache 정책·추가 요금은 실행 전에 확인해야 한다. `gpt-6.1-terra`에 같은 토큰을 적용하면 USD 0.38 계획으로 달라져 0.05 예산은 거부된다. 실제 비용 필드는 invoice/신뢰된 과금 확인 전 null이며 token cost upper estimate와 구분한다.

개발용 실제 telemetry는 `development-data/pilot-v1`에 준비했다. Source identity와 평가 20건의 불일치는 evaluator-only 매핑으로 확인했으며, 세 신호가 있는 서비스를 GT 없이 선택했다. 익명 ID 차이만으로 독립성을 판단하지 않았다. 사용자는 이 사례의 외부 전송, 로그 마스킹의 한계 및 provider 데이터 처리 조건을 승인해야 한다. 다른 사례 선택 시 provenance를 다시 생성·검증한다.

`store=false`는 Zero Data Retention을 의미하지 않는다. [공식 데이터 정책](https://developers.openai.com/api/docs/guides/your-data)에 따르면 기본 abuse monitoring 로그에 고객 내용이 포함될 수 있으며 통상 최대 30일 보존된다(법률/보호 예외 가능). ZDR/Modified Abuse Monitoring은 별도 자격·승인이 필요하다. 실제 프로젝트 설정과 조직 정책을 확인한 뒤 `allow_provider_data_processing`을 승인해야 한다.

## 승인 파일

승인 파일은 사용자가 명시적으로 승인한 뒤에만 호스트의 owner-only 0700 디렉터리/0600 파일로 만든다. 이 작업에서는 활성 승인 파일이나 API 키를 생성하지 않는다. 필수 필드는 `approval.approved_profile`의 schema와 CLI help에 있다.

- 승인 bool: approved, allow_api_key_use, allow_provider_data_processing, allow_unverified_api_contract, retention_approved.
- 바인딩: dry-run의 settings_version, 현재 harness_source_version, 비공개 provenance 파일 SHA256, 승인 유효기한(expires).
- runtime: journal_volume, dns_lease, dns_refresh, dns_timeout, http_timeout, session_timeout, family_policy, private_output.
- total_budget: 모든 Budget 필드. observation_limits: max_records/max_chars.
- retention: journal_days_after_reconciliation, artifact_days, development_days, delete_mode(manual만 지원).

모든 값은 사용자가 확정한다. 파일의 `approved`를 true로 바꾸는 것은 단순 readiness 검사가 아니라 유료 실행에 대한 명시적 운영 승인이다. 템플릿이나 이 제안만으로 승인을 추정하지 않는다. 기존 승인 파일도 code/settings/provenance/기한/runtime이 달라지면 거부된다. persistent journal 이름은 같은 사례의 재시작에서 유지해야 하며 불명확 요청을 우회하려고 새 journal로 바꾸면 안 된다.

## 실행 절차

1. `pilot --help`로 필수 Budget 및 live 인수를 확인하고, 개발 manifest/provenance와 evaluator mapping을 지정해 dry-run한다. 결과는 실제 분석이 아니다.
2. 계정 모델 접근·가격·data processing·key 사용·한도·수동 조정 및 보존 정책을 사용자에게 승인받는다.
3. 승인 파일을 비공개로 만들고 API key는 secret injector의 raw-token FD(3 이상)로 전달한다. CLI 인수 값·환경·이미지·Git에 key를 넣지 않는다. `.env`는 자동 읽지 않는다.
4. 아래 명령의 placeholder를 승인 값으로 대체하고 `--live`를 명시한다. 누락된 승인/FD/provenance/환경 검증은 LIVE_BLOCKED다. WAN DNS/lease/OS/TLS bootstrap이 실패하면 key FD도 읽지 않는다.
5. 실행 후 성공·실패 모두 보고하며 불명확 상태는 journal 수동 확인 후에만 다룬다. 자동 원격 취소·청구 취소는 제공하지 않는다.

```sh
uv run --locked --no-sync python -m rca_agent.pilot \
  --development-root <private-cases-root> --case-directory <approved-case-directory> \
  --development-manifest <manifest.json> --provenance-file <private-provenance.json> \
  --evaluation-mapping <evaluator-only-mapping.json> \
  --model <approved-model> --price-profile standard-global-conservative \
  --max-calls <n> --max-tool-calls <n> --max-iterations <n> \
  --max-input-tokens <n> --max-output-tokens <n> --max-total-tokens <n> \
  --max-cost-usd <usd> --output-tokens-per-call <n> --timeout-seconds <seconds> \
  --max-concurrency 1 --max-retries 0 \
  --approval-file <private-approval.json> --credential-fd <integer-fd> \
  --journal-volume <approved-persistent-journal> --private-output <private-output-directory> \
  --dns-lease-seconds <seconds> --dns-refresh-seconds <seconds> --dns-timeout-seconds <seconds> \
  --http-timeout-seconds <seconds> --session-timeout-seconds <seconds> \
  --address-family-policy <ipv4-only-or-dual-stack> --live
```

FD는 3 이상의 정수로, 실제 명령에는 `--credential-fd 3`처럼 지정한다. secret FD의 준비와 유료 명령 실행은 승인 이후 작업이며 이 예시를 현재 실행하지 않는다. API의 최초 schema/count/인증/청구 동작은 아직 미검증이므로 실패와 미확인 청구 가능성을 수용하는 개발용 1건 파일럿으로만 승인한다. 평가용 20건의 실모델 분석은 포함하지 않는다.
