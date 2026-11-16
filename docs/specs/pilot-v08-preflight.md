# v0.8 개발용 1건 최종 사전점검

기준선은 PR #16 병합 커밋 `913acf89aa642dddffba60757c2ba7a9d862922c`다. 사용자 요청에 따라 운영 제안값을 사전점검 설정으로 선택했다. 이는 API 키 사용·유료 실행 승인이 아니다. 이번 작업에서는 활성 승인 파일·credential FD·`--live`를 만들거나 실행하지 않는다.

## 수락 기준과 영향 범위

- evaluator-only 원본 mapping, official ID binding, 준비된 private provenance와 telemetry 파일 hash를 재검사한다. 공개 기록에는 case/source ID·원문·GT·비밀을 남기지 않는다.
- 선택 설정을 별도 JSON에 기록한다. 사례/전체 Budget은 동일하며 모델 토큰 비용 계획과 실제 청구를 구분한다. DNS/관측/수동 보존 설정도 기록하되 실행 승인으로 변환하지 않는다.
- 실제 API HTTP 없이 CLI dry-run, Mock E2E, OS DNS 회전/만료 및 실제 시스템 CA TLS handshake-only 검사를 실행한다. 공식 문서 확인과 계정/API 실증을 구분한다.
- HTTP 20초 설정이 transport의 숨은 기본 1초로 잘리지 않게 수정한다. Mock 연결이 받은 socket timeout과 전체 deadline을 검증하고 기존 timeout 음성 검증을 유지한다.
- 기존 macOS/컨테이너 격리·Budget·재시도 정책을 유지한다. `make check`, `make harness-verify` 결과와 READY/BLOCKED를 기록한다.

변경 계약은 Proxy의 명시적 timeout 전달과 사전점검용 설정·문서다. 유료 활성화 경로·모델·예산 한도 확장은 하지 않는다. timeout 회귀는 네트워크 없는 연결 대역으로 확인하고 실제 TLS Mock 오류 suite로 deadline 중단을 재검증한다.

## 선택 설정

`configs/pilot-preflight-v0.8.json`은 실행 승인 파일이 아니다. JSON에는 API key, private case/source identity, provenance digest 또는 활성화 bool이 없다. 원본 데이터와 실제 해시 검사는 호스트에서만 수행한다. 보존은 journal 청구 조정 후 90일, 결과 7일, 개발 telemetry 14일의 수동 처리이며 자동 삭제를 보장하지 않는다.

## 검증 결과

확인일: 2026-11-17(KST). 오프라인 사전점검은 READY이며 실제 유료 실행은 **LIVE_BLOCKED**를 유지한다. 이 기록의 READY는 계정/API 실행 성공이나 실제 RCA 정확도 판정이 아니다.

| 항목 | 판정 | 실제 근거/한계 |
| --- | --- | --- |
| 선택 설정/Budget | READY | JSON에서 Budget·ObservationLimits를 생성해 검증. 사례/전체 동일, 모델 8/count 8로 HTTP 최대 16회 계획 |
| 모델 비용 계획 | READY | 최대 입력 64,000 × USD 0.50/M + 출력 8,000 × USD 0.75/M = USD 0.038. 실제 토큰 수 계산이나 청구 보장이 아님 |
| CLI dry-run | READY | `DRY_RUN_VALIDATED`, 원본 독립성 true, 활성 승인/FD/`--live` 인수 없음 |
| 개발/평가 원본 독립성 | READY | evaluator selection 20개 원본 + official ID-map + 평가 blind 디렉터리 binding 검증, private provenance의 원본 제외/파일 SHA256/권한 검증 |
| 원본/세 신호 | READY | 고정 index SHA256과 일치하는 로컬 사본 2개 확인. 준비된 metrics/logs/traces가 모두 비어 있지 않음. 원본/개발 데이터 수정 없음 |
| 실제 telemetry TLS Mock E2E | READY | Agent→Broker→Worker→Proxy 격리 확인, 3개 신호, 모델 Mock 6/count 6, private artifact 0600. 기존 합성 Budget/가격·관측 fixture를 사용한 결과이며 선택한 실제 계정 비용·모델 정확도가 아님 |
| 실제 WAN IPv4 DNS/TLS | READY | DNS query 5초/lease 90초/TLS timeout 20초 조건에서 선택 주소 2개 TLS 1.3, 시스템 CA/호스트명 검증. 다른 IP·port EHOSTUNREACH. HTTP 0/keys false |
| 커널 주소 회전/만료 | READY | local IPv4/IPv6 Mock: 초기 허용·비허용 IP/port 차단, 새 주소 허용/이전 주소 차단, guardian 없는 만료 차단. 만료 IPv6 timeout은 단독 커널 REJECT 카운터 증가로 분리 검증했고 raw UNVERIFIED를 보존 |
| journal 재시작 | READY | 정상 2개 완료, 전송 직전/응답 직후 강제 종료 각각 MANUAL_REVIEW 1개. 세 경우 모두 재시작 모델 HTTP 0 |
| TLS/오류 회귀 | READY | Egress Mock suite 16개 PASS. 비정상 TLS chain/hostname/version, redirect, count 실패, 429/5xx, refusal, usage 누락, 응답 지연·느린 body 및 replay 음성 경로 확인 |
| 하네스 | READY(로컬) | `make check`와 `make harness-verify` 각각 321개 PASS/no skips. 전체 하네스 판정은 외부 검증 대기로 DEFERRED |
| API 키/활성 승인/유료 실행 | BLOCKED | 생성·열람·사용·활성화하지 않음. `--live` 실행 0, 실제 API HTTP 0 |
| 실제 API contract/계정/청구 | 미검증 | 계정 모델 접근·schema 수용·count 정확도·usage·rate limit·invoice·원격 취소는 실제 인증 요청 없이 검증 불가 |
| 실제 WAN IPv6 | 미검증 | 선택 정책은 ipv4-only. local IPv6 커널 검증을 실제 WAN dual-stack 검증으로 간주하지 않음 |

