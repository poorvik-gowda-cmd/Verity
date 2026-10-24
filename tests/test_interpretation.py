"""Unit tests for Phase 5: Google Gemini Evidence Interpretation.

All tests strictly mock the Gemini client to ensure zero live API calls during
automated test execution, verifying resilience, caching, fallback, and grounding.
"""

from unittest.mock import MagicMock, patch
import pytest

from tracepoint.interpretation import (
    DeterministicFallbackInterpreter,
    EvidenceClaim,
    EvidenceClaimType,
    EvidenceSnippet,
    GeminiExtractionOutput,
    GeminiInterpreter,
    InterpretationCache,
    InterpretationResult,
    InterpretationTarget,
    UsageMetrics,
    UsageTracker,
    validate_and_filter_grounding,
)


@pytest.fixture
def sample_target() -> InterpretationTarget:
    """Fixture providing a standard interpretation target with 2 snippets."""
    return InterpretationTarget(
        case_id="case_05",
        user_id="user_05",
        query="Verify if account data remains in customer support and failover replica",
        evidence_snippets=[
            EvidenceSnippet(
                evidence_id="evidence_54",
                locator="table:accounts/row:5",
                system_id="account_failover_replica",
                resource_id="account_005",
                timestamp="2026-09-02T22:45:00Z",
                content='{"resource_id": "account_005", "email": "leila.haddad@yahoo.com", "status": "active"}',
            ),
            EvidenceSnippet(
                evidence_id="evidence_12",
                locator="doc:ticket_441",
                system_id="support_service",
                resource_id="ticket_441",
                timestamp="2026-08-25T11:00:00Z",
                content="Customer requested account deletion. Profile data redacted.",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# 1. Schema & Grounding Validation Tests
# ---------------------------------------------------------------------------


def test_schema_valid_extraction():
    """Verify GeminiExtractionOutput parses valid JSON schema."""
    data = {
        "claims": [
            {
                "claim_type": "RECORD_PRESENT",
                "supporting_evidence_id": "evidence_54",
                "system_id": "account_failover_replica",
                "resource_id": "account_005",
                "observed_state": "PRESENT",
                "timestamp": "2026-09-02T22:45:00Z",
                "supporting_locator": "table:accounts/row:5",
                "identifiers_found": ["leila.haddad@yahoo.com"],
                "confidence": 0.95,
                "uncertainty_note": None,
            }
        ],
        "summary": "Account record confirmed present in replica.",
        "ambiguities": [],
    }
    output = GeminiExtractionOutput.model_validate(data)
    assert len(output.claims) == 1
    assert output.claims[0].claim_type == EvidenceClaimType.RECORD_PRESENT
    assert output.claims[0].supporting_evidence_id == "evidence_54"
    assert output.claims[0].confidence == 0.95


def test_grounding_rejects_hallucinated_evidence_id(sample_target: InterpretationTarget):
    """Ensure claims citing evidence IDs not in the target snippets are rejected."""
    output = GeminiExtractionOutput(
        claims=[
            EvidenceClaim(
                claim_type=EvidenceClaimType.RECORD_PRESENT,
                supporting_evidence_id="evidence_54",  # Valid
                system_id="account_failover_replica",
                resource_id="account_005",
            ),
            EvidenceClaim(
                claim_type=EvidenceClaimType.RECORD_PRESENT,
                supporting_evidence_id="evidence_9999_fake",  # Hallucinated!
                system_id="imaginary_system",
                resource_id="ghost_account",
            ),
        ],
        summary="Mixed claims",
        ambiguities=[],
    )

    valid_claims, rejected_count = validate_and_filter_grounding(
        output, sample_target.allowed_evidence_ids()
    )

    assert len(valid_claims) == 1
    assert valid_claims[0].supporting_evidence_id == "evidence_54"
    assert rejected_count == 1


# ---------------------------------------------------------------------------
# 2. Deterministic Fallback Interpreter Tests
# ---------------------------------------------------------------------------


def test_deterministic_fallback_present_record(sample_target: InterpretationTarget):
    """Verify deterministic fallback correctly flags active record and identifiers."""
    fallback = DeterministicFallbackInterpreter()
    output = fallback.interpret(sample_target)

    assert len(output.claims) == 2
    claim_54 = [c for c in output.claims if c.supporting_evidence_id == "evidence_54"][0]
    assert claim_54.claim_type == EvidenceClaimType.RECORD_PRESENT
    assert claim_54.observed_state == "PRESENT"
    assert "account_005" in claim_54.identifiers_found

    claim_12 = [c for c in output.claims if c.supporting_evidence_id == "evidence_12"][0]
    assert claim_12.claim_type == EvidenceClaimType.TOMBSTONE_PRESERVED
    assert claim_12.observed_state == "REDACTED"


# ---------------------------------------------------------------------------
# 3. Usage Tracker & Cost Estimation Tests
# ---------------------------------------------------------------------------


def test_usage_tracker_metrics_and_pricing():
    """Verify token accumulation, call counts, latency, and cost calculation."""
    tracker = UsageTracker(
        provider="google",
        model="gemini-2.5-flash",
        input_cost_per_million=0.075,
        output_cost_per_million=0.30,
    )

    # Record 1 active call
    tracker.record_call(input_tokens=10_000, output_tokens=2_000, latency_ms=250.0)
    # Record 1 cached call
    tracker.record_call(input_tokens=0, output_tokens=0, latency_ms=0.0, cached=True)

    summary = tracker.get_summary()
    assert summary["total_calls"] == 2
    assert summary["api_calls_executed"] == 1
    assert summary["cached_calls"] == 1
    assert summary["input_tokens"] == 10_000
    assert summary["output_tokens"] == 2_000
    assert summary["total_tokens"] == 12_000
    assert summary["average_latency_ms"] == 250.0

    # 10,000 * 0.075 / 1,000,000 = 0.00075
    # 2,000 * 0.30 / 1,000,000 = 0.00060
    # Total = 0.00135
    expected_cost = tracker.calculate_cost(10_000, 2_000)
    assert pytest.approx(expected_cost, rel=1e-5) == 0.00135


# ---------------------------------------------------------------------------
# 4. Caching Tests
# ---------------------------------------------------------------------------


def test_interpretation_cache_hit(sample_target: InterpretationTarget, tmp_path):
    """Verify cache stores and retrieves identical queries without calling LLM."""
    cache_file = tmp_path / "cache.json"
    cache = InterpretationCache(cache_file=cache_file)

    output = GeminiExtractionOutput(
        claims=[
            EvidenceClaim(
                claim_type=EvidenceClaimType.RECORD_PRESENT,
                supporting_evidence_id="evidence_54",
            )
        ],
        summary="Cached summary",
    )

    # Initially empty
    assert cache.get("gemini-2.5-flash", sample_target) is None

    # Store
    cache.set("gemini-2.5-flash", sample_target, output)

    # Hit
    retrieved = cache.get("gemini-2.5-flash", sample_target)
    assert retrieved is not None
    assert retrieved.summary == "Cached summary"
    assert len(retrieved.claims) == 1


# ---------------------------------------------------------------------------
# 5. Gemini Client Mocked Execution & Resilience Tests
# ---------------------------------------------------------------------------


def test_client_missing_api_key_uses_fallback(sample_target: InterpretationTarget):
    """When API key is None, client must gracefully invoke deterministic fallback."""
    interpreter = GeminiInterpreter(api_key=None)
    assert not interpreter.is_available

    result = interpreter.interpret(sample_target)
    assert result.fallback_used
    assert result.provider == "deterministic_fallback"
    assert len(result.claims) == 2
    assert "deterministic fallback" in (result.error_message or "").lower()


def test_client_successful_mocked_gemini_call(sample_target: InterpretationTarget):
    """Verify client correctly processes and validates structured Gemini response."""
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = (
        '{"claims": [{"claim_type": "RECORD_PRESENT", "supporting_evidence_id": "evidence_54", '
        '"system_id": "account_failover_replica", "resource_id": "account_005", "observed_state": "PRESENT"}], '
        '"summary": "Account record remains in failover replica.", "ambiguities": []}'
    )
    mock_response.usage_metadata.prompt_token_count = 150
    mock_response.usage_metadata.candidates_token_count = 45
    mock_genai_client.models.generate_content.return_value = mock_response

    interpreter = GeminiInterpreter(api_key="mock_secret_key_12345")
    interpreter._client = mock_genai_client

    result = interpreter.interpret(sample_target)

    assert not result.fallback_used
    assert result.provider == "google"
    assert len(result.claims) == 1
    assert result.claims[0].supporting_evidence_id == "evidence_54"
    assert result.claims[0].claim_type == EvidenceClaimType.RECORD_PRESENT
    assert result.tokens_in == 150
    assert result.tokens_out == 45
    assert not result.cached

    # Second identical call should hit the cache!
    cached_result = interpreter.interpret(sample_target)
    assert cached_result.cached
    assert cached_result.tokens_in == 0
    assert cached_result.latency_ms == 0.0
    # API was called only once
    assert mock_genai_client.models.generate_content.call_count == 1


def test_client_retry_and_fallback_on_api_error(sample_target: InterpretationTarget):
    """Verify bounded retries and graceful fallback when Gemini raises exceptions."""
    mock_genai_client = MagicMock()
    mock_genai_client.models.generate_content.side_effect = RuntimeError("503 Service Unavailable")

    interpreter = GeminiInterpreter(
        api_key="mock_secret_key_12345",
        max_retries=2,
    )
    interpreter._client = mock_genai_client

    with patch("time.sleep", return_value=None):  # Fast retries
        result = interpreter.interpret(sample_target)

    assert result.fallback_used
    assert result.provider == "deterministic_fallback"
    assert mock_genai_client.models.generate_content.call_count == 3  # Initial + 2 retries
    assert "used fallback" in (result.error_message or "").lower()
    assert len(result.claims) == 2


def test_client_malformed_json_triggers_fallback(sample_target: InterpretationTarget):
    """Verify malformed JSON from Gemini triggers deterministic fallback."""
    mock_genai_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "NOT VALID JSON {broken"
    mock_genai_client.models.generate_content.return_value = mock_response

    interpreter = GeminiInterpreter(
        api_key="mock_secret_key_12345",
        max_retries=1,
    )
    interpreter._client = mock_genai_client

    with patch("time.sleep", return_value=None):
        result = interpreter.interpret(sample_target)

    assert result.fallback_used
    assert result.provider == "deterministic_fallback"
    assert len(result.claims) == 2
