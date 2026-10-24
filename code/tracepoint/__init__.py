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
from .interpretation import (
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

from .policy import (
    DataCategory,
    FlowState,
    ObservedState,
    PolicyEvaluationResult,
    PolicyEvaluator,
    PolicyFinding,
    PolicyRule,
    PolicyRuleRegistry,
    RequiredAction,
    ResurrectionRisk,
    ResurrectionRiskAnalyzer,
)

from .decision import (
    CaseDecision,
    DecisionEngine,
    RequestClassifier,
    StepGenerator,
    StepValidator,
    ValidationDiagnostic,
    ValidationResult,
)

__version__ = "0.6.0"
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
    "DeterministicFallbackInterpreter",
    "EvidenceClaim",
    "EvidenceClaimType",
    "EvidenceSnippet",
    "GeminiExtractionOutput",
    "GeminiInterpreter",
    "InterpretationCache",
    "InterpretationResult",
    "InterpretationTarget",
    "UsageMetrics",
    "UsageTracker",
    "validate_and_filter_grounding",
    "DataCategory",
    "FlowState",
    "ObservedState",
    "PolicyEvaluationResult",
    "PolicyEvaluator",
    "PolicyFinding",
    "PolicyRule",
    "PolicyRuleRegistry",
    "RequiredAction",
    "ResurrectionRisk",
    "ResurrectionRiskAnalyzer",
    "CaseDecision",
    "DecisionEngine",
    "RequestClassifier",
    "StepGenerator",
    "StepValidator",
    "ValidationDiagnostic",
    "ValidationResult",
]
