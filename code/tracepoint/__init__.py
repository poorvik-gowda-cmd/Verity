"""Tracepoint package initialization."""

try:
    from config import ProjectConfig, config
except ImportError:
    from code.config import ProjectConfig, config

from .resolver import (
    IdentityLink,
    IdentityResolver,
    MatchMethod,
    ResolutionResult,
    ResolutionStatus,
)
from .timeline import (
    Contradiction,
    ContradictionDetector,
    ContradictionType,
    EventKind,
    EventSource,
    Timeline,
    TimelineBuilder,
    TimelineEvent,
    format_timestamp,
    parse_timestamp,
)

__version__ = "0.3.0"
__all__ = [
    "ProjectConfig",
    "config",
    "IdentityLink",
    "IdentityResolver",
    "MatchMethod",
    "ResolutionResult",
    "ResolutionStatus",
    "Contradiction",
    "ContradictionDetector",
    "ContradictionType",
    "EventKind",
    "EventSource",
    "Timeline",
    "TimelineBuilder",
    "TimelineEvent",
    "format_timestamp",
    "parse_timestamp",
]
