# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Property-based test for environment variable validation (Property 9).

Feature: eks-log-slack-alerts, Property 9: Missing environment variable startup failure

For any required environment variable in the set {SLACK_CHANNEL_WEBHOOK_URL,
MONITORED_LOG_GROUPS, CONFIDENCE_THRESHOLD, LOG_POLL_INTERVAL_SECONDS,
CLASSIFICATION_MODEL_ID, SEVERITY_EXAMPLES_TABLE_NAME, PREFIX}, if that
variable is missing at startup, the config loader SHALL raise an error whose
message contains the name of the missing variable.

Uses Hypothesis to generate random subsets of missing variables and verify
that the config loader always fails with a descriptive error naming at least
one of the missing variables.

Validates: Requirements 9.2
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import the config module from the pattern directory (uses dashes in name,
# so we add it to sys.path and use importlib).
# ---------------------------------------------------------------------------
_PATTERN_DIR: Path = Path(__file__).resolve().parents[2] / "patterns" / "eks-log-alerts"
sys.path.insert(0, str(_PATTERN_DIR))
_config_module = importlib.import_module("config")

load_config = _config_module.load_config
REQUIRED_ENV_VARS: List[str] = _config_module.REQUIRED_ENV_VARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_env() -> Dict[str, str]:
    """Return a complete set of valid environment variables for load_config.

    Every required variable is set to a syntactically valid value so that
    removing any single variable isolates the failure to that variable alone.

    Returns:
        A dictionary mapping every required env var name to a valid value.
    """
    return {
        "SLACK_CHANNEL_WEBHOOK_URL": "https://hooks.slack.com/services/T00/B00/xxx",
        "MONITORED_LOG_GROUPS": "/aws/eks/team5-app/containers",
        "CONFIDENCE_THRESHOLD": "0.7",
        "LOG_POLL_INTERVAL_SECONDS": "30",
        "CLASSIFICATION_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "SEVERITY_EXAMPLES_TABLE_NAME": "team5-severity-examples",
        "PREFIX": "team5",
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy that draws exactly one required env var name at random.
single_missing_var_strategy: st.SearchStrategy[str] = st.sampled_from(
    REQUIRED_ENV_VARS
)

# Strategy that draws a non-empty subset of required env var names.
# This tests the case where multiple variables are missing simultaneously.
missing_vars_subset_strategy: st.SearchStrategy[FrozenSet[str]] = st.frozensets(
    elements=st.sampled_from(REQUIRED_ENV_VARS),
    min_size=1,
    max_size=len(REQUIRED_ENV_VARS),
)


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


class TestConfigEnvVarValidationProperty:
    """Property 9: Missing environment variable startup failure.

    For any required env var removed, the config loader SHALL raise an error
    whose message contains the missing variable name.
    """

    @given(missing_var=single_missing_var_strategy)
    @settings(max_examples=100)
    def test_single_missing_var_raises_with_name_in_message(
        self, missing_var: str
    ) -> None:
        """Removing any single required env var causes an EnvironmentError
        whose message contains the name of the missing variable.

        This is the core Property 9 assertion: the system fails loudly and
        names the offending variable so operators can fix it immediately.

        Args:
            missing_var: The name of the required env var to remove,
                drawn randomly by Hypothesis from REQUIRED_ENV_VARS.
        """
        env: Dict[str, str] = _full_env()

        # Build a clean environment: set every var except the missing one.
        # We patch os.environ directly and restore it after the test to
        # avoid cross-test pollution.
        original_environ: Dict[str, str] = os.environ.copy()
        try:
            os.environ.clear()
            for key, value in env.items():
                if key != missing_var:
                    os.environ[key] = value

            with pytest.raises(EnvironmentError) as exc_info:
                load_config()

            # The error message MUST contain the missing variable name
            # so operators know exactly which variable to set.
            assert missing_var in str(exc_info.value), (
                f"EnvironmentError was raised but its message does not contain "
                f"the missing variable name '{missing_var}'. "
                f"Actual message: {exc_info.value}"
            )
        finally:
            # Restore the original environment to prevent side effects.
            os.environ.clear()
            os.environ.update(original_environ)

    @given(missing_vars=missing_vars_subset_strategy)
    @settings(max_examples=100)
    def test_any_subset_of_missing_vars_raises_naming_at_least_one(
        self, missing_vars: FrozenSet[str]
    ) -> None:
        """Removing any non-empty subset of required env vars causes an
        EnvironmentError whose message names at least one of the missing
        variables.

        This strengthens Property 9 by verifying that even when multiple
        variables are absent, the error message is still actionable — it
        names at least one of the missing variables so the operator has a
        starting point for remediation.

        Args:
            missing_vars: A non-empty frozenset of required env var names
                to remove, drawn randomly by Hypothesis.
        """
        env: Dict[str, str] = _full_env()

        original_environ: Dict[str, str] = os.environ.copy()
        try:
            os.environ.clear()
            for key, value in env.items():
                if key not in missing_vars:
                    os.environ[key] = value

            with pytest.raises(EnvironmentError) as exc_info:
                load_config()

            # At least one of the missing variable names must appear in the
            # error message so the operator can begin fixing the problem.
            error_message: str = str(exc_info.value)
            found_any: bool = any(
                var_name in error_message for var_name in missing_vars
            )
            assert found_any, (
                f"EnvironmentError was raised but its message does not contain "
                f"any of the missing variable names {sorted(missing_vars)}. "
                f"Actual message: {error_message}"
            )
        finally:
            os.environ.clear()
            os.environ.update(original_environ)

    @given(missing_var=single_missing_var_strategy)
    @settings(max_examples=100)
    def test_empty_string_var_raises_with_name_in_message(
        self, missing_var: str
    ) -> None:
        """Setting any required env var to an empty string causes an
        EnvironmentError whose message contains the variable name.

        An empty string is functionally equivalent to "missing" — the
        config loader must not silently accept it.

        Args:
            missing_var: The name of the required env var to set to an
                empty string, drawn randomly by Hypothesis.
        """
        env: Dict[str, str] = _full_env()

        original_environ: Dict[str, str] = os.environ.copy()
        try:
            os.environ.clear()
            for key, value in env.items():
                os.environ[key] = value
            # Override the target variable with an empty string.
            os.environ[missing_var] = ""

            with pytest.raises(EnvironmentError) as exc_info:
                load_config()

            assert missing_var in str(exc_info.value), (
                f"EnvironmentError was raised for empty '{missing_var}' but "
                f"the message does not contain the variable name. "
                f"Actual message: {exc_info.value}"
            )
        finally:
            os.environ.clear()
            os.environ.update(original_environ)

    @given(missing_var=single_missing_var_strategy)
    @settings(max_examples=100)
    def test_whitespace_only_var_raises_with_name_in_message(
        self, missing_var: str
    ) -> None:
        """Setting any required env var to whitespace-only causes an
        EnvironmentError whose message contains the variable name.

        Whitespace-only values are treated as missing by the config loader.

        Args:
            missing_var: The name of the required env var to set to
                whitespace, drawn randomly by Hypothesis.
        """
        env: Dict[str, str] = _full_env()

        original_environ: Dict[str, str] = os.environ.copy()
        try:
            os.environ.clear()
            for key, value in env.items():
                os.environ[key] = value
            # Override the target variable with whitespace.
            os.environ[missing_var] = "   \t  "

            with pytest.raises(EnvironmentError) as exc_info:
                load_config()

            assert missing_var in str(exc_info.value), (
                f"EnvironmentError was raised for whitespace-only "
                f"'{missing_var}' but the message does not contain the "
                f"variable name. Actual message: {exc_info.value}"
            )
        finally:
            os.environ.clear()
            os.environ.update(original_environ)
