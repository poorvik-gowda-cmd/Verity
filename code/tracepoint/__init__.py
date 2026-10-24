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

__version__ = "0.2.0"
__all__ = [
    "ProjectConfig",
    "config",
    "IdentityLink",
    "IdentityResolver",
    "MatchMethod",
    "ResolutionResult",
    "ResolutionStatus",
]
