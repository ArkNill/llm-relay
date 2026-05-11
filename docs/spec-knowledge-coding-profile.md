# Knowledge Module — Coding Profile Template Spec

> **Status**: Draft v0.1 (2026-05-04)  
> **Module**: `llm-relay/knowledge`  
> **Target**: `llm-relay init --profile coding`

---

## 1. Overview

### 1.1 Purpose

CLI 기반으로 LLM을 사용하는 개발자가 별도 설정 없이 즉시 활용할 수 있는
**문서 파일 시스템 템플릿**을 제공한다.

사용자가 LLM과 작업한 결과물(결정, 교훈, 진행 상태, 참조 자료)이
일회성으로 휘발되지 않고 **구조화된 개인 자산**으로 축적되도록 한다.

### 1.2 Design Principles

| 원칙 | 설명 |
|------|------|
| **Zero-force** | 강제하지 않음. 감지 → 제안 → 가이드라인 제공 |
| **File-first** | DB 아닌 파일시스템 기반. git-friendly, 이식성 최대화 |
| **Tool-agnostic** | Claude Code, Codex, Gemini, 로컬 LLM 어디서든 동작 |
| **Progressive** | 처음엔 3개 파일로 시작 → 필요에 따라 확장 |
| **Opinionated defaults** | "잘 설계된 스키마를 공짜로 뿌리는 것" — 합리적 기본값 제공 |

### 1.3 Inspiration

이 스펙은 203개 메모리 파일 / 8개 티어 / 261개 문서로 구성된
실제 운영 시스템(ZBook, 2026-02~05)에서 추출한 패턴이다.

### 1.4 Related Documents

- **General Profile**: `spec-knowledge-general-profile.md` — 비개발자/일반 사용자용 프로필
- **Shared Core**: 양쪽 프로필이 공유하는 요소: `.knowledge/` 경로, Web UI (`/knowledge`), 토큰 예산 (2000), `INSTRUCTIONS.md` + 심링크, `archive/` 디렉토리, health check

---

## 2. Directory Structure

### 2.1 Minimal (First-time Setup)

```
project-root/
├── INSTRUCTIONS.md          # 프로젝트 규칙 + LLM 지시사항
└── .knowledge/
    ├── INDEX.md             # 메모리 인덱스 (자동 생성 가능)
    ├── me.md                # 사용자 프로필 (역할, 선호, 스택)
    └── decisions.md         # 핵심 결정 로그
```

INSTRUCTIONS.md + knowledge 파일 3개로 시작. 이것만으로도 다음 세션에서 컨텍스트 반복이 감소한다.

> **Note**: General Profile 사용자는 `llm-relay init --profile general` 또는
> `llm-relay start`로 더 간단한 구조(2파일)를 생성할 수 있음.
> → `spec-knowledge-general-profile.md` 참조.

### 2.2 Standard (Recommended)

```
project-root/
├── INSTRUCTIONS.md          # 프로젝트 규칙 (= CLAUDE.md/AGENTS.md/GEMINI.md)
└── .knowledge/
    ├── INDEX.md             # 메모리 인덱스
    │
    ├── feedback/            # 교정 사항 (실수에서 배운 것)
    │   ├── no-force-push.md
    │   └── test-before-commit.md
    │
    ├── project/             # 진행 중인 작업
    │   ├── auth-migration.md
    │   └── api-v2-rollout.md
    │
    ├── reference/           # 참조 자료 (인프라, 설정, 외부 시스템)
    │   ├── deploy-checklist.md
    │   └── staging-env.md
    │
    ├── learned/             # 기술 발견, 리서치 결과
    │   └── redis-cluster-pitfalls.md
    │
    └── archive/             # 완료/폐기된 항목
        └── completed-auth-migration.md
```

### 2.3 Advanced (Multi-project / Multi-tool)

```
~/
├── INSTRUCTIONS.md              # 글로벌 규칙 (모든 프로젝트 공통)
├── .knowledge/                  # 글로벌 메모리
│   ├── INDEX.md
│   ├── me.md
│   ├── feedback/
│   ├── reference/
│   └── archive/
│
├── project-a/
│   ├── INSTRUCTIONS.md          # 프로젝트 로컬 규칙 (글로벌 override)
│   └── .knowledge/              # 프로젝트 로컬 메모리
│       ├── INDEX.md
│       ├── project/
│       └── learned/
│
└── project-b/
    ├── INSTRUCTIONS.md
    └── .knowledge/
```

