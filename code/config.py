"""Configuration layer for Tracepoint.

Provides deterministic path resolution and settings with safe defaults
and no embedded secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def find_repo_root() -> Path:
    """Find repository root by searching upward for AGENTS.md or dataset/."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "AGENTS.md").exists() or (parent / "dataset").exists():
            return parent
    return current.parent


@dataclass(frozen=True)
class ProjectConfig:
    """Project configuration and dataset path mappings."""

    repo_root: Path
    dataset_dir: Path
    code_dir: Path
    docs_dir: Path
    tests_dir: Path

    # Core dataset files
    deletion_requests_csv: Path
    sample_deletion_requests_csv: Path
    sample_output_csv: Path
    user_identities_csv: Path
    user_audit_log_csv: Path
    system_activity_csv: Path
    deletion_rules_csv: Path
    evidence_files_csv: Path
    template_output_csv: Path

    # Evidence directory
    evidence_dir: Path
    evidence_databases_dir: Path
    evidence_documents_dir: Path
    evidence_images_dir: Path
    evidence_logs_dir: Path
    evidence_records_dir: Path

    # Final submission target
    root_output_csv: Path

    @classmethod
    def load(cls, custom_repo_root: Path | str | None = None) -> ProjectConfig:
        """Load configuration with optional environment variable overrides."""
        if custom_repo_root is not None:
            root = Path(custom_repo_root).resolve()
        else:
            env_root = os.getenv("TRACEPOINT_REPO_ROOT")
            root = Path(env_root).resolve() if env_root else find_repo_root()

        env_dataset = os.getenv("TRACEPOINT_DATASET_DIR")
        dataset = Path(env_dataset).resolve() if env_dataset else root / "dataset"

        evidence = dataset / "evidence"

        return cls(
            repo_root=root,
            dataset_dir=dataset,
            code_dir=root / "code",
            docs_dir=root / "docs",
            tests_dir=root / "tests",
            deletion_requests_csv=dataset / "deletion_requests.csv",
            sample_deletion_requests_csv=dataset / "sample_deletion_requests.csv",
            sample_output_csv=dataset / "sample_output.csv",
            user_identities_csv=dataset / "user_identities.csv",
            user_audit_log_csv=dataset / "user_audit_log.csv",
            system_activity_csv=dataset / "system_activity.csv",
            deletion_rules_csv=dataset / "deletion_rules.csv",
            evidence_files_csv=dataset / "evidence_files.csv",
            template_output_csv=dataset / "output.csv",
            evidence_dir=evidence,
            evidence_databases_dir=evidence / "databases",
            evidence_documents_dir=evidence / "documents",
            evidence_images_dir=evidence / "images",
            evidence_logs_dir=evidence / "logs",
            evidence_records_dir=evidence / "records",
            root_output_csv=root / "output.csv",
        )


# Default singleton instance
config = ProjectConfig.load()
