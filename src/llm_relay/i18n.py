"""Lightweight i18n — message dict with en/ko locales.

Usage (Python):
    from llm_relay.i18n import t
    t("zone.danger")      # returns English or Korean based on LLM_RELAY_LANG
    t("zone.danger", "ko") # force Korean

Usage (JS): import messages from /api/v1/i18n?lang=ko (or auto-detect)
"""

import os

MESSAGES = {
    "en": {
        # Zone labels
        "zone.safe": "safe",
        "zone.caution": "caution",
        "zone.warning": "warning",
        "zone.danger": "danger",
        "zone.blocked": "blocked",

        # Zone messages — turn-based
        "zone.turn.red": "Exceeded {n} turns. Quality degradation likely. Switch to a new session.",
        "zone.turn.orange": "Reached {n} turns. Approaching quality degradation. Rotation recommended.",
        "zone.turn.yellow": "Reached {n} turns. Prepare a new session.",

        # Zone messages — absolute tokens
        "zone.abs.hard": "Exceeded {n}K. Immediate session cleanup required.",
        "zone.abs.red": "Reached {n}K. Session rotation required.",
        "zone.abs.orange": "Reached {n}K. Finish current work then rotate.",
        "zone.abs.yellow": "Reached {n}K. Update docs and prepare to rotate.",

        # Zone messages — ratio (pct-aware format)
        "zone.ratio.hard": "{pct}% ({cur}K/{ceil}K) ceiling reached. Immediate session cleanup.",
        "zone.ratio.red": "{pct}% ({cur}K/{ceil}K) reached. Rotation required.",
        "zone.ratio.orange": "{pct}% ({cur}K/{ceil}K) reached. Finish then rotate.",
        "zone.ratio.yellow": "{pct}% ({cur}K/{ceil}K) reached. Prepare to rotate.",

        # UI strings
        "ui.no_active_sessions": "No active sessions",
        "ui.no_prompt": "(No prompt)",
        "ui.no_history": "No session history recorded yet.",
        "ui.enable_history": "Enable with LLM_RELAY_HISTORY=1",

        # Composition tooltips
        "ui.comp.user_text": "Percentage of context occupied by user-input prompt text",
        "ui.comp.assistant_text": "Percentage of model-generated response text",
        "ui.comp.tool_use": "Percentage of tool-call definitions (Read, Bash, Edit, etc.)",
        "ui.comp.tool_result": (
            "Percentage of tool execution results (file contents, grep output, etc.)."
            " Higher = more context noise."
        ),
        "ui.comp.thinking_overhead": "Model-internal reasoning (thinking) blocks + signature overhead",
        "ui.comp.snr": (
            "Signal-to-Noise Ratio. (User+Asst) / (Result+Think)."
            " 1.0+ is ideal; below 0.5 is a warning."
        ),
        "ui.comp.dupes": (
            "Number of times the same file was Read 2+ times."
            " Re-reading after compaction is the main cause."
        ),
    },
    "ko": {
        "zone.safe": "안전",
        "zone.caution": "주의",
        "zone.warning": "경고",
        "zone.danger": "위험",
        "zone.blocked": "차단",

        "zone.turn.red": "{n}턴 초과. 품질 저하 가능성이 높습니다. 새 세션으로 전환하세요.",
        "zone.turn.orange": "{n}턴 도달. 품질 저하 구간 진입 임박. 로테이션을 권장합니다.",
        "zone.turn.yellow": "{n}턴 도달. 새 세션 준비를 권장합니다.",

        "zone.abs.hard": "{n}K 초과. 즉시 세션 정리 필요.",
        "zone.abs.red": "{n}K 도달. 세션 로테이션 필수.",
        "zone.abs.orange": "{n}K 도달. 현재 작업 마무리 후 rotate.",
        "zone.abs.yellow": "{n}K 도달. 문서 업데이트 + rotate 준비.",

        "zone.ratio.hard": "{pct}% ({cur}K/{ceil}K) 천장 도달. 즉시 세션 정리.",
        "zone.ratio.red": "{pct}% ({cur}K/{ceil}K) 도달. 로테이션 필수.",
        "zone.ratio.orange": "{pct}% ({cur}K/{ceil}K) 도달. 마무리 후 rotate.",
        "zone.ratio.yellow": "{pct}% ({cur}K/{ceil}K) 도달. rotate 준비.",

        "ui.no_active_sessions": "활성 세션 없음",
        "ui.no_prompt": "(프롬프트 없음)",
        "ui.no_history": "세션 기록이 없습니다.",
        "ui.enable_history": "LLM_RELAY_HISTORY=1로 활성화하세요",

        "ui.comp.user_text": "사용자가 입력한 프롬프트 텍스트가 차지하는 비율",
        "ui.comp.assistant_text": "모델이 생성한 응답 텍스트 비율",
        "ui.comp.tool_use": "도구 호출 정의(Read, Bash, Edit 등) 비율",
        "ui.comp.tool_result": "도구 실행 결과(파일 내용, grep 출력 등) 비율. 높을수록 컨텍스트 오염",
        "ui.comp.thinking_overhead": "모델 내부 추론(thinking) 블록 + 서명 오버헤드",
        "ui.comp.snr": "Signal-to-Noise Ratio. (User+Asst) / (Result+Think). 1.0 이상이 이상적, 0.5 미만은 경고",
        "ui.comp.dupes": "같은 파일을 2회 이상 Read한 횟수. compaction 후 재읽기가 주요 원인",
    },
}

_lang = os.getenv("LLM_RELAY_LANG", "en")


def get_lang():
    """Return the current server locale."""
    return _lang


def t(key, lang=None, **kwargs):
    """Translate a message key. Falls back to English if key not found."""
    locale = lang or _lang
    msgs = MESSAGES.get(locale, MESSAGES["en"])
    msg = msgs.get(key, MESSAGES["en"].get(key, key))
    if kwargs:
        return msg.format(**kwargs)
    return msg