**Scope Resolution Order:**
1. Project-local `.knowledge/` (최우선)
2. Global `~/.knowledge/` (fallback)
3. `INSTRUCTIONS.md` local → global 순

---

## 3. File Schema

### 3.1 Frontmatter (YAML Header)

모든 knowledge 파일은 YAML frontmatter를 가진다:

```yaml
---
title: "Force push 금지"
type: feedback              # feedback | project | reference | learned | profile
priority: high              # high | medium | low
created: 2026-05-04
updated: 2026-05-04
tags: [git, safety]         # 선택사항, 검색/필터용
status: active              # active | archived | superseded
---
```

**Required fields:** `title`, `type`, `created`  
**Optional fields:** `priority`, `updated`, `tags`, `status`

### 3.2 Body Structure by Type

#### feedback (교정 사항)

```markdown
---
title: "프로덕션 DB에 직접 쿼리 금지"
type: feedback
priority: high
created: 2026-05-04
---

프로덕션 DB에 SELECT 포함 모든 직접 쿼리 금지. 반드시 read-replica 사용.

**Why:** 2026-04-20 인시던트 — 무거운 SELECT가 write lock 유발, 3분 다운타임.

**How to apply:** DB 접속 시 항상 호스트 확인. `prod-read.internal` 사용.
```

#### project (진행 중인 작업)

```markdown
---
title: "Auth v2 마이그레이션"
type: project
priority: high
created: 2026-04-15
updated: 2026-05-01
status: active
tags: [auth, backend, q2-goal]
---

## 목표
JWT → session-based auth 전환. 기존 클라이언트 하위호환 유지.

## 현재 상태
- [x] 스키마 설계
- [x] 마이그레이션 스크립트
- [ ] 롤백 플랜 검증
- [ ] 카나리 배포 (5/10 예정)

## 컨텍스트
- 법무팀 요청: 세션 토큰 저장 방식 컴플라이언스 이슈
- 마감: 2026-05-15
```

#### reference (참조 자료)

```markdown
---
title: "스테이징 환경 접속 정보"
type: reference
priority: medium
created: 2026-03-10
---

## 엔드포인트
- API: https://staging-api.example.com
- DB: staging-db.internal:5432 (read-replica: staging-read.internal)
- Redis: staging-redis.internal:6379

## 접근 방법
VPN 필수. `make tunnel-staging` 으로 로컬 포워딩.

## 주의사항
매주 월요일 03:00 UTC 데이터 리셋됨.
```

#### learned (배운 것)

```markdown
---
title: "Redis Cluster에서 KEYS 명령 절대 금지"
type: learned
priority: medium
created: 2026-04-22
tags: [redis, performance, incident]
---

Redis Cluster 환경에서 `KEYS *` 사용 시 모든 노드 블로킹.
10만 키 이상이면 수 초간 응답 불가.

**대안:** `SCAN` 커맨드 사용 (cursor 기반, non-blocking)

**출처:** 2026-04-22 스테이징 장애 분석
```

#### profile (사용자 프로필)

```markdown
---
title: "개발자 프로필"
type: profile
created: 2026-05-04
updated: 2026-05-04
---

## 역할
백엔드 시니어 엔지니어. 팀 리드 겸임.

## 기술 스택
- Primary: TypeScript, Go
- Infra: AWS ECS, Terraform
- DB: PostgreSQL, Redis

## 작업 선호
- 테스트 먼저 작성
- PR은 작게, 자주
- 문서는 코드 옆에 (별도 wiki 싫음)

## LLM 사용 패턴
- 코드 리뷰 보조
- 아키텍처 설계 토론
- 인시던트 분석 가속
```

---

## 4. INDEX.md (인덱스)

### 4.1 형식

```markdown
# Knowledge Index

> Auto-generated from .knowledge/ files. Manual edits preserved.

## Critical (항상 참조)
- [Force push 금지](feedback/no-force-push.md) — prod 브랜치 사고 방지
- [프로덕션 DB 직접 쿼리 금지](feedback/no-prod-query.md) — read-replica 사용

## Active Work
- [Auth v2 마이그레이션](project/auth-migration.md) — JWT→session, 마감 5/15
- [API v2 롤아웃](project/api-v2-rollout.md) — 카나리 진행 중

## References
- [스테이징 환경](reference/staging-env.md) — 접속 정보 + VPN
- [배포 체크리스트](reference/deploy-checklist.md) — 릴리스 전 필수 확인

## Learned
- [Redis KEYS 금지](learned/redis-keys-danger.md) — SCAN 사용할 것
```

