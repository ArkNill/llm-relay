# Knowledge Module — General Profile Template Spec

> **Status**: Draft v0.1 (2026-05-04)  
> **Module**: `llm-relay/knowledge`  
> **Target**: `llm-relay init --profile general`  
> **Audience**: 비개발자, 바이브코더, 일반 지식노동자

---

## 1. Overview

### 1.1 Problem

일반 사용자의 LLM 상호작용:
```
질문 → 답변 → 잊음 → 같은 질문 반복
"나 채식주의자라고 세 번째 말하는 건데..."
"지난번에 결정한 거 뭐였지..."
```

### 1.2 Solution

사용자가 **아무것도 하지 않아도** 쌓이고, 원하면 정리할 수 있는 지식 시스템.
"잘 설계된 빈 DB 스키마를 공짜로 뿌리는 것" — 구조를 제공하되 강제하지 않음.

### 1.3 Design Principles

| 원칙 | 설명 | 근거 |
|------|------|------|
| **Zero-config** | 설치 즉시 동작, 설정 필요 없음 | CoWork OS: "5분 만에 시작" |
| **Plain language** | 모든 파일은 평범한 한국어/영어 문장 | MindStudio: "Everything is markdown in natural language" |
| **Folder-first** | 태그/DB 아닌 폴더 구조 | 연구: 75명 대상 실험서 폴더>>태그 선호 |
| **Progressive disclosure** | 2개 파일로 시작 → 필요 시 확장 | peterkrueck: 4파일로 시작 |
| **No jargon** | frontmatter/YAML/schema 같은 용어 없음 | 비개발자는 YAML을 모름 |
| **AI does the work** | 사용자가 정리하는 게 아니라 AI가 정리 | Mem.ai: "self-organizing workspace" |

### 1.4 Research Foundation

| 출처 | Stars/Users | 차용 패턴 |
|------|-------------|-----------|
| superpowers (obra) | 178K | 7단계 방법론, 계획서=기억 |
| gstack (garrytan) | 88.8K | 페르소나 기반 스킬, 자연어 지시 |
| Mem0 | 50K | 3-scope 메모리 (user/session/agent) |
| fabric (danielmiessler) | 41.5K | 패턴 = 재사용 프롬프트 템플릿, contexts/ |
| PARA (Tiago Forte) | 수백만 사용자 | 실행가능성 기반 4계층 |
| CoWork OS | — | 비개발자 온보딩 대화, 5분 설정 |
| MindStudio | — | Rules/Context/Skills/Learnings 4레이어 |
| Karpathy LLM Wiki | — | raw/wiki 2층, 70x RAG 대비 효율 |
| Pieces LTM-2 | — | OS-level 자동 캡처, 시간 기반 검색 |

### 1.5 Related Documents

- **Coding Profile**: `spec-knowledge-coding-profile.md` — 개발자용 프로필
- **Shared Core**: 양쪽 프로필이 공��하는 요소: `.knowledge/` 경로, Web UI (`/knowledge`), 토큰 예산 (2000), `INSTRUCTIONS.md` + 심링크, `archive/` 디렉토리, health check, `.llm-relay.yaml`

---

## 2. Mental Model (사용자가 이해하는 비유)

### 2.1 "AI 비서의 수첩"

```
당신의 AI 비서가 수첩을 들고 있습니다.

📓 나에 대해 (About Me)     — 비서가 당신을 아는 정보
📋 진행 중인 일 (Ongoing)   — 지금 같이 하고 있는 일
💡 배운 것 (Learned)        — 대화에서 나온 유용한 정보  
📌 규칙 (Rules)            — "이것만은 꼭 지켜줘"
📦 보관함 (Archive)         — 끝난 일, 나중에 볼 것
```

사용자는 "폴더 구조"를 의식하지 않아도 됨.
웹 UI에서는 위 5개 탭으로 보여줌.

### 2.2 왜 이 5개인가

