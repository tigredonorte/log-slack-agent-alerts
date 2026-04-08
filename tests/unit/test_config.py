# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the EKS Log Alerts environment variable configuration loader.

Tests that load_config() correctly reads, parses, and validates all required
environment variables, and fails loudly when any are missing or malformed.

Validates: Requirements 9.1, 9.2
"""

import importlib
import sys
from pathlib import Path
from typing import Dict

import pytest

# The pattern directory uses dashes ("eks-log-alerts") which is not a valid
# Python package name. We add the pattern directory to sys.path and import
# the config module directly.
_PATTERN_DIR = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
sys.path.insert(0, str(_PATTERN_DIR))
config_module = importlib.import_module("config")

load_config = config_module.load_config
AppConfig = config_module.AppConfig
REQUIRED_ENV_VARS = config_module.REQUIRED_ENV_VARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _full_env() -> Dict[str, str]:
    """Return a complete set of valid environment variables for load_config.

    Returns:
        A dictionary mapping every required env var name to a valid value.
    """
    return {
        "SLACK_CHANNEL_WEBHOOK_URL": "https://hooks.slack.com/services/T00/B00/xxx",
        "MONITORED_LOG_GROUPS": "/aws/eks/team5-app/containers,/aws/eks/team5-api/containers",
        "CONFIDENCE_THRESHOLD": "0.7",
        "LOG_POLL_INTERVAL_SECONDS": "30",
        "CLASSIFICATION_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "SEVERITY_EXAMPLES_TABLE_NAME": "team5-severity-examples",
        "PREFIX": "team5",
    }


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestLoadConfigHappyPath:
    """Tests that load_config succeeds with valid environment variables."""

    def test_all_fields_populated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that load_config returns an AppConfig with all fields set."""
        env = _full_env()
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cfg: AppConfig = load_config()

        assert cfg.slack_channel_webhook_url == env["SLACK_CHANNEL_WEBHOOK_URL"]
        assert cfg.monitored_log_groups == [
            "/aws/eks/team5-app/containers",
            "/aws/eks/team5-api/containers",
        ]
        assert cfg.confidence_threshold == 0.7
        assert cfg.log_poll_interval_seconds == 30
        assert cfg.classification_model_id == env["CLASSIFICATION_MODEL_ID"]
        assert cfg.severity_examples_table_name == env["SEVERITY_EXAMPLES_TABLE_NAME"]
        assert cfg.prefix == "team5"

    def test_single_log_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that a single log group (no comma) is parsed correctly."""
        env = _full_env()
        env["MONITORED_LOG_GROUPS"] = "/aws/eks/single-group"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cfg: AppConfig = load_config()
        assert cfg.monitored_log_groups == ["/aws/eks/single-group"]

    def test_confidence_threshold_boundary_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that confidence_threshold=0.0 is accepted."""
        env = _full_env()
        env["CONFIDENCE_THRESHOLD"] = "0.0"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cfg: AppConfig = load_config()
        assert cfg.confidence_threshold == 0.0

    def test_confidence_threshold_boundary_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that confidence_threshold=1.0 is accepted."""
        env = _full_env()
        env["CONFIDENCE_THRESHOLD"] = "1.0"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cfg: AppConfig = load_config()
        assert cfg.confidence_threshold == 1.0

    def test_config_is_frozen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that the returned AppConfig is immutable (frozen dataclass)."""
        env = _full_env()
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        cfg: AppConfig = load_config()
        with pytest.raises(AttributeError):
            cfg.prefix = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Missing environment variable tests
# ---------------------------------------------------------------------------


class TestLoadConfigMissingVars:
    """Tests that load_config fails loudly when required vars are missing."""

    @pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
    def test_missing_required_var_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing_var: str
    ) -> None:
        """Verify that removing any single required var raises EnvironmentError.

        The error message must name the missing variable (Requirement 9.2).

        Args:
            monkeypatch: Pytest fixture for modifying environment.
            missing_var: The name of the env var to remove.
        """
        env = _full_env()
        # Set all vars except the one we're testing
        for key, value in env.items():
            if key != missing_var:
                monkeypatch.setenv(key, value)
        # Ensure the target var is unset
        monkeypatch.delenv(missing_var, raising=False)

        with pytest.raises(EnvironmentError, match=missing_var):
            load_config()

    @pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
    def test_empty_string_var_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing_var: str
    ) -> None:
        """Verify that setting a required var to empty string raises EnvironmentError.

        Args:
            monkeypatch: Pytest fixture for modifying environment.
            missing_var: The name of the env var to set to empty.
        """
        env = _full_env()
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        # Override the target var with an empty string
        monkeypatch.setenv(missing_var, "")

        with pytest.raises(EnvironmentError, match=missing_var):
            load_config()

    @pytest.mark.parametrize("missing_var", REQUIRED_ENV_VARS)
    def test_whitespace_only_var_raises(
        self, monkeypatch: pytest.MonkeyPatch, missing_var: str
    ) -> None:
        """Verify that setting a required var to whitespace raises EnvironmentError.

        Args:
            monkeypatch: Pytest fixture for modifying environment.
            missing_var: The name of the env var to set to whitespace.
        """
        env = _full_env()
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv(missing_var, "   ")

        with pytest.raises(EnvironmentError, match=missing_var):
            load_config()


# ---------------------------------------------------------------------------
# Invalid value tests
# ---------------------------------------------------------------------------


class TestLoadConfigInvalidValues:
    """Tests that load_config fails on malformed typed values."""

    def test_confidence_threshold_not_a_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that a non-numeric CONFIDENCE_THRESHOLD raises ValueError."""
        env = _full_env()
        env["CONFIDENCE_THRESHOLD"] = "not-a-number"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="CONFIDENCE_THRESHOLD"):
            load_config()

    def test_confidence_threshold_too_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that CONFIDENCE_THRESHOLD > 1.0 raises ValueError."""
        env = _full_env()
        env["CONFIDENCE_THRESHOLD"] = "1.5"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="CONFIDENCE_THRESHOLD"):
            load_config()

    def test_confidence_threshold_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that CONFIDENCE_THRESHOLD < 0.0 raises ValueError."""
        env = _full_env()
        env["CONFIDENCE_THRESHOLD"] = "-0.1"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="CONFIDENCE_THRESHOLD"):
            load_config()

    def test_log_poll_interval_not_a_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that a non-integer LOG_POLL_INTERVAL_SECONDS raises ValueError."""
        env = _full_env()
        env["LOG_POLL_INTERVAL_SECONDS"] = "abc"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="LOG_POLL_INTERVAL_SECONDS"):
            load_config()

    def test_log_poll_interval_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that LOG_POLL_INTERVAL_SECONDS=0 raises ValueError."""
        env = _full_env()
        env["LOG_POLL_INTERVAL_SECONDS"] = "0"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="LOG_POLL_INTERVAL_SECONDS"):
            load_config()

    def test_log_poll_interval_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that a negative LOG_POLL_INTERVAL_SECONDS raises ValueError."""
        env = _full_env()
        env["LOG_POLL_INTERVAL_SECONDS"] = "-5"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="LOG_POLL_INTERVAL_SECONDS"):
            load_config()

    def test_monitored_log_groups_all_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that MONITORED_LOG_GROUPS with only commas raises ValueError."""
        env = _full_env()
        env["MONITORED_LOG_GROUPS"] = ",,,"
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        with pytest.raises(ValueError, match="MONITORED_LOG_GROUPS"):
            load_config()