### 4.2 자동 생성 규칙

INDEX.md는 frontmatter를 파싱하여 자동 생성할 수 있다:

```
분류 기준: type → priority → created (역순)
표시 형식: - [title](relative-path) — description 첫 줄 요약
섹션 분리: priority=high → "Critical", status=active → "Active Work", 나머지 type별 그룹
```

사용자가 수동으로 추가한 줄(자동 생성 마커 외부)은 보존한다.

---

## 5. INSTRUCTIONS.md (프로젝트 지시)

### 5.1 목적

LLM이 이 프로젝트에서 작업할 때 따라야 할 규칙.
기존 `CLAUDE.md` / `.cursorrules` / `AGENTS.md`의 범용 대체.

### 5.2 Template

```markdown
# Project Instructions

## About This Project
[프로젝트 한 줄 설명]

## Tech Stack
- Language: [...]
- Framework: [...]
- Database: [...]

## Rules
- [프로젝트 특화 규칙들]
- [코딩 스타일, 금지 사항 등]

## Knowledge
This project uses `.knowledge/` for persistent memory.
See `.knowledge/INDEX.md` for current state.
```

### 5.3 Cross-tool Compatibility

```bash
# 프로젝트 초기화 시 자동 생성되는 심링크
INSTRUCTIONS.md              # 원본 (canonical)
CLAUDE.md -> INSTRUCTIONS.md # Claude Code 호환
AGENTS.md -> INSTRUCTIONS.md # Codex 호환
GEMINI.md -> INSTRUCTIONS.md # Gemini 호환
```

하나의 원본 파일로 모든 LLM CLI 도구와 호환.

---

## 6. Lifecycle Management

### 6.1 상태 전이

```
[생성] → active → archived
                → superseded (새 버전으로 대체됨)
```

### 6.2 Archive 규칙

| 조건 | 액션 |
|------|------|
| project type + 모든 체크박스 완료 | → archive/ 이동 제안 |
| 90일 이상 미수정 + priority=low | → stale 경고 표시 |
| 명시적으로 superseded 표기 | → archive/ 이동 |
| feedback type | 삭제 안 함 (교훈은 영구 보존) |

### 6.3 Health Check

`llm-relay knowledge health` 명령으로 확인:

```
Knowledge Health Report
━━━━━━━━━━━━━━━━━━━━━━
Total files:     42
Active:          35
Archived:         7
Stale (90d+):     3 ⚠️
  - reference/old-staging.md (142 days)
  - project/done-migration.md (95 days, all tasks ✓)
  - learned/webpack-config.md (120 days)

Suggestions:
  → Move project/done-migration.md to archive/ (completed)
  → Review reference/old-staging.md (possibly outdated)

INDEX.md sync: ✓ up to date
```

---

## 7. Detection & Suggestion (감지 → 제안)

### 7.1 llm-relay가 감지하는 조건

| 신호 | 의미 |
|------|------|
| `.knowledge/` 디렉토리 없음 | 구조화 안 됨 |
| `INSTRUCTIONS.md` / `CLAUDE.md` 없음 | 프로젝트 규칙 없음 |
| 3회 이상 동일 컨텍스트 반복 전달 | 메모리가 없어서 반복 중 |
| 세션 종료 시 유의미한 결정 존재 | 저장 대상 있지만 저장 안 됨 |

### 7.2 제안 UX

```
┌─────────────────────────────────────────────────────┐
│ 💡 llm-relay: Knowledge structure not detected.     │
│                                                     │
│ Your LLM sessions produce decisions, rules, and     │
│ context that could persist across sessions.         │
│                                                     │
│ Set up a knowledge structure?                       │
│                                                     │
│ [Yes, minimal (3 files)]  [Yes, standard]  [Skip]  │
└─────────────────────────────────────────────────────┘
```

**Skip 선택 시**: 다시 묻지 않음 (`.llm-relay-ignore` 생성)

### 7.3 세션 종료 시 추출 제안