| 카테고리 | PARA 매핑 | MindStudio 매핑 | 사용자 질문 |
|----------|-----------|-----------------|-------------|
| About Me | — | Context | "내가 누구인지 알아?" |
| Ongoing | Projects | — | "지금 뭐 하고 있었지?" |
| Learned | Resources | Learnings | "전에 뭐라고 했더라?" |
| Rules | Areas | Rules | "이건 꼭 이렇게 해줘" |
| Archive | Archives | — | "예전에 한 거 찾아줘" |

---

## 3. Directory Structure

### 3.1 Minimal (첫 시작 — 파일 2개 + INSTRUCTIONS.md)

```
project-or-home/
├── INSTRUCTIONS.md          # LLM 지시 (자동 생성, 사용자 안 봐도 됨)
├── CLAUDE.md -> INSTRUCTIONS.md   # 심링크 (자동)
├── AGENTS.md -> INSTRUCTIONS.md   # 심링크 (자동)
├── GEMINI.md -> INSTRUCTIONS.md   # 심링크 (자동)
└── .knowledge/
    ├── about-me.md          # 내 소개
    └── rules.md             # AI한테 지키라고 한 것들
```

이것만으로 다음 세션부터 "나 누구야"를 반복하지 않아도 됨.
(INSTRUCTIONS.md와 심링크는 시스템이 자동 생성 — 사용자가 신경 쓸 필요 없음)

### 3.2 Standard (권장 — 사용하면서 자연 성장)

```
.knowledge/
├── about-me.md          # 내 소개 (역할, 선호, 상황)
├── rules.md             # AI 규칙 ("이렇게 해줘/하지 마")
├── ongoing/             # 진행 중인 일
│   ├── diet-plan.md
│   └── house-hunting.md
├── learned/             # 대화에서 배운 것
│   ├── tax-deductions.md
│   └── good-restaurants.md
└── archive/             # 끝난 것
    └── 2026-04-trip-planning.md
```

### 3.3 Power User (확장형)

```
.knowledge/
├── about-me.md
├── rules.md
├── ongoing/
├── learned/
├── templates/           # 자주 쓰는 요청 패턴
│   ├── email-reply.md
│   ├── meeting-prep.md
│   └── weekly-review.md
├── people/              # 사람 관계 메모
│   ├── team-members.md
│   └── clients.md
├── decisions/           # 중요한 결정 기록
│   └── 2026-05-career-direction.md
└── archive/
```

---

## 4. File Format (사용자가 보는 것)

### 4.1 핵심 원칙: **YAML 없음, 구조 없음, 그냥 글**

비개발자에게 frontmatter는 이질적. 대신:

```markdown
# 나에 대해

저는 프리랜서 번역가입니다. 영어↔한국어 전문.
주로 IT/마케팅 분야 문서를 번역합니다.

## 선호
- 번역체 싫음, 자연스러운 한국어 선호
- 외래어는 최소화
- 존댓말 사용

## 현재 상황
- 5월 마감 프로젝트 2건 진행 중
- 화~목 집중 작업, 월/금은 미팅
```

**기계가 읽을 메타데이터는 AI가 자동 생성하여 별도 저장** (사용자 파일에 노출 안 함):

```json
// .knowledge/.meta/about-me.json (숨김 파일, 사용자 안 봄)
{
  "created": "2026-05-04",
  "updated": "2026-05-04",
  "type": "profile",
  "keywords": ["번역가", "프리랜서", "영한", "IT", "마케팅"],
  "token_count": 156
}
```

### 4.2 각 카테고리별 예시

#### about-me.md (내 소개)

```markdown
# 나에 대해

## 나는 누구
[직업/역할/상황 자유 서술]

## 이렇게 대해줘
[말투, 스타일, 분량 선호]

## 지금 관심 있는 것
[현재 관심사, 목표]
```

#### rules.md (AI 규칙)

```markdown
# 규칙

## 꼭 지켜줘
- 답변은 항상 한국어로
- 3줄 이상이면 번호 매겨줘
- 건강 관련 조언은 "의사와 상담하세요" 붙여줘

## 하지 마
- 이모지 쓰지 마
- "물론이죠!" 같은 과한 친절 하지 마
- 내 글 고칠 때 원문 의미 바꾸지 마
```

