# llm-relay 로컬 소스코드 백업

## 구조

```
Layer 1: ~/.llm-relay/backups/          ← 로컬 디스크 (7일 보관)
Layer 2: dgx:~/backups/llm-relay/       ← DGX1 원격 (14일 보관)
Layer 3: dgx2:~/backups/llm-relay/      ← DGX2 원격 (14일 보관)
```

## 사용법

```bash
# 수동 백업 (3층 전체)
./scripts/backup-local.sh

# 로컬만 빠르게
./scripts/backup-local.sh local
```

## Cron 등록 (선택)

```bash
# 매일 04:17 자동 백업
(crontab -l; echo "17 4 * * * /home/hmjhp/GitHub/llm-relay/scripts/backup-local.sh >> /home/hmjhp/.llm-relay/backup.log 2>&1") | crontab -
```

## 복구 방법

```bash
# 1. 로컬 bundle에서 복구
git clone ~/.llm-relay/backups/llm-relay-YYYYMMDD-HHMMSS.bundle llm-relay-restored

# 2. DGX1에서 가져와 복구
scp dgx:~/backups/llm-relay/llm-relay-YYYYMMDD-HHMMSS.bundle /tmp/
git clone /tmp/llm-relay-YYYYMMDD-HHMMSS.bundle llm-relay-restored

# 3. 기존 레포에 bundle 반영 (브랜치 복구)
git bundle verify /path/to/bundle.bundle
git fetch /path/to/bundle.bundle main:restored-main
```

## 백업 대상

git bundle --all: 모든 브랜치 + 태그 + stash 포함.
.env.local, usage.db 등 gitignore 파일은 미포함 — 별도 관리 필요.