```
┌─────────────────────────────────────────────────────┐
│ 📝 This session produced saveable knowledge:        │
│                                                     │
│ • Decision: "Use Redis Streams instead of Pub/Sub"  │
│ • Rule: "Never deploy on Fridays"                   │
│ • Reference: "New staging endpoint: ..."            │
│                                                     │
│ Save to .knowledge/?                                │
│                                                     │
│ [Save all]  [Select]  [Skip]                        │
└─────────────────────────────────────────────────────┘
```

---

## 8. CLI Interface

### 8.1 Commands

```bash
# 초기화
llm-relay init --profile coding          # Standard 구조 생성
llm-relay init --profile coding --minimal # Minimal 구조 (3 files)

# 조회
llm-relay knowledge list                  # 전체 목록
llm-relay knowledge list --type feedback  # 타입별 필터
llm-relay knowledge show <filename>       # 파일 내용 표시
llm-relay knowledge search "redis"        # 전문 검색

# 관리
llm-relay knowledge add                   # 대화형 새 항목 추가
llm-relay knowledge archive <filename>    # archive/로 이동
llm-relay knowledge health                # 건강 체크
llm-relay knowledge reindex               # INDEX.md 재생성

# 내보내기
llm-relay knowledge export --format json  # JSON 내보내기
llm-relay knowledge export --format html  # 웹 뷰 생성
```

### 8.2 Web UI (8083)

기존 llm-relay 웹 UI에 추가되는 화면:

| 경로 | 기능 |
|------|------|
| `/knowledge` | 대시보드 — 전체 현황, health score |
| `/knowledge/browse` | 파일 브라우저 — 타입별 트리 뷰 |
| `/knowledge/timeline` | 타임라인 — 언제 뭘 배웠나 |
| `/knowledge/search` | 전문 검색 |
| `/knowledge/suggest` | 미저장 세션 정보 표시 (저장 제안) |

---

## 9. Cross-tool Injection

### 9.1 컨텍스트 주입 방식

llm-relay가 세션을 중계할 때, `.knowledge/`의 관련 항목을 자동 주입:

```
사용자 프롬프트: "Redis 캐시 계층 설계해줘"
        ↓
llm-relay 감지: tags에 "redis" 포함된 knowledge 존재
        ↓
주입: learned/redis-keys-danger.md + reference/staging-env.md (Redis 섹션)
        ↓
LLM은 기존 knowledge를 알고 있는 상태로 응답
```

### 9.2 주입 대상 선정 기준

| 우선순위 | 조건 |
|----------|------|
| 1 | priority=high + type=feedback (항상 주입) |
| 2 | 프롬프트 키워드와 tags/title 매칭 |
| 3 | 최근 7일 내 수정된 active project |
| 4 | me.md (사용자 프로필, 항상 포함) |

**토큰 예산**: 주입 총량은 설정 가능 (기본: 2000 tokens)

---

## 10. Migration from Existing Systems

### 10.1 CLAUDE.md → INSTRUCTIONS.md

```bash
llm-relay migrate --from claude-md
# CLAUDE.md 내용을 INSTRUCTIONS.md로 변환
# 메모리 관련 내용은 .knowledge/로 분리
# 원본 CLAUDE.md는 심링크로 대체
```

### 10.2 기존 .claude/ memory → .knowledge/

```bash
llm-relay migrate --from claude-memory
# .claude-gt/projects/*/memory/ 파일들을
# .knowledge/ 구조로 변환 (frontmatter 정규화)
```

### 10.3 Cursor Rules → INSTRUCTIONS.md

```bash
llm-relay migrate --from cursorrules
# .cursorrules 내용을 INSTRUCTIONS.md Rules 섹션으로
```

---

## 11. Configuration

### 11.1 `.llm-relay.yaml` (프로젝트별)

```yaml
knowledge:
  enabled: true
  profile: coding
  auto_suggest: true          # 세션 종료 시 저장 제안
  auto_inject: true           # 세션 시작 시 관련 knowledge 주입
  inject_budget_tokens: 2000  # 주입 토큰 예산
  health_check_interval: 7d   # 건강 체크 주기
  index_auto_update: true     # INDEX.md 자동 갱신
  
  # 타입별 보존 정책
  retention:
    feedback: permanent       # 교훈은 영구 보존
    project: archive-on-complete
    reference: warn-after-90d
    learned: warn-after-180d
```

