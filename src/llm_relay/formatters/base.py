"""Base formatter protocol."""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_relay.detect.models import FullReport


class BaseFormatter(abc.ABC):
    @abc.abstractmethod
    def format(self, report: FullReport) -> str: ...
