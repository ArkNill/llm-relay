# llm-relay

LLM 사용 통합 관리 — API 프록시 + 세션 진단 + 멀티 CLI 오케스트레이션

[English](README.md) | [llms.txt](llms.txt)

## 기능

- **Proxy**: API 투명 프록시 — 캐시/토큰 모니터링 + 12전략 pruning
- **Detect**: 8종 디텍터 (orphan, stuck, inflation, synthetic, bloat, cache, resume, microcompact)
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
llm-relay scan              # 세션 건강 검사 (8종 디텍터)
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
- `/dashboard/` — CLI 상태, 비용, 위임 히스토리
- `/display/` — 턴 카운터 + CC/Codex/Gemini 세션 카드

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
