# llm-relay — Claude Code 프로젝트 지시 파일

> 모든 LLM 사용 통합 관리 — API 프록시 + 세션 진단(8 detector) + 복구 + 멀티 CLI 오케스트레이션
> 패키지명: `llm-relay` (구 cc-relay + ccpulse 통합)
> 경로: `~/GitHub/llm-relay/`
> Mirror Agent 생태계 오픈망 DLC

## 스택

- Python 3.9+
- Build: pyproject.toml (hatchling)
- HTTP: httpx (프록시), aiohttp (비동기)
- CLI: click + rich
- Test: pytest
- Lint: ruff (line-length 120)
- Zero-dep core (detect/recover/guard/cost)

## 모듈 구조

```
src/llm_relay/
├── proxy       — API 투명 프록시 (캐시/토큰 모니터링/12전략 pruning) + 통합 Starlette 앱
├── detect      — 8 detector (orphan/stuck/inflation...)
├── recover     — 세션 복구 / doctor (7개 건강 검사)
├── guard       — 4-tier 임계값 daemon
├── cost        — per-1% cost 산출 / ratelimit 헤더 분석
├── orch        — 멀티 CLI 오케스트레이션 (models/discovery/executor/db/router) — stdlib only
├── mcp         — FastMCP 서버 7 tools (stdio transport) — optional dep: mcp[cli]
├── api         — HTTP API 10 endpoints (Starlette) — /api/v1/... + display helper (멀티CLI 세션탐색, JSONL tail, proc liveness)
├── dashboard   — 정적 SPA (vanilla JS, 빌드물 번들, npm 불필요) + Turn Monitor 섹션
├── display     — 전용 턴 카운터 웹 페이지 `/display/` (CC/Codex/Gemini 세션카드+provider배지+TTY배지+마지막프롬프트+프로세스생존필터+composition 파이차트+SNR+duplicate reads: CC=proxy DB / Codex·Gemini=세션파일 직접 파싱+fd-open PID→TTY/conn_type/term_name)
├── providers   — LLM provider 추상화 (CC/Codex/Gemini 세션 파싱)
├── strategies  — pruning 전략
└── formatters  — 출력 포매터
```

## 운영

```
# Docker 프록시 (cc-relay 대체, port 8080 + 8082)
# DB: ~/.cc-relay/usage.db 직접 이어쓰기
docker compose up -d

# API + 대시보드 (ZBook 다이렉트, port 8083)
.venv/bin/uvicorn llm_relay.proxy.proxy:app --host 0.0.0.0 --port 8083

# MCP 서버 (CC/MA에서 stdio 연결)
llm-relay-mcp   # 또는 python -m llm_relay.mcp
```

Claude Code 프록시 연결: `ANTHROPIC_BASE_URL=http://localhost:8080`

웹 페이지 (`/display`, `/dashboard` 는 자동으로 trailing slash 리다이렉트):
- `/dashboard/` — 통합 대시보드 (CLI 상태, 비용, 히스토리, Turn Monitor — alive 세션만, `?include_dead=1` 로 우회)
- `/display/` — 턴 카운터 전용 (CC/Codex/Gemini 세션 통합 표시 + provider 배지 + TTY·conn_type·term_name 배지 + 마지막 프롬프트 + 4존 임계값 + composition 파이차트 + cache hit rate + alive 필터: CC=cc_pid+TTY fallback, Codex/Gemini=fd-open+PID 기반 TTY 감지)

MCP 도구 (7): cli_delegate, cli_status, cli_probe, orch_delegate, orch_history, relay_stats, session_turns

## ★★★ Privacy Filter — 커밋/push 전 필수

이 레포는 GrowthBook 인터셉트 기능을 포함. 커밋/push/PR 전에 비공개 협정 위반 여부 반드시 확인.

**절대 포함 금지:**
- GrowthBook flag override **구체 값** (before→after 매핑)
- Override 구현 상세 (proxy scripts, file watchers, injection methods)
- Defence stack 아키텍처 상세
- 비공개 기여자 개인정보

**공개 가능:**
- 플래그 이름 + Anthropic 기본값 (`~/.claude.json` 공개 정보)
- "플래그를 오버라이드했다"는 사실 (구체 값 없이)
- B4/B5 결과 수치

**Pre-push check:**
```bash
grep -rn "1,000,000\|999999\|keepRecent.*999\|gap.*9999\|intercept\.py\|inject\.py\|flags\.json\|gb.patcher\|inotify.*claude\|3.layer\|defence.stack" --include="*.md" --include="*.py" src/ docs/ README.md
```
결과 0건이어야 push 가능.

## 코딩 규칙

> 공통 규칙: `~/docs/CODING-STANDARDS.md` 참조. 아래는 llm-relay 고유 규칙.
- Python 3.9 호환 (typing.Union 등 사용, `X | Y` 구문 금지)
- Zero-dep 원칙: detect/recover/guard/cost/orch는 표준 라이브러리만
- provider 추가 시 providers/ 하위에 독립 모듈로
- ruff ignore: UP006, UP007, UP032, UP035, UP045 (Py3.9 호환)
- subprocess 호출 시 반드시 `stdin=subprocess.DEVNULL` (stdin 블로킹 방지)

## 참고 문서

- 아키텍처 설계: `docs/design-opennet-cli-orchestration.md`
- 전략/설계/분석: 로컬 프로젝트 메모리 (Claude Code memory 디렉토리)
- 레거시: cc-relay (프록시 원본), ccpulse (진단 원본) — llm-relay로 통합 완료

## 공개 준비 검증 (2026-04-15)

- ruff 린트 0 에러 (79개 수정: cc_relay→llm_relay 11곳, Py3.9 호환, import 정리)
- 481 tests pass (4/28, v0.8.6: composition 22 + per-tool detail 8 tests)
- 시크릿/하드코딩 0건, .gitignore 정상
- display 페이지 CC+Codex+Gemini 3 CLI 통합 표시 완료 (composition 파이차트, cache hit rate, duplicate reads, TTY/conn_type/term_name, 도구별 호출횟수, exec 성공률, thinking count)
- Codex/Gemini는 프록시 DB 미경유 → provider 어댑터 직접 세션 파일 탐색으로 병합
- Gemini CLI v0.38.0 oauth-personal 403 장애중 — 서버사이드 cloudaicompanionProject 프로비저닝 버그 (#25425), 마지막 성공 4/10, API key 전환으로 우회 가능
