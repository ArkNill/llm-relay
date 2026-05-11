# llm-relay

LLM 사용 통합 관리 — API 프록시 + 세션 진단 + 멀티 CLI 오케스트레이션

[English](README.md) | [llms.txt](llms.txt)

## 기능

- **Proxy**: API 투명 프록시 — 캐시/토큰 모니터링 + 12전략 pruning
- **Detect**: 7종 디텍터 (orphan, stuck, bloat, synthetic, cache, resume, microcompact)
- **Recover**: 세션 복구 + doctor (7개 건강 검사)
- **Guard**: 4-tier 임계값 데몬 — dual-zone(절대+비율) 분류
- **Cost**: per-1% 비용 산출 + rate-limit 헤더 분석
- **Orch**: 멀티 CLI 오케스트레이션 (Claude Code, Codex CLI, Gemini CLI)
- **Display**: 멀티 CLI 세션 모니터 — provider 배지 + 프로세스 생존 감지
- **I18n**: 다국어 지원 (영어/한국어) — 브라우저 자동 감지 + `LLM_RELAY_LANG` 환경변수
- **MCP**: stdio 전송 8개 도구 (cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns, session_history)

## 설치

### 1. Python 환경 설정

<details>
<summary><b>Windows (pip)</b></summary>

```cmd
python -m venv .venv
.venv\Scripts\activate
```
</details>

<details>
<summary><b>Windows (conda)</b></summary>

```cmd
conda create -n llm-relay python=3.12
conda activate llm-relay
```
</details>

<details>
<summary><b>Linux / macOS (pip)</b></summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
```
</details>

### 2. llm-relay 설치

```bash
# 기본 (SQLite, 설정 불필요)
pip install llm-relay

# 프록시 + 웹 대시보드
pip install llm-relay[proxy]

# PostgreSQL 지원 (장기 데이터 분석 + 벡터 검색)
pip install llm-relay[pg]

# MCP 서버 (Python 3.10 이상)
pip install llm-relay[mcp]

# 전부
pip install llm-relay[all]
```

### 3. 데이터베이스 선택

| | SQLite (기본) | PostgreSQL |
|---|---|---|
| 설정 | 불필요 | PG 서버 필요 |
| 적합 | 시작, 가벼운 사용 | 장기 데이터 분석, 벡터 검색 |
| 설치 | `pip install llm-relay` | `pip install llm-relay[pg]` |
| 설정 | (없음) | `LLM_RELAY_DB=postgresql://user:pass@host/db` |

### 4. 초기화

```bash
llm-relay init
```

## 빠른 시작

### 원클릭 설정

```bash
llm-relay init              # CLI 자동 감지, 프록시 설정, 서버 시작
```

### CLI 명령어

```bash
llm-relay scan              # 세션 건강 검사 (7종 디텍터)
llm-relay doctor            # 설정 건강 검사 (7개 항목)
llm-relay recover           # 세션 컨텍스트 추출 (재개용)
llm-relay serve             # 프록시 서버 + 웹 대시보드
llm-relay top               # 라이브 터미널 모니터 (btop 스타일)
llm-relay service install   # Windows: 백그라운드 서비스 + 자동 시작 (콘솔 창 없음)
llm-relay service stop      # Windows: 서비스 중지
llm-relay service uninstall # Windows: 서비스 제거 + 정리
```

### 웹 대시보드

```bash
# 방법 1: 직접 실행 (Linux/macOS/Windows)
llm-relay serve --port 8083

# 방법 2: Docker (Linux)
cp .env.public .env         # 필요에 따라 수정
docker compose up -d
```

접속 주소:
- `/dashboard/` — CLI 상태, 비용, 위임 히스토리, Turn Monitor (alive 세션만; `?include_dead=1` 로 우회)
- `/display/` — 턴 카운터 + CC/Codex/Gemini 세션 카드 (alive 필터: CC=cc_pid+TTY fallback, Codex/Gemini=fd-open; Windows는 mtime+프로세스 감지)
- `/history/` — 세션 대화 히스토리 브라우저

### MCP 서버

```bash
llm-relay-mcp               # stdio 전송, 8개 도구
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

## 플랫폼 지원

| 플랫폼 | 모드 | 비고 |
|--------|------|------|
| Linux | 네이티브 + Docker | 전체 기능, systemd 권장 |
| macOS | 네이티브 | 전체 기능 |
| Windows | 네이티브 | `llm-relay service install`로 백그라운드 데몬 (콘솔 창 없음) |

## 요구 사항

- Python >= 3.9
- MCP 도구는 Python >= 3.10

## 라이선스

MIT

## 생태계

[QuartzUnit](https://github.com/QuartzUnit) 오픈소스 생태계의 일부입니다.
