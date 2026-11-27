"""Parse Antigravity CLI (agy) Code Assist traffic into the relay's stored format.

agy talks to Google's Code Assist backend (``/v1internal:*``). The functions here
turn what passes through the relay into plain values the store can write:

* ``parse_request`` -- a ``streamGenerateContent`` request body (``{"project",
  "requestId", "request": {...}, "model", "requestType", ...}``) into an
  ``AgyRequest`` carrying the conversation identity agy puts in
  ``request.labels`` and ``requestId``.
* ``normalize_contents`` -- Gemini ``contents`` into messages
  ``{"role": "user"|"assistant", "content": [block, ...]}`` with Anthropic-style
  blocks (``text``, ``thinking``, ``tool_use``, ``tool_result``, ...).
* ``SSEAccumulator`` / ``parse_json_response`` -- a streamed (or plain JSON)
  response into an ``AgyResponse``: merged text / tool_use blocks, thinking
  blocks, the last cumulative ``usageMetadata``, the finish reason, the other
  keys of candidate 0 (``extra``) and, with a clock, when the first thought and
  the first answer part arrived.
* ``last_user_prompt``, ``first_message_fingerprint``, ``usage_columns`` and
  ``parse_quota_summary`` -- the derived columns.
* ``content_hash`` / ``parse_json_value`` -- the sha256 and size of a JSON value
  in one canonical serialization, and a JSON body decoded as is (for the blob
  store, which keeps each distinct system prompt / tool list / snapshot once).

Role mapping: Gemini ``"model"`` becomes ``"assistant"`` and every other role
(or a missing one) becomes ``"user"``, except that a content whose parts are all
``functionResponse`` is always ``"user"``. agy sends tool results with role
``"model"``, but in the stored format a tool result is input to the model, as
in the Anthropic messages API. ``thoughtSignature`` is always dropped, and an
inline base64 payload is never kept -- only its size, the same rule as
``proxy.history`` (2026-09-29).

Never-raise contract: this module runs inside the proxy path, where a parsing
bug must not break forwarding. Every public function and method catches its own
errors and returns a neutral value instead -- ``None``, ``[]``, all-zero usage
columns, or an ``AgyResponse`` holding whatever was parsed so far. stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("llm-relay.antigravity.wire")

# Upper bound for AgyResponse.extra in canonical JSON bytes; a larger value is replaced by its size.
EXTRA_MAX_BYTES = 1024 * 1024
# Candidate keys that already have their own place in AgyResponse; every other key goes to extra.
_CANDIDATE_OWN_KEYS = frozenset({"content", "finishReason", "index"})
_EVENT_METADATA_KEY = "_event_metadata"

# agy wraps the typed prompt in these tags and appends <ADDITIONAL_METADATA>.
_USER_REQUEST_OPEN = "<USER_REQUEST>"
_USER_REQUEST_CLOSE = "</USER_REQUEST>"
# Harness notices are sent as user contents but were not typed by the user.
_HARNESS_NOTICE_PREFIX = "The following is a <SYSTEM_MESSAGE>"
_INT_RE = re.compile(r"[+-]?[0-9]+")
_BOM = "\ufeff"


@dataclass
class AgyRequest:
    method: str  # as passed in, e.g. "streamGenerateContent"
    request_type: Optional[str]
    request_id: Optional[str]
    conversation_id: Optional[str]  # labels.cascade_id, else 2nd segment of "agent/<id>/..."; None otherwise
    root_conversation_id: Optional[str]  # labels.root_cascade_id
    trajectory_id: Optional[str]
    step_index: Optional[int]  # int(labels.last_step_index) when numeric
    model: Optional[str]
    thinking_level: Optional[str]
    client_session_id: Optional[str]  # str(request.sessionId)
    project: Optional[str]
    message_count: int  # len(contents)
    system_chars: int  # total chars of systemInstruction text parts
    tools_count: int  # total functionDeclarations
    contents: List[Dict[str, Any]]  # the raw contents list (same objects, not copied)
    system_instruction: Any = None  # request.systemInstruction as sent (same object, not copied)
    tools: Any = None  # request.tools as sent (same object, not copied)


@dataclass
class AgyResponse:
    blocks: List[Dict[str, Any]]  # assistant content blocks in order: text and tool_use
    thinking: List[Dict[str, Any]]  # thinking blocks in order
    usage: Optional[Dict[str, Any]]  # last usageMetadata seen, raw
    model_version: Optional[str]
    finish_reason: Optional[str]  # last non-empty finishReason
    response_id: Optional[str]
    trace_id: Optional[str]
    n_events: int  # parsed data events
    n_bad_events: int  # data lines that were not valid JSON objects
    error: Optional[str]  # "<status|code>: <message>" from an {"error": {...}} payload, else None
    response_bytes: int  # total bytes fed
    # Candidate-0 keys other than content / finishReason / index, last non-empty value of each, plus
    # "_event_metadata" (the last non-empty top-level event metadata); {"_omitted_bytes": n} above
    # EXTRA_MAX_BYTES.
    extra: Dict[str, Any] = field(default_factory=dict)
    first_thought_at: Optional[float] = None  # clock() at the first thinking part; None without a clock
    first_answer_at: Optional[float] = None  # clock() at the first other kept part (text, tool_use, ...)


# ── Small coercion helpers (all total: they never raise on JSON-shaped input) ──


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _nonempty_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _as_bytes(body: Any) -> Optional[bytes]:
    if isinstance(body, bytes):
        return body
    if isinstance(body, (bytearray, memoryview)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8", "replace")
    return None


def _loads(body: Any) -> Any:
    """Decode a JSON body; invalid UTF-8 is replaced rather than rejected. Raises on bad JSON."""
    data = _as_bytes(body)
    if data is None:
        raise ValueError("body is not bytes")
    return json.loads(data.decode("utf-8", "replace").lstrip(_BOM))


def _canonical_json(obj: Any) -> bytes:
    """The one serialization behind content_hash and the extra size bound. Raises on unserializable input.

    Sorted keys and no whitespace make equal values hash alike whatever their key
    order; "replace" turns a lone surrogate into "?" instead of failing.
    """
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return text.encode("utf-8", "replace")


def _is_empty(value: Any) -> bool:
    """None and empty containers / strings carry nothing worth keeping; False and 0 do."""
    return value is None or (isinstance(value, (dict, list, str)) and not value)


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _count(value: Any) -> int:
    """A token count: ints, finite floats and integer strings; anything else is 0."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else 0
    if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return 0