### 11.2 글로벌 설정 (`~/.llm-relay/config.yaml`)

```yaml
knowledge:
  global_path: ~/.knowledge   # 글로벌 knowledge 경로
  language: auto              # 문서 언어 (auto = 시스템 로케일)
  editor: $EDITOR             # knowledge 편집 시 사용할 에디터
```

---

## 12. Implementation Phases

> **Note**: General Profile과 동일 Phase 번호 체계.
> 공통 인프라는 한 번만 구현하고 프로필별 분기.

### Phase 1: Scaffold (MVP) — General Profile과 공유
- `llm-relay init --profile coding` 명령
- 디렉토리 + 템플릿 파일 생성
- INSTRUCTIONS.md + 심링크 생성
- INDEX.md 자동 생성 (frontmatter 파싱)

### Phase 2: Health & Management — General Profile과 공유
- `knowledge health` / `knowledge list` / `knowledge search`
- Stale 감지 + archive 제안
- INDEX.md 자동 재생성

### Phase 3: Detection & Suggestion — General Profile과 공유
- 구조 미존재 시 제안 UX
- 세션 종료 시 saveable knowledge 추출
- 반복 컨텍스트 감지

### Phase 4: Auto-injection — General Profile과 공유
- 세션 시작 시 관련 knowledge 자동 주입
- 토큰 예산 관리
- 키워드/태그 매칭 엔진

### Phase 5: Web UI — General Profile과 공유
- `/knowledge` 대시보드 (프로필에 따라 다른 뷰)
- 브라우저, 타임라인, 검색
- 편집/삭제/아카이브 UI

---

## 13. Differentiation

### 13.1 기존 도구와의 차이

| | CLAUDE.md | .cursorrules | Notion AI | **llm-relay knowledge** |
|---|---|---|---|---|
| 구조 | 단일 파일 | 단일 파일 | 클라우드 DB | **파일시스템 계층** |
| 자동 축적 | 수동 | 없음 | 부분 | **감지+제안** |
| Cross-tool | CC 전용 | Cursor 전용 | 독립 | **CC/Codex/Gemini/로컬** |
| 버전 관리 | git 수동 | git 수동 | 자체 | **git 네이티브** |
| 오프라인 | ✓ | ✓ | ✗ | **✓** |
| 검색 | grep | 없음 | 내장 | **CLI + Web** |
| 이식성 | 낮음 | 낮음 | 벤더 락인 | **폴더 복사로 끝** |

### 13.2 핵심 가치 제안

> **"LLM을 쓸수록 당신의 .knowledge/는 두꺼워지고,
> 다음 세션은 더 빨라진다."**

---

## Appendix A: File Naming Convention

```
{type}_{descriptive-slug}.md

Examples:
  feedback_no-force-push.md
  project_auth-v2-migration.md
  reference_staging-environment.md
  learned_redis-cluster-pitfalls.md
```

**Rules:**
- 소문자 + 하이픈 (slug style)
- Type prefix 필수 (자동 분류 기반)
- 날짜 접미사 선택 (`_20260504`) — 시점 스냅샷일 때만
- 확장자: 항상 `.md`

## Appendix B: Priority System

| Level | 의미 | 예시 |
|-------|------|------|
| `high` | 매 세션 반드시 참조. 위반 시 실제 피해 | 안전 규칙, 인증 정보, 삭제 금지 |
| `medium` | 관련 작업 시 참조. 과거 교훈 | 특정 도구 사용법, 배포 체크리스트 |
| `low` | 필요 시 검색. 보조 정보 | 외부 링크, 선택적 참고 자료 |

## Appendix C: Template Files (init 시 생성)

### `me.md` 템플릿

```markdown
---
title: "My Profile"
type: profile
created: {{date}}
---

## Role
[Your role — e.g., Backend Engineer, Data Scientist, Full-stack Developer]

## Tech Stack
- Primary: [languages]
- Framework: [frameworks]
- Infra: [cloud/tools]

## Preferences
- [How you like to work with LLMs]
- [Coding style preferences]
- [Communication preferences]
```

### `decisions.md` 템플릿

```markdown
---
title: "Key Decisions"
type: learned
created: {{date}}
---

## Decisions Log

| Date | Decision | Reason | Status |
|------|----------|--------|--------|
| {{date}} | [What was decided] | [Why] | Active |
```
