"""Tests for configuration layer and path resolution."""

import os
from pathlib import Path
import pytest

try:
    from config import ProjectConfig, config
except ImportError:
    from code.config import ProjectConfig, config


def test_config_paths_exist():
    """Verify that all core configured dataset paths physically exist."""
    assert config.dataset_dir.exists(), "dataset/ directory missing"
    assert config.deletion_requests_csv.exists(), "deletion_requests.csv missing"
    assert config.sample_deletion_requests_csv.exists(), "sample_deletion_requests.csv missing"
    assert config.sample_output_csv.exists(), "sample_output.csv missing"
    assert config.user_identities_csv.exists(), "user_identities.csv missing"
    assert config.user_audit_log_csv.exists(), "user_audit_log.csv missing"
    assert config.system_activity_csv.exists(), "system_activity.csv missing"
    assert config.deletion_rules_csv.exists(), "deletion_rules.csv missing"
    assert config.evidence_files_csv.exists(), "evidence_files.csv missing"
    assert config.evidence_dir.exists(), "evidence/ directory missing"


def test_custom_repo_root_override(tmp_path):
    """Verify loading config with a custom root directory."""
    custom_dataset = tmp_path / "dataset"
    custom_dataset.mkdir()
    cfg = ProjectConfig.load(custom_repo_root=tmp_path)
    assert cfg.repo_root == tmp_path
    assert cfg.dataset_dir == custom_dataset
