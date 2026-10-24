"""Tracepoint package initialization."""

try:
    from config import ProjectConfig, config
except ImportError:
    from code.config import ProjectConfig, config

__version__ = "0.1.0"
__all__ = ["ProjectConfig", "config"]