# ── Requests ──


def _conversation_id(labels: Dict[str, Any], request_id: Optional[str]) -> Optional[str]:
    cid = _nonempty_str(labels.get("cascade_id"))
    if cid is not None:
        return cid
    # requestId = agent/<conversation_id>/<ts_ms>/<trajectory_id>/<n>; the side call
    # "agent_monitoring/<uuid>" has no conversation.
    if request_id is not None:
        segments = request_id.split("/")
        if len(segments) >= 2 and segments[0] == "agent" and segments[1]:
            return segments[1]
    return None


def _session_id(value: Any) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, int):
        return str(value)
    return None


def _system_chars(system_instruction: Any) -> int:
    if isinstance(system_instruction, str):
        return len(system_instruction)
    total = 0
    for part in _list(_dict(system_instruction).get("parts")):
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            total += len(part["text"])
    return total


def _tools_count(tools: Any) -> int:
    return sum(len(_list(tool.get("functionDeclarations"))) for tool in _list(tools) if isinstance(tool, dict))


def parse_request(body: bytes, method: str) -> Optional[AgyRequest]:
    """Parse a Code Assist request body; None unless the body is a JSON object."""
    try:
        obj = _loads(body)
        if not isinstance(obj, dict):
            return None
        request = _dict(obj.get("request"))
        labels = _dict(request.get("labels"))
        request_id = _str(obj.get("requestId"))
        contents = request.get("contents")
        if not isinstance(contents, list):
            contents = []
        thinking_config = _dict(_dict(request.get("generationConfig")).get("thinkingConfig"))
        system_instruction = request.get("systemInstruction")
        tools = request.get("tools")
        return AgyRequest(
            method=method,
            request_type=_str(obj.get("requestType")),
            request_id=request_id,
            conversation_id=_conversation_id(labels, request_id),
            root_conversation_id=_nonempty_str(labels.get("root_cascade_id")),
            trajectory_id=_nonempty_str(labels.get("trajectory_id")),
            step_index=_int_or_none(labels.get("last_step_index")),
            model=_str(obj.get("model")),
            thinking_level=_str(thinking_config.get("thinkingLevel")),
            client_session_id=_session_id(request.get("sessionId")),
            project=_str(obj.get("project")),
            message_count=len(contents),
            system_chars=_system_chars(system_instruction),
            tools_count=_tools_count(tools),
            contents=contents,
            system_instruction=system_instruction,
            tools=tools,
        )
    except Exception:
        logger.debug("agy request parse failed", exc_info=True)
        return None