#### ongoing/house-hunting.md (진행 중인 일)

```markdown
# 집 찾기

시작: 2026년 4월

## 조건
- 서울 마포/용산/성동
- 전세 3억 이하
- 역 도보 10분
- 층간소음 적은 구조 (필로티 X)

## 지금까지 본 곳
- 마포 A 오피스텔: 좁음, 탈락
- 용산 B 아파트: 좋은데 예산 초과
- 성동 C 빌라: 다음주 재방문 예정

## 다음 단계
- C 빌라 재방문 (5/8)
- 전세자금대출 한도 확인
```

#### learned/tax-deductions.md (배운 것)

```markdown
# 프리랜서 세금 공제 정리

2026년 5월 대화에서 정리한 내용.

## 경비 인정 항목
- 노트북, 모니터 등 장비: 감가상각 4년
- 인터넷/통신비: 업무 비율만큼
- 코워킹 스페이스: 전액 경비
- 도서구입비: 업무 관련이면 OK

## 주의사항
- 간이과세자는 매입세액공제 안 됨
- 홈오피스 비용은 전용 공간 있을 때만
```

#### templates/email-reply.md (재사용 패턴)

```markdown
# 이메일 답장 템플릿

클라이언트 이메일에 답장할 때 이 형식으로 써줘:

1. 감사 인사 (1문장)
2. 핵심 답변 (3문장 이내)
3. 다음 단계 제안 (있으면)
4. 마무리 인사

톤: 전문적이지만 딱딱하지 않게.
길이: 최대 10줄.
```

---

## 5. Onboarding Flow (첫 설정)

### 5.1 대화형 온보딩 (CoWork OS 패턴 참고)

```
┌─────────────────────────────────────────────────────┐
│ 👋 안녕하세요! AI와의 대화를 더 효율적으로            │
│    만들어볼까요?                                     │
│                                                     │
│ 몇 가지 질문에 답하면, AI가 당신을 기억하는           │
│ 프로필을 만들어드립니다. (2분 소요)                    │
│                                                     │
│ [시작하기]  [나중에]                                  │
└─────────────────────────────────────────────────────┘
```

### 5.2 온보딩 질문 (5개)

1. "어떤 일을 하시나요?" → about-me.md 역할 섹션
2. "AI한테 어떤 말투로 대답받고 싶으세요?" → about-me.md 선호 섹션
3. "AI가 절대 하면 안 되는 것 있나요?" → rules.md
4. "지금 진행 중인 일이 있나요?" → ongoing/ 첫 파일
5. "답변 언어는?" → rules.md 언어 설정

### 5.3 결과

온보딩 완료 시 자동 생성:
```
.knowledge/
├── about-me.md     (질문 1,2 기반)
├── rules.md        (질문 3,5 기반)
└── ongoing/
    └── [project].md  (질문 4 기반, 있으면)
```

---

## 6. Auto-extraction (자동 추출)

### 6.1 Philosophy: "AI가 정리해줌"

비개발자는 직접 `.knowledge/`에 파일을 만들지 않음.
**AI가 대화 중에 저장할 만한 것을 감지하고 제안함.**

### 6.2 추출 트리거

| 대화 패턴 | 추출 타입 | 저장 위치 |
|-----------|-----------|-----------|
| "나는 ~이야", "내 직업은~" | 프로필 정보 | about-me.md |
| "이건 항상 ~해줘", "~하지 마" | 규칙 | rules.md |
| "~하기로 했어", "~로 결정" | 결정 | decisions/ 또는 ongoing/ |
| "오늘 알게 된 건~", "정리하면~" | 학습 | learned/ |
| "다 했어", "이제 끝" | 완료 | → archive/ 이동 제안 |
| 3회 이상 같은 컨텍스트 반복 | 누락 메모리 | 해당 카테고리 제안 |

### 6.3 제안 UX (세션 중)

