# llm-relay

LLM 사용 통합 관리 — API 프록시 + 세션 진단 + 멀티 CLI 오케스트레이션

[English](README.md) | [llms.txt](llms.txt)

## 왜 만들었나

특정 AI 코딩 도구에 대한 깊은 벤더 의존성에서 벗어나기 위해 시작한 프로젝트입니다. [Claude Code의 숨겨진 동작을 조사](https://github.com/ArkNill/claude-code-hidden-problem-analysis)하면서 — 무음 토큰 인플레이션, 거짓 속도 제한, 컨텍스트 무단 삭제, 불투명한 피처 플래그 — 단일 벤더의 블랙박스에 의존하는 것이 위험하다는 것을 확인했습니다. llm-relay는 가시성과 통제권을 되찾기 위해 만들어졌습니다: 실제로 무슨 일이 일어나는지 모니터링하고, 문제를 독립적으로 진단하며, 여러 CLI 도구(Claude Code, Codex, Gemini)를 오케스트레이션해서 단일 장애점을 없애는 것이 목표입니다.

## 기능

- **Proxy**: API 투명 프록시 — 캐시/토큰 모니터링 + 12전략 pruning
- **Detect**: 7종 디텍터 (orphan, stuck, synthetic, bloat, cache, resume, microcompact)
- **Recover**: 세션 복구 + doctor (7개 건강 검사)
- **Guard**: 4-tier 임계값 데몬 — dual-zone(절대+비율) 분류
- **Cost**: per-1% 비용 산출 + rate-limit 헤더 분석
- **Orch**: 멀티 CLI 오케스트레이션 (Claude Code, Codex CLI, Gemini CLI)
- **Display**: 멀티 CLI 세션 모니터 — provider 배지 + 프로세스 생존 감지
- **MCP**: stdio 전송 7개 도구 (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns)

## 설치

```bash
# CLI 전용 (진단, 복구, 오케스트레이션)
pip install llm-relay

# 프록시 + 웹 대시보드
pip install llm-relay[proxy]

# MCP 서버 (Python 3.10 이상)
pip install llm-relay[mcp]

# 전부
pip install llm-relay[all]
```

## 빠른 시작

### CLI 진단 (서버 불필요)

```bash
llm-relay scan              # 세션 건강 검사 (7종 디텍터)
llm-relay doctor            # 설정 건강 검사 (7개 항목)
llm-relay recover           # 세션 컨텍스트 추출 (재개용)
```

### 웹 대시보드

```bash
# 방법 1: 직접 실행
pip install llm-relay[proxy]
uvicorn llm_relay.proxy.proxy:app --host 0.0.0.0 --port 8083

# 방법 2: Docker
cp .env.example .env        # 필요에 따라 수정
docker compose up -d
```

접속 주소:
- `/dashboard/` — CLI 상태, 비용, 위임 히스토리, Turn Monitor (alive 세션만; `?include_dead=1` 로 우회)
- `/display/` — 턴 카운터 + CC/Codex/Gemini 세션 카드 (alive 필터: CC=cc_pid+TTY fallback, Codex/Gemini=fd-open)

### MCP 서버

```bash
llm-relay-mcp               # stdio 전송, 7개 도구
```

### Claude Code API 프록시

```bash
# Claude Code에서 설정
export ANTHROPIC_BASE_URL=http://localhost:8080
```

## CLI 지원 현황

| CLI | 상태 |
|-----|------|
| Claude Code | 전체 지원 |
| OpenAI Codex | 전체 지원 |
| Gemini CLI | Display 지원, oauth-personal 서버사이드 403 버그 ([#25425](https://github.com/google-gemini/gemini-cli/issues/25425)) |

## 요구 사항

- Python >= 3.9
- MCP 도구는 Python >= 3.10

## 라이선스

MIT

## 생태계

[QuartzUnit](https://github.com/QuartzUnit) 오픈소스 생태계의 일부입니다.
