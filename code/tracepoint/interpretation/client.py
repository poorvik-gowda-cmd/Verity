"""Google Gemini evidence interpreter with resilient retries, caching, and fallback.

Integrates Google Gemini strictly for ambiguous evidence interpretation and structured claim
extraction that deterministic parsing cannot reliably handle.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

try:
    from google import genai
    from google.genai import types
    from google.genai.errors import APIError
except ImportError:
    genai = None  # type: ignore
    types = None  # type: ignore
    APIError = Exception  # type: ignore

try:
    from config import config
except ImportError:
    from code.config import config

from .cache import InterpretationCache
from .fallback import DeterministicFallbackInterpreter
from .models import (
    EvidenceClaim,
    EvidenceSnippet,
    GeminiExtractionOutput,
    InterpretationResult,
    InterpretationTarget,
    validate_and_filter_grounding,
)
from .usage import UsageTracker

logger = logging.getLogger("tracepoint.interpretation")

_SENTINEL = object()


class GeminiInterpreter:
    """Resilient, evidence-grounded interpreter powered by Google Gemini."""

    def __init__(
        self,
        api_key: Any = _SENTINEL,
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        cache: Optional[InterpretationCache] = None,
        usage_tracker: Optional[UsageTracker] = None,
        fallback_interpreter: Optional[DeterministicFallbackInterpreter] = None,
    ) -> None:
        if api_key is _SENTINEL:
            self.api_key = config.gemini_api_key or os.getenv("GEMINI_API_KEY")
        else:
            self.api_key = api_key
        self.model = model or config.gemini_model or os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else config.gemini_timeout_seconds
        )
        self.max_retries = (
            max_retries if max_retries is not None else config.gemini_max_retries
        )

        self.cache = cache or InterpretationCache()
        self.usage_tracker = usage_tracker or UsageTracker(
            provider="google", model=self.model
        )
        self.fallback = fallback_interpreter or DeterministicFallbackInterpreter()

        self._client: Optional[Any] = None
        if self.api_key and genai is not None:
            self._init_client()

    def _init_client(self) -> None:
        """Initialize Google GenAI client with configured timeout."""
        try:
            timeout_ms = int(self.timeout_seconds * 1000)
            http_opts = types.HttpOptions(timeout=timeout_ms)
            self._client = genai.Client(api_key=self.api_key, http_options=http_opts)
        except Exception as exc:
            logger.warning("Failed to initialize Google GenAI client: %s", exc)
            self._client = None

    @property
    def is_available(self) -> bool:
        """True if Gemini SDK and API key are available."""
        return self._client is not None and bool(self.api_key)

    def _build_prompt(self, target: InterpretationTarget) -> str:
        """Construct a structured, evidence-grounded extraction prompt."""
        snippets_text = []
        for s in target.evidence_snippets:
            loc = f" (locator: {s.locator})" if s.locator else ""
            sys_info = f" [system: {s.system_id or 'unknown'}, resource: {s.resource_id or 'unknown'}]"
            ts = f" [timestamp: {s.timestamp}]" if s.timestamp else ""
            snippets_text.append(
                f"--- EVIDENCE ITEM: {s.evidence_id}{loc}{sys_info}{ts} ---\n{s.content}\n"
            )

        joined_snippets = "\n".join(snippets_text)

        allowed_ids_str = ", ".join(sorted(target.allowed_evidence_ids()))

        return (
            "You are a deletion audit evidence analyst for Northstar Shop. "
            "Analyze the provided evidence snippets to extract factual claims regarding personal data, "
            "deletion status, synchronization risks, or tombstones.\n\n"
            "STRICT RULES:\n"
            "1. Extract ONLY claims directly supported by the text of the supplied snippets.\n"
            f"2. You MUST cite one of the supplied evidence IDs: [{allowed_ids_str}]. Do NOT invent IDs.\n"
            "3. Do NOT make or alter final deletion decisions (such as declaring COMPLETE or INCOMPLETE).\n"
            "4. Only populate timestamp or resource_id if explicitly stated in the evidence text.\n"
            "5. If a fact is ambiguous, reflect the uncertainty with a lower confidence and note.\n\n"
            f"Case: {target.case_id}\n"
            f"Subject User ID: {target.user_id or 'Unknown'}\n"
            f"Target Query: {target.query}\n\n"
            "EVIDENCE SNIPPETS:\n"
            f"{joined_snippets}\n\n"
            "Extract structured claims conforming to the requested schema."
        )

    def interpret(self, target: InterpretationTarget) -> InterpretationResult:
        """Interpret evidence snippets with caching, retries, validation, and fallback."""
        start_time = time.time()
        allowed_ids = target.allowed_evidence_ids()

        # Check cache first
        cached_output = self.cache.get(self.model, target)
        if cached_output is not None:
            valid_claims, rejected_count = validate_and_filter_grounding(
                cached_output, allowed_ids
            )
            self.usage_tracker.record_call(
                input_tokens=0,
                output_tokens=0,
                latency_ms=0.0,
                cached=True,
            )
            return InterpretationResult(
                target=target,
                claims=valid_claims,
                summary=cached_output.summary,
                ambiguities=cached_output.ambiguities,
                rejected_claims_count=rejected_count,
                fallback_used=False,
                provider="google",
                model=self.model,
                tokens_in=0,
                tokens_out=0,
                latency_ms=0.0,
                cached=True,
            )

        # Fallback if client is unavailable
        if not self.is_available:
            fb_output = self.fallback.interpret(target)
            valid_claims, rejected = validate_and_filter_grounding(fb_output, allowed_ids)
            elapsed_ms = (time.time() - start_time) * 1000.0
            return InterpretationResult(
                target=target,
                claims=valid_claims,
                summary=fb_output.summary,
                ambiguities=fb_output.ambiguities,
                rejected_claims_count=rejected,
                fallback_used=True,
                provider="deterministic_fallback",
                model="heuristics",
                tokens_in=0,
                tokens_out=0,
                latency_ms=elapsed_ms,
                cached=False,
                error_message="Gemini client not configured or API key missing; used deterministic fallback.",
            )

        # Execute call with bounded retries
        prompt = self._build_prompt(target)
        attempts = 0
        backoff_sec = 1.0
        last_error: Optional[str] = None
        extracted_output: Optional[GeminiExtractionOutput] = None
        tokens_in = 0
        tokens_out = 0

        while attempts <= self.max_retries:
            attempts += 1
            call_start = time.time()
            try:
                config_params = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GeminiExtractionOutput,
                    temperature=0.0,  # Deterministic factual extraction
                )

                response = self._client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=config_params,
                )

                call_latency_ms = (time.time() - call_start) * 1000.0

                # Extract token usage
                if hasattr(response, "usage_metadata") and response.usage_metadata:
                    tokens_in = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                    tokens_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

                # Parse response
                raw_text = response.text or "{}"
                extracted_output = GeminiExtractionOutput.model_validate_json(raw_text)

                # Record successful call
                self.usage_tracker.record_call(
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    latency_ms=call_latency_ms,
                    cached=False,
                    failed=False,
                )

                # Store in cache
                self.cache.set(self.model, target, extracted_output)
                break

            except Exception as exc:
                call_latency_ms = (time.time() - call_start) * 1000.0
                last_error = str(exc)
                logger.warning(
                    "Gemini API attempt %d/%d failed: %s",
                    attempts,
                    self.max_retries + 1,
                    exc,
                )

                if attempts <= self.max_retries:
                    time.sleep(backoff_sec)
                    backoff_sec *= 2.0
                else:
                    self.usage_tracker.record_call(
                        input_tokens=0,
                        output_tokens=0,
                        latency_ms=call_latency_ms,
                        cached=False,
                        failed=True,
                    )

        total_latency_ms = (time.time() - start_time) * 1000.0

        # Handle successful extraction
        if extracted_output is not None:
            valid_claims, rejected = validate_and_filter_grounding(
                extracted_output, allowed_ids
            )
            return InterpretationResult(
                target=target,
                claims=valid_claims,
                summary=extracted_output.summary,
                ambiguities=extracted_output.ambiguities,
                rejected_claims_count=rejected,
                fallback_used=False,
                provider="google",
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=total_latency_ms,
                cached=False,
            )

        # Fallback on failure
        fb_output = self.fallback.interpret(target)
        valid_claims, rejected = validate_and_filter_grounding(fb_output, allowed_ids)
        return InterpretationResult(
            target=target,
            claims=valid_claims,
            summary=fb_output.summary,
            ambiguities=fb_output.ambiguities,
            rejected_claims_count=rejected,
            fallback_used=True,
            provider="deterministic_fallback",
            model="heuristics",
            tokens_in=0,
            tokens_out=0,
            latency_ms=total_latency_ms,
            cached=False,
            error_message=f"Gemini API failed after {attempts} attempts ({last_error}); used fallback.",
        )

    def interpret_image(
        self,
        image_path: Path,
        evidence_id: str,
        default_captured_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Extract structured textual claims from an image screenshot using Gemini Multimodal Vision."""
        start_time = time.time()
        cache_key = f"img_{evidence_id}_{image_path.name}"

        # Check cache
        if hasattr(self.cache, "_memory_cache") and cache_key in self.cache._memory_cache:
            self.usage_tracker.record_call(input_tokens=0, output_tokens=0, latency_ms=0.0, cached=True)
            return self.cache._memory_cache[cache_key]

        extracted: Dict[str, Any] = {
            "evidence_id": evidence_id,
            "text": f"Image screenshot: {image_path.stem}",
            "system_id": None,
            "resource_id": None,
            "identifiers": [],
            "status": "PARSED",
        }

        if not self.is_available or genai is None or types is None:
            return extracted

        try:
            image_data = image_path.read_bytes()
            image_part = types.Part.from_bytes(data=image_data, mime_type="image/png")
            prompt = (
                "You are an evidence audit parser. Inspect this system console or support screenshot. "
                "Extract all visible text, including: system name, resource IDs (e.g. account_*, ticket_*, backup_*), "
                "user identifiers (emails, user IDs, phones), and status/state (e.g. active, deleted, redacted). "
                "Output concise text summarizing the factual contents."
            )
            response = self._client.models.generate_content(
                model=self.model,
                contents=[image_part, prompt],
            )
            call_latency = (time.time() - start_time) * 1000.0
            tokens_in = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
            tokens_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
            self.usage_tracker.record_call(
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                latency_ms=call_latency,
                cached=False,
            )
            extracted["text"] = (response.text or "").strip() or extracted["text"]

            # Store in cache
            if hasattr(self.cache, "_memory_cache"):
                self.cache._memory_cache[cache_key] = extracted
                if self.cache.cache_file:
                    self.cache._save_to_file()
        except Exception as exc:
            logger.warning("Gemini image extraction failed for %s: %s", image_path.name, exc)

        return extracted
