"""Caching layer for Gemini evidence interpretation.

Caches structured extraction results by request signature to prevent duplicate
API calls and provide instantaneous answers on repeated queries.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .models import GeminiExtractionOutput, InterpretationTarget


class InterpretationCache:
    """Thread-safe cache for evidence interpretation responses."""

    def __init__(self, cache_file: Optional[Path | str] = None) -> None:
        self.cache_file = Path(cache_file) if cache_file else None
        self._lock = threading.Lock()
        self._memory_cache: Dict[str, Dict[str, Any]] = {}
        if self.cache_file and self.cache_file.exists():
            self._load_from_file()

    def _generate_key(self, model: str, target: InterpretationTarget) -> str:
        """Generate a deterministic SHA-256 cache key from model and target."""
        raw_dict = {
            "model": model,
            "case_id": target.case_id,
            "user_id": target.user_id,
            "query": target.query,
            "snippets": [
                {
                    "evidence_id": s.evidence_id,
                    "locator": s.locator,
                    "content": s.content,
                }
                for s in target.evidence_snippets
            ],
        }
        serialized = json.dumps(raw_dict, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, model: str, target: InterpretationTarget) -> Optional[GeminiExtractionOutput]:
        """Retrieve cached output if present, or None."""
        key = self._generate_key(model, target)
        with self._lock:
            data = self._memory_cache.get(key)
            if data is not None:
                try:
                    return GeminiExtractionOutput.model_validate(data)
                except Exception:
                    return None
            return None

    def set(self, model: str, target: InterpretationTarget, output: GeminiExtractionOutput) -> None:
        """Store output in cache and optionally persist to disk."""
        key = self._generate_key(model, target)
        with self._lock:
            self._memory_cache[key] = output.model_dump()
            if self.cache_file:
                self._save_to_file()

    def _load_from_file(self) -> None:
        """Load cache entries from JSON file."""
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                self._memory_cache = json.load(f)
        except Exception:
            self._memory_cache = {}

    def _save_to_file(self) -> None:
        """Save cache entries to JSON file."""
        if not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._memory_cache, f, indent=2)
        except Exception:
            pass

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._memory_cache.clear()
            if self.cache_file and self.cache_file.exists():
                try:
                    self.cache_file.unlink()
                except Exception:
                    pass
