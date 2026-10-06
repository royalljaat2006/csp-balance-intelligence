"""
run_autopilot must call all five generation steps even when one of them
raises — a bad API response or a validation failure in one step (e.g.
generate_priority_calls) must never prevent draft_csp_nudges or the CSP
Operations Agent run from running.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command

_ALL_STEPS = [
    "generate_insights",
    "generate_priority_calls",
    "review_ingest_anomalies",
    "draft_csp_nudges",
    "run_csp_operations_agent",
]


@pytest.mark.django_db
def test_run_autopilot_calls_all_five_steps():
    with patch("autopilot.management.commands.run_autopilot.call_command") as mock_call:
        call_command("run_autopilot")

    called = [c.args[0] for c in mock_call.call_args_list]
    assert called == _ALL_STEPS


@pytest.mark.django_db
def test_run_autopilot_continues_past_a_failing_step():
    with patch("autopilot.management.commands.run_autopilot.call_command") as mock_call:
        mock_call.side_effect = [RuntimeError("boom"), None, None, None, None]
        call_command("run_autopilot")

    called = [c.args[0] for c in mock_call.call_args_list]
    # All five still attempted, in order, despite the first one raising.
    assert called == _ALL_STEPS