임시 Mock/테스트 journal과 결과는 검증 종료 시 정리했으며 운영 journal은 생성하지 않았다.

실행 환경은 Docker Desktop Engine 29.1.3 linux/arm64, Python 3.12.13, openai 3.31.0, pydantic 2.14.3, httpx 0.28.1, langgraph 1.2.20, pyarrow 22.0.0이다. timeout 수정 뒤 Egress 이미지는 `sha256:784d4bfb6ebd479f7f33d175996d0972e8e38859e0e731206251a30ef6626ed8`이며 host source hash와 이미지 label이 일치함을 확인했다. 임시 컨테이너·network·Volume은 0개다. 재사용 이미지만 남겼다.

## 공식 문서와 SDK 재확인

[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna) 문서에서 Responses·Structured Outputs와 최대 입력 922,000/출력 128,000을 확인했다. [공식 Standard 가격표](https://developers.openai.com/api/docs/pricing)는 기존 `2026-11-16` catalog snapshot과 일치한다. Short 입력/출력 USD 0.10/0.50, Long 입력/출력 0.20/0.75, Long 12h cache write 0.50(각 백만 토큰 기준)이며 기존 보수적 프로필을 유지했다. 실제 계정·지역·tier·추가 요금·count endpoint 과금은 미확인이다. 문서의 가격이 실제 invoice의 상한을 증명하지 않는다.

[Count API 사양](https://developers.openai.com/api/reference/resources/responses/subresources/input_tokens/methods/count)의 model/instructions/input/tools/text와 `response.input_tokens` 응답을 확인했다. 사용 중인 고정 SDK openai 3.31.0의 `responses.input_tokens.count` 인자와 일치한다. count에는 store/max_output_tokens/service_tier를 전달하지 않으며 Responses는 store=false, service_tier=default, retries=0을 유지한다. [Structured Outputs 규칙](https://developers.openai.com/api/docs/guides/structured-outputs)의 root object·required·additionalProperties 요구는 기존 Provider schema 회귀로 검사한다. 실제 서버 수용은 미검증이다. SDK 내부 parsing helper 의존성은 남아 있으므로 SDK 갱신 시 동일 schema/요청 회귀를 다시 실행해야 한다.

[데이터 정책](https://developers.openai.com/api/docs/guides/your-data)을 재확인했다. store=false는 ZDR 승인이 아니며 기본 abuse monitoring에 내용이 포함돼 통상 최대 30일(예외 가능) 보존될 수 있다. Prompt caching의 application state 및 실제 프로젝트 정책도 외부 전송 승인 전에 검토해야 한다. 이번에 선택한 로컬 7일/14일 수동 보존은 provider의 보존 정책을 변경하지 않는다.

## 실행한 검증과 재현 절차

아래는 유료 경로를 실행하지 않는 명령이다. 원본 case/source ID와 원문 telemetry는 출력하지 않는다. 개인 데이터 경로를 shell trace(`set -x`)나 공유 로그로 기록하지 않는다.

```sh
uv run --locked --no-sync python scripts/build_offline_image.py --egress
make check
make harness-verify
uv run --locked --no-sync python -m rca_agent.mock_egress --suite
uv run --locked --no-sync python -m rca_agent.dns_rotation_probe
uv run --locked --no-sync python -m rca_agent.journal_probe
```

CLI dry-run은 아래와 같이 manifest에서 디렉터리만 해석해 실행했다. 각 설정값은 선택 JSON과 비교했다. DNS/HTTP/session·관측·보존 설정은 모델 API 없이 별도 검증했으며 CLI dry-run이 이들 값을 실제 적용해 모델 요청했다는 의미가 아니다.

```sh
PILOT_CASE_DIR=$(uv run --locked --no-sync python - <<'PY'
import json
from pathlib import Path
root = Path('development-data/pilot-v1/cases')
print(root / json.loads((root / 'manifest.json').read_text())['directory'])
PY
)
uv run --locked --no-sync python -m rca_agent.pilot \
  --development-root development-data/pilot-v1/cases \
  --case-directory "$PILOT_CASE_DIR" \
  --development-manifest development-data/pilot-v1/cases/manifest.json \
  --provenance-file development-data/pilot-v1/private/provenance.json \
  --evaluation-mapping phase1-data/official-evaluator-private-v2/selection.json \
  --model gpt-6-luna --price-profile standard-global-conservative \
  --max-calls 8 --max-tool-calls 3 --max-iterations 3 \
  --max-input-tokens 64000 --max-output-tokens 8000 --max-total-tokens 72000 \
  --max-cost-usd 0.05 --output-tokens-per-call 1000 --timeout-seconds 30 \
  --max-concurrency 1 --max-retries 0
unset PILOT_CASE_DIR
```

WAN handshake-only는 기존 `wan_tls_probe.run_probe`를 ipv4-only/lease 90/TLS timeout 20으로 호출하고, DNS resolver callback은 `system_addresses(5)`로 제한했다. 이는 실제 HTTP 전송이나 credential FD를 사용하지 않는다. 운영 DNS refresh 20초는 LiveDeployment 설정·guardian 경로를 코드로 확인했으며 연속 실제 WAN guardian 실행/유료 세션은 이번 범위에서 실행하지 않았다. 주소 교체·만료의 OS 강제는 별도 local Mock probe로 실증했다.

## 실제 실행 전 승인 파일 준비 — 이번에는 미실행

1. 사용자는 준비된 개발용 1건의 외부 전송과 로그 마스킹 한계, OpenAI 프로젝트의 데이터 처리·보존 조건을 확인하고 별도로 유료 실행/API key 사용을 승인한다. 평가용 비공개 20건은 대상이 아니다. 모델·한도·DNS·관측·로컬 수동 보존값은 이번 요청에서 선택했으므로 다시 미정으로 되돌리지 않는다.
2. 계정의 gpt-6-luna 접근, 잔액·rate limit, global Standard tier 및 count/부가 요금을 사용자가 확인한다. 실제 schema/count/인증/청구의 첫 실증이 파일럿에서 이뤄짐을 수용하고, 불명확 과금은 자동 재시도 없이 수동 조정한다. USD 0.05 예약은 provider invoice/원격 취소의 보장이 아니다.
3. 승인 후에만 owner 0700 디렉터리에 owner 0600 JSON 파일을 만든다. 파일은 `approval.approved_profile`이 요구하는 정확한 키만 가진다. 5개 bool(`approved`, `allow_api_key_use`, `allow_provider_data_processing`, `allow_unverified_api_contract`, `retention_approved`)은 실제 별도 승인 후에만 true로 설정한다. 이번에는 파일을 만들지 않았다.
4. 바인딩은 최신 dry-run `settings_version`, `docker_integration.source_hash()`의 `source_version`, private provenance 바이트 SHA256의 `provenance_digest`, 실행 시점 이후의 유효기한 `expires`다. 해시는 비공개 파일 안에만 기록한다. 코드/설정/사례/파일 변경 시 다시 검사하고 이전 승인을 재사용하지 않는다.
5. `total_budget`은 선택 JSON의 `case_and_total_budget` 전체, `observation_limits`와 `retention`은 같은 JSON의 대응 값을 사용한다. `runtime`은 선택 JSON의 6개 필드에 고정 `journal_volume`과 절대경로 `private_output`을 추가한다. journal 이름은 `rca-pilot-journal-`로 시작하는 소문자/숫자/하이픈이어야 한다. private output 디렉터리는 owner 0700으로 준비한다. 이 두 경로/이름과 승인 만료 시점은 아직 정하지 않았고 활성 파일 생성 직전에 확정한다.
6. 동일 사례를 재시작할 때 journal volume을 바꾸지 않는다. SENT/MANUAL_REVIEW가 있으면 계정·청구·journal을 수동 확인한다. 새 journal 또는 새 request ID로 불명확 과금을 우회하지 않는다. 실제 journal을 자동 삭제하지 않는다.

## Credential FD 준비 — 별도 승인 후에만 수행

키를 `.env`에서 자동 읽거나 CLI 값·환경 변수·파일·로그에 복사하지 않는다. 이번 사전점검에서는 `.env` 또는 실제 API 키를 열람하지 않았고 credential FD도 만들지 않았다.

승인 후 신뢰된 로컬 secret injector 또는 비에코 터미널 입력(`getpass`)에서 키를 메모리에 받아, 길이 1~4096의 ASCII raw token으로 일회용 pipe에 전달한다. 신뢰된 launcher가 read FD(3 이상)만 파일럿 Python 프로세스에 상속한다. Python subprocess라면 `pass_fds=(read_fd,)`를 명시하고 인수에는 `--credential-fd <정수>`만 넣는다. 키 문자열을 subprocess 인수나 env에 넣지 않는다. 상속이 불확실한 shell/uv wrapper에 의존하지 말고 고정 `.venv/bin/python -m rca_agent.pilot`을 직접 실행한다.

부모는 미사용 read/write FD를 닫고 실행 후 키 참조와 메모리 pipe를 정리한다. OS 메모리/스왑의 완전 소거는 보장하지 않는다. 입력이 FD로 제공됐더라도 실제 launcher는 승인·provenance·OS DNS/TLS bootstrap 성공 뒤에만 이를 읽어 Proxy stdin에 전달한다. Agent/Broker/Worker에는 key를 전달하지 않는다. 실행 중 shell trace·core dump·프로세스 stdout 캡처로 키를 노출하지 않는다.

미래 유료 실행에는 기존 dry-run 인수 외에 `--approval-file`, `--credential-fd`, `--journal-volume`, `--private-output`, DNS 90/20/5초, HTTP 20초, session 300초, ipv4-only 및 명시적인 live 선택이 모두 필요하다. 실행 명령은 [기존 운영 절차](phase4b3d-operator-proposal.md)를 따른다. 이번에는 이 유료 명령을 구성해 실행하지 않는다.

## 수정과 리뷰

`egress_proxy.EgressProxy.forward`가 `min(생성자 기본 1초, 명시 HTTP timeout)`을 적용하던 결함을 재현했다. 20초 회귀는 수정 전 실패(실제 1초), 수정 후 PASS이며 0.25초처럼 더 짧은 명시 제한도 그대로 유지된다. 전체 전송 timer·Broker deadline·세션 종료·미확인 사용량 차단은 유지했다. 실제 API 동작 개선을 검증했다고 주장하지 않는다.

기존 macOS 정책·network=none 역할·호스트 allowlist·키/GT 마운트 경계를 변경하지 않았다. 이번 소스 변경은 HTTP timeout 전달에 한정된다. source hash가 바뀌므로 기존 이미지를 재사용하지 않고 다시 빌드했다. 설정 JSON과 문서는 활성 승인으로 해석되지 않는다. PR 생성·push·병합은 이번 요청에 포함하지 않았다.

마지막 남은 조건은 (1) 개발 1건의 외부 전송·API key 사용·유료 실행에 대한 별도 승인, (2) 계정의 모델 권한·global Standard 요금/부가 과금·잔액/한도·데이터 정책 확인, (3) private 결과 경로·동일 사례용 persistent journal 이름·승인 유효기한 확정, (4) 최신 code/settings/provenance에 결합한 private 승인 파일과 신뢰된 credential FD 준비다. 실제 인증/schema/count/usage/billing은 그 승인 뒤 첫 파일럿에서만 확인할 수 있다. 선택된 ipv4-only 실행에는 WAN IPv6 승인을 요구하지 않는다.