# ── Stored message format ──


def _part_to_block(part: Any) -> Optional[Dict[str, Any]]:
    """Map one Gemini part to a stored block; None = nothing to keep (empty text, signature only)."""
    if not isinstance(part, dict):
        return None
    call = part.get("functionCall")
    if isinstance(call, dict):
        return {"type": "tool_use", "id": call.get("id"), "name": call.get("name"), "input": call.get("args")}
    result = part.get("functionResponse")
    if isinstance(result, dict):
        return {
            "type": "tool_result",
            "tool_use_id": result.get("id"),
            "name": result.get("name"),
            "content": result.get("response"),
        }
    inline = part.get("inlineData")
    if isinstance(inline, dict):
        # The base64 payload is never kept: one image would bloat every stored turn.
        data = inline.get("data")
        size = len(data) if isinstance(data, (str, bytes)) else 0
        return {"type": "inline_data", "mime_type": inline.get("mimeType"), "omitted_bytes": size}
    file_data = part.get("fileData")
    if isinstance(file_data, dict):
        return {"type": "file", "mime_type": file_data.get("mimeType"), "uri": file_data.get("fileUri")}
    text = part.get("text")
    if isinstance(text, str):
        if not text:
            # Response streams end with {"text": "", "thoughtSignature": ...}; nothing to keep.
            return None
        if part.get("thought") is True:
            return {"type": "thinking", "thinking": text}
        return {"type": "text", "text": text}
    keys = sorted(str(key) for key in part if key != "thoughtSignature")
    if not keys:
        return None  # a bare thoughtSignature part
    return {"type": "unknown", "keys": keys}