```
┌─────────────────────────────────────────────────────┐
│ 💾 이 내용을 기억해둘까요?                           │
│                                                     │
│ "프리랜서 세금 경비 인정 항목"                        │
│                                                     │
│ [저장] [다듬어서 저장] [안 할래]                      │
└─────────────────────────────────────────────────────┘
```

### 6.4 제안 UX (세션 종료 시)

```
┌─────────────────────────────────────────────────────┐
│ 📝 이번 대화에서 기억할 만한 것:                      │
│                                                     │
│ • 결정: "C 빌라 재방문하기로 함"                      │
│ • 학습: "필로티 구조 = 층간소음 취약"                  │
│ • 규칙: "예산 3억 넘으면 알려달라고 함"                │
│                                                     │
│ [전부 저장] [골라서 저장] [건너뛰기]                   │
└─────────────────────────────────────────────────────┘
```

---

## 7. Context Injection (자동 주입)

### 7.1 세션 시작 시

```
사용자가 새 대화 시작
      ↓
llm-relay가 .knowledge/ 스캔
      ↓
자동 주입:
  1. about-me.md (항상)
  2. rules.md (항상)
  3. ongoing/ 중 최근 수정된 것 (상위 3개)
      ↓
AI는 "이미 아는 상태"로 응답
```

### 7.2 토큰 예산

| 우선순위 | 내용 | 예산 |
|----------|------|------|
| 1 (필수) | about-me.md + rules.md | 500 tokens |
| 2 (자동) | 최근 ongoing 3건 | 800 tokens |
| 3 (관련) | 프롬프트 키워드 매칭 learned/ | 700 tokens |
| **합계** | | **2,000 tokens** (설정 가능) |

### 7.3 키워드 매칭 (간단 구현)

```
사용자: "세금 신고 준비해야 하는데"
      ↓
키워드 "세금" → learned/tax-deductions.md 매칭
      ↓
해당 파일 내용 주입
      ↓
AI: "이전에 정리하신 경비 인정 항목 기준으로..."
```

---

## 8. Web UI (8083)

### 8.1 General Profile Dashboard

```
┌────────────────────────────────────────────────────────┐
│  🧠 내 지식                                    [설정]  │
├────────────────────────────────────────────────────────┤
│                                                        │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐        │
│  │나    │ │진행중│ │배운것│ │규칙  │ │보관함│        │
│  │소개  │ │  3건 │ │  7건 │ │  5줄 │ │  2건 │        │
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘        │
│                                                        │
│  📊 현황                                               │
│  ├─ 총 저장된 지식: 12건                                │
│  ├─ 이번 주 새로 배운 것: 3건                            │
│  ├─ 마지막 업데이트: 2시간 전                            │
│  └─ AI가 당신을 아는 정도: ████████░░ 80%               │
│                                                        │
│  💡 제안                                               │
│  • "집 찾기"가 3주째 진행 중입니다. 업데이트할까요?         │
│  • 지난 대화에서 저장 안 한 정보 2건이 있습니다            │
│                                                        │
└────────────────────────────────────────────────────────┘
```

### 8.2 파일 편집 UI

```
┌────────────────────────────────────────────────────────┐
│  📋 진행 중인 일 > 집 찾기                     [편집]  │
├────────────────────────────────────────────────────────┤
│                                                        │
│  시작: 2026년 4월                                      │
│                                                        │
│  ■ 조건                                               │
│  • 서울 마포/용산/성동                                  │
│  • 전세 3억 이하                                       │
│  • 역 도보 10분                                        │
│                                                        │
│  ■ 본 곳                                              │
│  • 마포 A 오피스텔: 좁음 ❌                             │
│  • 용산 B 아파트: 예산 초과 ❌                           │
│  • 성동 C 빌라: 재방문 예정 ⏳                          │
│                                                        │
│  ┌─────────────────────────────────────┐               │
│  │ [완료 처리] [편집] [삭제]           │               │
│  └─────────────────────────────────────┘               │
└────────────────────────────────────────────────────────┘
```

