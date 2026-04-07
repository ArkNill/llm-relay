# llm-relay-detect

> [English](README.md)

**Claude Code 세션 진단 도구** — 읽기 전용, 의존성 없는 코어.

로컬 Claude Code 세션 파일을 스캔해서 과도한 토큰 소비를 유발하는 알려진 버그를 탐지합니다. 데이터를 절대 수정하지 않습니다.

## 설치

```bash
pip install llm-relay-detect            # 의존성 없음, 일반 텍스트 출력
pip install 'llm-relay-detect[cli]'     # click + rich 포맷 출력
```

또는 [Releases](https://github.com/ArkNill/llm-relay-detect/releases)에서 독립 실행 파일을 다운로드하세요.

## 사용법

```bash
llm-relay-detect                        # 최근 10개 세션 스캔
llm-relay-detect scan --all             # 전체 세션 스캔
llm-relay-detect scan --last 20         # 최근 20개
llm-relay-detect scan --session 0348    # 특정 세션 (prefix 매칭)
llm-relay-detect scan --json            # JSON 출력
llm-relay-detect recover                # 최근 세션 컨텍스트 추출 (핸드오프 형식)
llm-relay-detect recover --format actions   # 구조화된 액션 목록
llm-relay-detect doctor                 # 7개 건강 검사 실행
llm-relay-detect doctor --fix           # 이슈 자동 수정 시도
```

## 탐지 항목

| 탐지기 | 대상 | 심각도 | 참조 |
|--------|------|--------|------|
| **가짜 Rate Limiter** | `<synthetic>` 엔트리 — 클라이언트가 생성한 가짜 제한 | CRITICAL | [#40584](https://github.com/anthropics/claude-code/issues/40584) |
| **컨텍스트 스트리핑** | 도구 결과가 `[Old tool result content cleared]`로 대체됨 | WARN/CRIT | [#42542](https://github.com/anthropics/claude-code/issues/42542) |
| **캐시 효율** | 낮은 캐시 읽기 비율 + 콜드 스타트 낭비 | INFO~CRIT | [#42906](https://github.com/anthropics/claude-code/issues/42906) |
| **로그 인플레이션** | PRELIM/FINAL 엔트리 중복 (로컬 통계 부풀림) | INFO/WARN | — |
| **Resume 손상** | 타임스탬프 역전, null 바이트, DAG 단절, 버전 혼재 | WARN | [#43044](https://github.com/anthropics/claude-code/issues/43044) |
| **FeatureFlags 플래그** | 서버사이드 기능 플래그 (도구 결과 예산, 도구별 한도) | INFO/WARN | [#42542](https://github.com/anthropics/claude-code/issues/42542) |
| **Orphan Tool Calls** | tool_use에 매칭 tool_result 없음 (또는 그 반대) | INFO/WARN | — |
| **Stuck Tool Calls** | 결과 없이 세션이 계속 진행된 도구 호출 | INFO/WARN | — |

## 설계 원칙

- **READ-ONLY** — 세션 파일을 절대 수정하지 않음
- **런타임 의존성 없음** — 코어는 stdlib만 사용 (Python 3.9+)
- **CLI 우선** — 터미널 사용자를 위한 도구
- **실행 가능한 권고** — 모든 발견 사항에 권고 + GitHub 이슈 링크 포함

## 배경

이 도구는 [claude-code-cache-analysis](https://github.com/ArkNill/claude-code-cache-analysis)의 연구 결과를 기반으로 합니다 — Claude Code의 6개 이상 버그를 프록시 수준 측정과 100개 이상 GitHub 이슈 분석을 통해 종합적으로 조사한 결과입니다.

## 라이선스

MIT