def _dict_parts(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [part for part in _list(content.get("parts")) if isinstance(part, dict)]


def _is_tool_result_only(parts: List[Dict[str, Any]]) -> bool:
    return bool(parts) and all("functionResponse" in part for part in parts)


def _normalize_content(content: Any) -> Dict[str, Any]:
    try:
        if not isinstance(content, dict):
            return {"role": "user", "content": []}
        parts = _dict_parts(content)
        role = "assistant" if content.get("role") == "model" else "user"
        if _is_tool_result_only(parts):
            role = "user"
        blocks = [block for block in (_part_to_block(part) for part in parts) if block is not None]
        return {"role": role, "content": blocks}
    except Exception:
        logger.debug("agy content normalize failed", exc_info=True)
        return {"role": "user", "content": []}


def normalize_contents(contents: Any) -> List[Dict[str, Any]]:
    """One stored message per input content, same order and same length; [] for non-lists.

    Callers slice the result by raw index for delta storage, so a malformed
    content yields an empty user message instead of being skipped.
    """
    if not isinstance(contents, list):
        return []
    return [_normalize_content(content) for content in contents]


def extract_user_request(text: str) -> str:
    """Inner text of the first <USER_REQUEST>..</USER_REQUEST>, stripped; the stripped input when absent."""
    try:
        if not isinstance(text, str):
            return ""
        start = text.find(_USER_REQUEST_OPEN)
        if start >= 0:
            start += len(_USER_REQUEST_OPEN)
            end = text.find(_USER_REQUEST_CLOSE, start)
            if end >= 0:
                return text[start:end].strip()
        return text.strip()
    except Exception:
        return ""


def last_user_prompt(contents: Any) -> Optional[str]:
    """The latest prompt the user typed, or None.

    Scans from the end for a user content that is not tool-result-only and has a
    non-blank, non-thought text part that is not a harness notice; returns
    ``extract_user_request`` of the first such part.
    """
    try:
        if not isinstance(contents, list):
            return None
        for content in reversed(contents):
            if not isinstance(content, dict) or content.get("role") == "model":
                continue
            parts = _dict_parts(content)
            if _is_tool_result_only(parts):
                continue
            for part in parts:
                text = part.get("text")
                if not isinstance(text, str) or part.get("thought") is True or not text.strip():
                    continue
                if text.lstrip().startswith(_HARNESS_NOTICE_PREFIX):
                    continue
                return extract_user_request(text)
        return None
    except Exception:
        logger.debug("agy last prompt failed", exc_info=True)
        return None


def first_message_fingerprint(contents: Any) -> Optional[str]:
    """16 hex chars identifying a conversation by its first message; None for empty/non-list.

    agy resends the whole conversation on every request, so the first message
    stays the same for every call of one conversation (cf. proxy.history
    ``_conversation_key``).
    """
    try:
        if not isinstance(contents, list) or not contents:
            return None
        payload = json.dumps(normalize_contents(contents[:1]), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        logger.debug("agy fingerprint failed", exc_info=True)
        return None


# ── Responses ──


def _format_error(err: Any) -> str:
    if isinstance(err, dict):
        label = _nonempty_str(err.get("status"))
        if label is None:
            code = err.get("code")
            label = str(code) if code is not None else "error"
        message = err.get("message")
        return "{}: {}".format(label, message if message is not None else "")
    return "error: {}".format(err)


class SSEAccumulator:
    """Incremental parser for a ``streamGenerateContent?alt=sse`` response body.

    ``feed`` takes the body in arbitrary chunks (an event, a line or a multi-byte
    UTF-8 character may be split across them); ``finish`` flushes a final line
    without a trailing newline and returns the ``AgyResponse``. Lines are split
    on LF before decoding (LF never occurs inside a multi-byte UTF-8 sequence)
    and a trailing CR is stripped, so the CRLF CRLF event separators on the wire
    need no special handling.

    ``clock`` (e.g. ``time.monotonic``) is read when the first thinking part and
    the first other kept part are parsed, once each, so the caller can tell
    how long the model thought before it answered. Parts are parsed inside
    ``feed``, so the readings are as late as the chunk that completed the
    line. Without a clock nothing is timed.
    """

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock
        self._first_thought_at: Optional[float] = None
        self._first_answer_at: Optional[float] = None
        self._extra: Dict[str, Any] = {}
        self._buf = bytearray()  # the incomplete last line; never contains LF
        self._bytes = 0
        # Blocks in order: a list of str = a text run still being merged, a dict = a finished block.
        self._items: List[Any] = []
        self._thinking: List[List[str]] = []
        self._last_kind: Optional[str] = None  # type of the last kept part, for run merging
        self._usage: Optional[Dict[str, Any]] = None
        self._model_version: Optional[str] = None
        self._finish_reason: Optional[str] = None
        self._response_id: Optional[str] = None
        self._trace_id: Optional[str] = None
        self._n_events = 0
        self._n_bad = 0
        self._error: Optional[str] = None

    def feed(self, chunk: bytes) -> None:
        try:
            data = _as_bytes(chunk)
            if not data:
                return
            self._bytes += len(data)
            start = len(self._buf)
            self._buf += data
            # Only the new bytes can hold a line end; rescanning the old ones would make
            # byte-at-a-time feeding quadratic in the line length.
            if self._buf.find(b"\n", start) < 0:
                return
            end = self._buf.rfind(b"\n")
            lines = bytes(self._buf[:end]).split(b"\n")
            del self._buf[: end + 1]
            for line in lines:
                self._line(line)
        except Exception:
            logger.debug("agy SSE feed failed", exc_info=True)

    def finish(self) -> AgyResponse:
        try:
            if self._buf:
                tail = bytes(self._buf)
                self._buf = bytearray()
                self._line(tail)
            return self._result()
        except Exception:
            logger.debug("agy SSE finish failed", exc_info=True)
            return AgyResponse(
                blocks=[],
                thinking=[],
                usage=None,
                model_version=None,
                finish_reason=None,
                response_id=None,
                trace_id=None,
                n_events=self._n_events,
                n_bad_events=self._n_bad,
                error=self._error,
                response_bytes=self._bytes,
            )

    # -- internals --

    def _line(self, raw: bytes) -> None:
        try:
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            if not raw.startswith(b"data:"):
                return  # event:, id:, comments, blank separators
            payload = raw[5:].strip()
            if not payload or payload == b"[DONE]":
                return
            try:
                obj = json.loads(payload.decode("utf-8", "replace"))
            except Exception:
                obj = None
            if not isinstance(obj, dict):
                self._n_bad += 1
                return
            self._event(obj)
        except Exception:
            logger.debug("agy SSE line failed", exc_info=True)

    def _json_body(self, body: Any) -> None:
        """A non-SSE body: one JSON object or a JSON array of them."""
        data = _as_bytes(body)
        if data is None:
            return
        self._bytes += len(data)
        text = data.decode("utf-8", "replace").lstrip(_BOM).strip()
        if not text:
            return
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        items = obj if isinstance(obj, list) else [obj]
        for item in items:
            if isinstance(item, dict):
                self._event(item)
            else:
                self._n_bad += 1

    def _event(self, obj: Dict[str, Any]) -> None:
        self._n_events += 1
        # Wrapped Code Assist form {"response": {...}, "traceId"} or a bare Gemini response.
        response = obj.get("response", obj)
        if not isinstance(response, dict):
            response = {}
        err = obj.get("error")
        if err is None:
            err = response.get("error")
        if err not in (None, False, ""):
            self._error = _format_error(err)
        trace_id = _nonempty_str(obj.get("traceId")) or _nonempty_str(response.get("traceId"))
        if trace_id is not None:
            self._trace_id = trace_id
        usage = response.get("usageMetadata")
        if isinstance(usage, dict):
            self._usage = usage  # cumulative: the last one carries the final numbers
        model_version = _nonempty_str(response.get("modelVersion"))
        if model_version is not None:
            self._model_version = model_version
        response_id = _nonempty_str(response.get("responseId"))
        if response_id is not None:
            self._response_id = response_id
        metadata = obj.get("metadata")
        if not _is_empty(metadata):
            self._extra[_EVENT_METADATA_KEY] = metadata
        candidate = self._first_candidate(response)
        if candidate is None:
            return
        finish_reason = _nonempty_str(candidate.get("finishReason"))
        if finish_reason is not None:
            self._finish_reason = finish_reason
        # Keys we have no column for (groundingMetadata, citationMetadata, safetyRatings, ...) are kept
        # without assuming their names or shapes; most are repeated or sent once near the end, so the
        # last non-empty value wins.
        for key, value in candidate.items():
            if key not in _CANDIDATE_OWN_KEYS and not _is_empty(value):
                self._extra[key] = value
        for part in _list(_dict(candidate.get("content")).get("parts")):
            self._part(part)

    @staticmethod
    def _first_candidate(response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # agy asks for one candidate. With several, only candidate 0 is kept so that
        # deltas of different candidates are never merged into one block.
        for candidate in _list(response.get("candidates")):
            if isinstance(candidate, dict) and candidate.get("index", 0) in (0, None):
                return candidate
        return None

    def _part(self, part: Any) -> None:
        block = _part_to_block(part)
        if block is None:
            return  # empty text / bare signature: does not end a run, is not an answer
        kind = block["type"]
        if self._clock is not None:
            if kind == "thinking":
                if self._first_thought_at is None:
                    self._first_thought_at = self._now()
            elif self._first_answer_at is None:
                self._first_answer_at = self._now()
        if kind == "thinking":
            if self._last_kind == "thinking":
                self._thinking[-1].append(block["thinking"])
            else:
                self._thinking.append([block["thinking"]])
        elif kind == "text":
            if self._last_kind == "text":
                self._items[-1].append(block["text"])
            else:
                self._items.append([block["text"]])
        else:
            self._items.append(block)
        self._last_kind = kind

    def _now(self) -> Optional[float]:
        clock = self._clock
        if clock is None:
            return None
        try:
            return float(clock())
        except Exception:
            # A clock that fails once is dropped: one debug line, not one per part.
            self._clock = None
            logger.debug("agy SSE clock failed", exc_info=True)
            return None

    def _bounded_extra(self) -> Dict[str, Any]:
        if not self._extra:
            return {}
        try:
            size = len(_canonical_json(self._extra))
        except Exception:
            logger.debug("agy SSE extra not serializable", exc_info=True)
            return {}
        if size > EXTRA_MAX_BYTES:
            return {"_omitted_bytes": size}
        return dict(self._extra)

    def _result(self) -> AgyResponse:
        blocks = [
            {"type": "text", "text": "".join(item)} if isinstance(item, list) else dict(item) for item in self._items
        ]
        thinking = [{"type": "thinking", "thinking": "".join(pieces)} for pieces in self._thinking]
        return AgyResponse(
            blocks=blocks,
            thinking=thinking,
            usage=self._usage,
            model_version=self._model_version,
            finish_reason=self._finish_reason,
            response_id=self._response_id,
            trace_id=self._trace_id,
            n_events=self._n_events,
            n_bad_events=self._n_bad,
            error=self._error,
            response_bytes=self._bytes,
            extra=self._bounded_extra(),
            first_thought_at=self._first_thought_at,
            first_answer_at=self._first_answer_at,
        )


def parse_json_response(body: bytes) -> AgyResponse:
    """Parse a non-SSE response body: one JSON object or a JSON array of them.

    A body that is not JSON counts as one bad event; an empty body as none. The
    whole body arrives before parsing, so nothing is timed.
    """
    acc = SSEAccumulator()
    try:
        acc._json_body(body)
    except Exception:
        logger.debug("agy JSON response parse failed", exc_info=True)
    return acc.finish()


# ── Columns ──


def usage_columns(usage: Any) -> Dict[str, int]:
    """Token columns from a usageMetadata dict; every missing or non-numeric value is 0."""
    try:
        u = _dict(usage)
        prompt = _count(u.get("promptTokenCount"))
        cached = _count(u.get("cachedContentTokenCount"))
        return {
            "prompt_tokens": prompt,
            "cache_read": cached,
            "input_tokens": max(prompt - cached, 0),
            "output_tokens": _count(u.get("candidatesTokenCount")),
            "thinking_tokens": _count(u.get("thoughtsTokenCount")),
            "total_tokens": _count(u.get("totalTokenCount")),
        }
    except Exception:
        logger.debug("agy usage columns failed", exc_info=True)
        return {
            "prompt_tokens": 0,
            "cache_read": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "total_tokens": 0,
        }


def _fraction(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def parse_quota_summary(body: bytes) -> List[Dict[str, Any]]:
    """One row per quota bucket of a retrieveUserQuotaSummary response; [] on malformed input.

    Buckets without ``bucketId`` and non-object groups / buckets are skipped.
    """
    try:
        obj = _loads(body)
        if not isinstance(obj, dict) or not isinstance(obj.get("groups"), list):
            return []
        rows: List[Dict[str, Any]] = []
        for group in obj["groups"]:
            if not isinstance(group, dict):
                continue
            group_name = _str(group.get("displayName"))
            for bucket in _list(group.get("buckets")):
                if not isinstance(bucket, dict):
                    continue
                bucket_id = _nonempty_str(bucket.get("bucketId"))
                if bucket_id is None:
                    continue
                rows.append(
                    {
                        "group_name": group_name,
                        "bucket_id": bucket_id,
                        "quota_window": _str(bucket.get("window")),
                        "remaining_fraction": _fraction(bucket.get("remainingFraction")),
                        "reset_time": _str(bucket.get("resetTime")),
                        "display_name": _str(bucket.get("displayName")),
                    }
                )
        return rows
    except Exception:
        logger.debug("agy quota summary parse failed", exc_info=True)
        return []


# ── Blobs ──


def content_hash(obj: Any) -> Optional[Tuple[str, int]]:
    """(sha256 hex, byte count) of ``obj`` in canonical JSON; None for None / empty values or on failure.

    Canonical = sorted keys, no whitespace, non-ASCII kept as UTF-8, unknown types
    via ``str``. Equal JSON values therefore hash alike whatever their key order,
    and the byte count is the size the blob store keeps.
    """
    try:
        if _is_empty(obj):
            return None
        data = _canonical_json(obj)
        return hashlib.sha256(data).hexdigest(), len(data)
    except Exception:
        logger.debug("agy content hash failed", exc_info=True)
        return None


def parse_json_value(body: Any) -> Any:
    """A response body decoded as JSON, whatever its top-level type; None for an empty or non-JSON body."""
    try:
        data = _as_bytes(body)
        if not data:
            return None
        text = data.decode("utf-8", "replace").lstrip(_BOM).strip()
        if not text:
            return None
        return json.loads(text)
    except Exception:
        logger.debug("agy JSON value parse failed", exc_info=True)
        return None
