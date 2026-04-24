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
        "zone.abs.yellow": "Reached {n}K. Prepare to rotate.",

        # Zone messages — ratio
        "zone.ratio.hard": "100% ({n}K) ceiling reached. Immediate session cleanup.",
        "zone.ratio.red": "90% ({n}K) reached. Rotation required.",
        "zone.ratio.orange": "70% ({n}K) reached. Finish then rotate.",
        "zone.ratio.yellow": "50% ({n}K) reached. Prepare to rotate.",

        # UI strings
        "ui.no_active_sessions": "No active sessions",
        "ui.no_prompt": "(no prompt)",
        "ui.no_history": "No session history recorded yet.",
        "ui.enable_history": "Enable with LLM_RELAY_HISTORY=1",
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

        "zone.ratio.hard": "100% ({n}K) 천장 도달. 즉시 세션 정리.",
        "zone.ratio.red": "90% ({n}K) 도달. 로테이션 필수.",
        "zone.ratio.orange": "70% ({n}K) 도달. 마무리 후 rotate.",
        "zone.ratio.yellow": "50% ({n}K) 도달. rotate 준비.",

        "ui.no_active_sessions": "활성 세션 없음",
        "ui.no_prompt": "(프롬프트 없음)",
        "ui.no_history": "세션 기록이 없습니다.",
        "ui.enable_history": "LLM_RELAY_HISTORY=1로 활성화하세요",
    },
}

_lang = os.getenv("LLM_RELAY_LANG", "en")


def get_lang():
    return _lang


def t(key, lang=None, **kwargs):
    """Translate a message key. Falls back to English if key not found."""
    locale = lang or _lang
    msgs = MESSAGES.get(locale, MESSAGES["en"])
    msg = msgs.get(key, MESSAGES["en"].get(key, key))
    if kwargs:
        return msg.format(**kwargs)
    return msg
