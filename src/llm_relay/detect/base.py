"""Base detector protocol."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_relay.detect.models import Finding, ParsedSession


class BaseDetector(abc.ABC):
    """All detectors must be stdlib-only."""

    detector_id: str
    display_name: str

    @abc.abstractmethod
    def check(self, session: ParsedSession) -> list[Finding]:
        """Return findings for this session. Empty list = no issues."""
        ...