### 8.3 타임라인 뷰

```
┌────────────────────────────────────────────────────────┐
│  📅 타임라인                                           │
├────────────────────────────────────────────────────────┤
│                                                        │
│  5월 4일                                               │
│  ├─ 💡 배운 것: "프리랜서 세금 공제 정리"               │
│  └─ 📌 규칙 추가: "예산 초과하면 알려줘"                │
│                                                        │
│  5월 2일                                               │
│  ├─ 📋 진행: "집 찾기" 업데이트 (C빌라 추가)            │
│  └─ 💡 배운 것: "필로티 = 층간소음 위험"                │
│                                                        │
│  4월 28일                                              │
│  └─ 📋 시작: "집 찾기"                                 │
│                                                        │
└────────────────────────────────────────────────────────┘
```

---

## 9. CLI Interface

### 9.1 비개발자도 쓸 수 있는 명령어 (최소한)

```bash
# 첫 설정 (대화형) — 아래 두 명령은 동일
llm-relay start                    # 사용자 친화적 alias
llm-relay init --profile general   # 명시적 (Coding Profile과 통일)

# 내 지식 보기 (웹 브라우저 열림)
llm-relay knowledge

# 뭔가 기억시키기
llm-relay remember "나는 채식주의자야"

# 검색
llm-relay recall "세금"
```

> **CLI 통일 원칙**: `llm-relay init --profile {coding|general}` 이 정식 명령.
> `llm-relay start`는 `init --profile general`의 alias.

### 9.2 Power User 명령어

```bash
# 전체 목록
llm-relay knowledge list
llm-relay knowledge list --ongoing
llm-relay knowledge list --learned

# 특정 파일 보기/편집
llm-relay knowledge show house-hunting
llm-relay knowledge edit house-hunting

# 건강 체크
llm-relay knowledge health

# 아카이브
llm-relay knowledge archive house-hunting

# 내보내기
llm-relay knowledge export --format html
llm-relay knowledge export --format pdf
```

---

## 10. Cross-tool Behavior

### 10.1 어디서든 동작

| CLI Tool | .knowledge/ 읽는 방법 |
|----------|---------------------|
| Claude Code | INSTRUCTIONS.md에서 `.knowledge/` 참조 지시 |
| Codex | `~/.codex/memories/`에 심링크 또는 복사 |
| Gemini | GEMINI.md에서 참조 |
| ChatGPT | Web UI에서 수동 붙여넣기 (export 기능) |
| 로컬 LLM | 시스템 프롬프트에 자동 주입 |

### 10.2 INSTRUCTIONS.md (General Profile 버전)

```markdown
# Instructions

이 폴더의 `.knowledge/`에 나에 대한 정보와 규칙이 있습니다.
새 대화를 시작할 때 반드시 참조하세요.

## 핵심 파일
- `.knowledge/about-me.md` — 내가 누구인지
- `.knowledge/rules.md` — 지켜야 할 규칙

## 행동 원칙
- 이전 대화에서 이미 말한 것은 다시 물어보지 마세요
- 새로 알게 된 중요한 정보는 저장 제안하세요
- 진행 중인 일이 끝나면 완료 처리 제안하세요
```

---

## 11. Lifecycle & Health

### 11.1 자동 관리

| 이벤트 | 시스템 행동 |
|--------|-------------|
| ongoing/ 항목 30일 미갱신 | "아직 진행 중인가요?" 알림 |
| about-me.md 60일 미갱신 | "프로필 최신인가요?" 알림 |
| learned/ 파일 100개 초과 | 주제별 병합 제안 |
| 세션에서 rules.md 위반 감지 | "규칙 업데이트할까요?" 제안 |

### 11.2 Health Score (웹 UI)

```
AI가 당신을 아는 정도: ████████░░ 80%

개선 가능:
  • "직업" 정보는 있지만 "취미"가 없어요 (+5%)
  • 진행 중인 일 2건의 상태가 오래됐어요 (+10%)
  • 규칙이 아직 2개뿐이에요 (+5%)
```

---

## 11.3 Configuration (`.llm-relay.yaml`)

General Profile 사용자는 직접 설정할 필요 없음 (기본값으로 동작).
Power User가 커스터마이징할 경우:

```yaml
knowledge:
  enabled: true
  profile: general
  auto_suggest: true          # 세션 종료 시 저장 제안
  auto_inject: true           # 세션 시작 시 자동 주입
  inject_budget_tokens: 2000  # 주입 토큰 예산
  health_check_interval: 30d  # 건강 체크 주기 (coding=7d, general=30d)
  language: auto              # auto = 시스템 로케일
  
  retention:
    ongoing: warn-after-30d
    learned: warn-after-180d
    rules: permanent
    about-me: warn-after-60d
```

> **Coding Profile과의 차이**: health_check_interval이 더 느슨(30d vs 7d),
> 타입 이름이 사용자 친화적(ongoing vs project, learned vs feedback+learned).

---

## 12. Privacy & Security

### 12.1 원칙

| 원칙 | 구현 |
|------|------|
| **로컬 전용** | .knowledge/는 사용자 기기에만 존재 |
| **클라우드 안 감** | 동기화/백업은 사용자가 선택 (iCloud, Dropbox 등) |
| **삭제 = 즉시 삭제** | 아카이브 아닌 삭제 요청 시 파일 완전 제거 |
| **민감 정보 경고** | 비밀번호/카드번호 등 저장 시도 시 차단 |
| **열어볼 수 있음** | 모든 파일은 텍스트 에디터로 직접 확인 가능 |

### 12.2 민감 정보 패턴 감지

```
저장 시도: "신용카드 번호는 1234-5678-..."
      ↓
⚠️ "민감한 금융 정보가 포함되어 있습니다.
    .knowledge/에 저장하면 다른 AI 세션에서도
    접근할 수 있습니다. 정말 저장할까요?"
      ↓
[저장 안 함 (권장)]  [그래도 저장]
```

---

## 13. Comparison with Coding Profile

| | Coding Profile | General Profile |
|---|---|---|
| 대상 | CLI 쓰는 개발자 | 누구나 |
| 파일 형식 | YAML frontmatter + 구조 | 순수 마크다운 (메타는 숨김) |
| 초기 파일 수 | 4개 (INSTRUCTIONS.md + INDEX.md + me.md + decisions.md) | 2개 + INSTRUCTIONS.md (about-me.md + rules.md) |
| 카테고리 | type별 prefix (feedback_, project_...) | 사람 말 폴더 (진행중, 배운것...) |
| 인덱스 | INDEX.md 명시적 관리 | 자동 (숨김 .meta/) |
| 온보딩 | `init --profile coding` + 수동 작성 | 대화형 질문 5개 |
| 추출 | 사용자 주도 ("기억해") | AI 주도 (자동 감지 + 제안) |
| 주입 | keyword/tag 매칭 | 전체 로드 (작으니까) |
| Cross-tool | 심링크 기반 | INSTRUCTIONS.md 참조 |

---

## 14. Migration Paths

### 14.1 ChatGPT Memory → .knowledge/

```bash
llm-relay migrate --from chatgpt-memory
# ChatGPT 설정에서 내보낸 memory.json 파싱
# → about-me.md (선호/사실)
# → rules.md (행동 지시)
# → learned/ (나머지)
```

### 14.2 Notion → .knowledge/

```bash
llm-relay migrate --from notion --export-path ./notion-export/
# Notion 마크다운 내보내기 파싱
# 페이지 → 적절 카테고리 매핑
```

### 14.3 Obsidian Vault → .knowledge/

```bash
llm-relay migrate --from obsidian --vault-path ~/my-vault/
# 볼트의 AI 관련 노트만 선별 추출
```

---

## 15. Implementation Phases

> **Note**: Coding Profile과 동일 Phase 번호 체계 사용.
> 공통 인프라(Phase 1, 5)는 한 번만 구현.

### Phase 1: Scaffold (MVP) — Coding Profile과 공유
- `llm-relay init --profile general` (alias: `llm-relay start`)
- .knowledge/ 디렉토리 + about-me.md + rules.md 생성
- INSTRUCTIONS.md + 심링크 생성
- 대화형 온보딩 5개 질문

### Phase 2: Health & Management — Coding Profile과 공유
- `knowledge health` / `knowledge list`
- Stale 감지 + archive 제안

### Phase 3: Detection & Suggestion + Auto-extraction
- 구조 미존재 시 제안 UX
- 대화 중 저장 가능 정보 감지 (General 전용: AI 주도)
- 세션 종료 시 저장 제안 UX
- ongoing/ 파일 자동 업데이트 제안
- 반복 컨텍스트 감지

### Phase 4: Auto-injection — Coding Profile과 공유
- 세션 시작 시 .knowledge/ 자동 주입
- 토큰 예산 관리 (기본 2000)
- 키워드 매칭 엔진

### Phase 5: Web UI — Coding Profile과 공유
- `/knowledge` 대시보드 (프로필에 따라 다른 뷰)
- 파일 브라우저 + 편집기 + 타임라인
- Health score

### Phase 6: Smart Features (General 전용)
- 반복 패턴 감지 ("이거 templates/로 만들까요?")
- 관련 지식 proactive surfacing
- 다중 사용자 프로필 (업무/개인 분리)

---

## 16. Differentiation (경쟁 제품 대비)

| | ChatGPT Memory | Mem.ai | Notion AI | **llm-relay knowledge (General)** |
|---|---|---|---|---|
| 가격 | 무료 (제한적) | $8-12/월 | $10+/월 | **무료 (오픈소스)** |
| 저장 위치 | OpenAI 서버 | 클라우드 | 클라우드 | **내 컴퓨터** |
| LLM 종속 | ChatGPT만 | 자체 AI | Notion만 | **아무 LLM** |
| 구조 | 플랫 문장 목록 | AI 자동 분류 | DB+페이지 | **폴더+마크다운** |
| 내보내기 | 제한적 | 가능 | 마크다운 | **이미 마크다운** |
| 투명성 | 블랙박스 | 반투명 | 보임 | **100% 텍스트 파일** |
| 오프라인 | 불가 | 불가 | 불가 | **완전 가능** |

### 핵심 차별점

> **"당신의 AI 기억은 당신 컴퓨터의 텍스트 파일입니다.
> 잠금 없고, 구독 없고, 열어보면 그냥 글입니다."**

---

## Appendix A: 비개발자 용어 매핑

| 기술 용어 (내부) | 사용자에게 보이는 말 |
|------------------|---------------------|
| knowledge store | 내 지식 |
| auto-extraction | AI가 기억해두기 |
| context injection | AI가 미리 읽어오기 |
| frontmatter/metadata | (보이지 않음) |
| archive | 보관함 |
| health check | 정리 제안 |
| token budget | (보이지 않음) |
| INDEX.md | (보이지 않음) |
| symlink | (보이지 않음) |

## Appendix B: fabric 패턴 활용

fabric의 200+ 패턴 중 비개발자에게 유용한 것을 templates/로 포팅:

| fabric 패턴 | General Profile 템플릿 |
|-------------|----------------------|
| summarize | templates/요약해줘.md |
| extract_wisdom | templates/핵심만-뽑아줘.md |
| write_essay | templates/글-써줘.md |
| analyze_claims | templates/팩트체크.md |
| create_meeting_summary | templates/회의록.md |
| rate_content | templates/평가해줘.md |

## Appendix C: PARA → General Profile 매핑

| PARA | General Profile | 설명 |
|------|-----------------|------|
| Projects | ongoing/ | 마감 있는 진행 중인 일 |
| Areas | about-me.md + rules.md | 계속 유지하는 책임/기준 |
| Resources | learned/ + templates/ | 참고할 수 있는 지식 |
| Archives | archive/ | 끝났지만 찾아볼 수 있는 것 |
