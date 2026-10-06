"""
autopilot.services — every generation function is tested with a mocked
Nemotron response (never a real network call) so these run offline and
never cost real API credits. The important behaviour under test isn't
"does Nemotron produce good text" (untestable without a live key) but "does
this code correctly validate/reject whatever Nemotron sends back" — a
hallucinated CSP code or malformed shape must never reach the database.
"""

import datetime as dt
import decimal
from unittest.mock import patch

import pytest
from autopilot.models import AnomalyFlag, DraftMessage, Insight, PriorityCall, ReviewedIngestRun
from autopilot.nemotron_client import NemotronUnavailable
from autopilot.services import (
    approve_draft_message,
    draft_csp_nudges,
    flag_ingestion_anomalies,
    generate_daily_briefing,
    generate_priority_calls,
    reject_draft_message,
)
from csp.models import Csp, IngestLog, MonthlySummary
from django.contrib.auth import get_user_model


@pytest.fixture
def two_csps_with_summary(db):
    month = dt.date.today().strftime("%Y-%m")
    a = Csp.objects.create(csp_code="1A850001", name="Alpha CSP", account_count=300)
    b = Csp.objects.create(csp_code="1A850002", name="Beta CSP", account_count=150)
    MonthlySummary.objects.create(
        csp=a, month=month, days_in_month=30, days_with_data=10,
        mtd_mab=decimal.Decimal("2000.00"), slab=MonthlySummary.Slab.NIL,
        incentive_rate_pa=decimal.Decimal("0"), gap_to_min=decimal.Decimal("501.00"),
        gap_to_next_slab=None, trend_flag=MonthlySummary.Trend.DECLINING,
        mom_change_pct=decimal.Decimal("-5.00"), is_eligible=True,
    )
    MonthlySummary.objects.create(
        csp=b, month=month, days_in_month=30, days_with_data=10,
        mtd_mab=decimal.Decimal("5000.00"), slab=MonthlySummary.Slab.S2,
        incentive_rate_pa=decimal.Decimal("1.20"), gap_to_min=decimal.Decimal("0"),
        gap_to_next_slab=decimal.Decimal("1000.00"), trend_flag=MonthlySummary.Trend.STABLE,
        is_eligible=True,
    )
    return a, b, month


@pytest.mark.django_db
def test_generate_daily_briefing_saves_valid_response(two_csps_with_summary):
    _, _, month = two_csps_with_summary
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {"headline": "Network stable", "body": "Nothing notable today."}
        insight = generate_daily_briefing(month=month)

    assert insight is not None
    assert Insight.objects.filter(month=month).count() == 1
    assert insight.headline == "Network stable"


@pytest.mark.django_db
def test_generate_daily_briefing_returns_none_on_unavailable(two_csps_with_summary):
    _, _, month = two_csps_with_summary
    with patch("autopilot.services.chat_json", side_effect=NemotronUnavailable("no key")):
        insight = generate_daily_briefing(month=month)

    assert insight is None
    assert Insight.objects.count() == 0


@pytest.mark.django_db
def test_generate_daily_briefing_rejects_missing_fields(two_csps_with_summary):
    _, _, month = two_csps_with_summary
    with patch("autopilot.services.chat_json", return_value={"headline": ""}):
        insight = generate_daily_briefing(month=month)

    assert insight is None
    assert Insight.objects.count() == 0


@pytest.mark.django_db
def test_generate_priority_calls_creates_only_valid_candidates(two_csps_with_summary):
    a, b, month = two_csps_with_summary
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {
            "priority_calls": [
                {"csp_code": a.csp_code, "reason": "Declining trend, small gap."},
                {"csp_code": "1A999999", "reason": "This CSP was never a candidate."},
                {"csp_code": b.csp_code, "reason": "Close to next slab."},
            ]
        }
        calls = generate_priority_calls(month=month, limit=15)

    codes = {c.csp_id for c in calls}
    assert codes == {a.csp_code, b.csp_code}  # hallucinated code silently dropped
    assert PriorityCall.objects.filter(month=month).count() == 2
    ranks = sorted(c.rank for c in calls)
    assert ranks == [1, 2]


@pytest.mark.django_db
def test_generate_priority_calls_respects_limit(two_csps_with_summary):
    a, b, month = two_csps_with_summary
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {
            "priority_calls": [
                {"csp_code": a.csp_code, "reason": "First."},
                {"csp_code": b.csp_code, "reason": "Second."},
            ]
        }
        calls = generate_priority_calls(month=month, limit=1)

    assert len(calls) == 1
    assert calls[0].csp_id == a.csp_code


@pytest.mark.django_db
def test_generate_priority_calls_is_idempotent_per_month(two_csps_with_summary):
    a, b, month = two_csps_with_summary
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {"priority_calls": [{"csp_code": a.csp_code, "reason": "x"}]}
        generate_priority_calls(month=month)
        generate_priority_calls(month=month)

    assert PriorityCall.objects.filter(month=month).count() == 1


@pytest.mark.django_db
def test_flag_ingestion_anomalies_marks_reviewed_even_with_no_flags(db):
    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    with patch("autopilot.services.chat_json", return_value={"anomalies": []}):
        flags = flag_ingestion_anomalies(log)

    assert flags == []
    assert ReviewedIngestRun.objects.filter(ingest_log=log).exists()


@pytest.mark.django_db
def test_flag_ingestion_anomalies_creates_flags_and_marks_reviewed(db):
    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {
            "anomalies": [{"description": "Odd spike", "severity": "high"}]
        }
        flags = flag_ingestion_anomalies(log)

    assert len(flags) == 1
    assert AnomalyFlag.objects.filter(ingest_log=log, severity="high").exists()
    assert ReviewedIngestRun.objects.filter(ingest_log=log).exists()


@pytest.mark.django_db
def test_flag_ingestion_anomalies_does_not_mark_reviewed_on_failure(db):
    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    with patch("autopilot.services.chat_json", side_effect=NemotronUnavailable("down")):
        flags = flag_ingestion_anomalies(log)

    assert flags == []
    # Must stay unreviewed so a later pass retries it once Nemotron is back.
    assert not ReviewedIngestRun.objects.filter(ingest_log=log).exists()


@pytest.mark.django_db
def test_flag_ingestion_anomalies_invalid_severity_falls_back_to_low(db):
    log = IngestLog.objects.create(source="calling_sheet", status=IngestLog.Status.SUCCESS)
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {
            "anomalies": [{"description": "Weird", "severity": "catastrophic"}]
        }
        flags = flag_ingestion_anomalies(log)

    assert flags[0].severity == AnomalyFlag.Severity.LOW


@pytest.mark.django_db
def test_draft_csp_nudges_only_drafts_known_candidates(two_csps_with_summary):
    a, b, month = two_csps_with_summary
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {
            "drafts": [
                {"csp_code": a.csp_code, "text": "You're close to S1!"},
                {"csp_code": "1A999999", "text": "Should never be saved."},
            ]
        }
        drafts = draft_csp_nudges(month=month)

    assert len(drafts) == 1
    assert drafts[0].csp_id == a.csp_code
    assert drafts[0].status == DraftMessage.Status.DRAFT  # never auto-approved/sent


@pytest.mark.django_db
def test_draft_csp_nudges_uses_priority_calls_when_available(two_csps_with_summary):
    a, b, month = two_csps_with_summary
    PriorityCall.objects.create(
        month=month, csp=a, rank=1, reason="Test reason",
        signals_used={"slab": "NIL", "gap": 501.0},
    )
    with patch("autopilot.services.chat_json", return_value={"drafts": []}) as mock_chat:
        draft_csp_nudges(month=month)
        sent_candidates_json = mock_chat.call_args[0][1]  # (system_prompt, user_prompt)

    assert a.csp_code in sent_candidates_json
    assert b.csp_code not in sent_candidates_json  # b was never made a priority call


@pytest.mark.django_db
def test_daily_activity_or_growth_absence_does_not_crash_briefing(db):
    # No CSPs, no summaries at all — the empty-network case must still work.
    with patch("autopilot.services.chat_json") as mock_chat:
        mock_chat.return_value = {"headline": "No data yet", "body": "Network is empty."}
        insight = generate_daily_briefing()

    assert insight is not None


@pytest.mark.django_db
def test_approve_draft_message_moves_draft_to_approved(two_csps_with_summary):
    a, _, _ = two_csps_with_summary
    user = get_user_model().objects.create_user(username="staff", password="pw")
    draft = DraftMessage.objects.create(csp=a, text="Hi", status=DraftMessage.Status.DRAFT)

    assert approve_draft_message(draft, user) is True
    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.APPROVED
    assert draft.reviewed_by_id == user.id
    assert draft.reviewed_at is not None


@pytest.mark.django_db
def test_approve_draft_message_is_noop_when_not_draft(two_csps_with_summary):
    a, _, _ = two_csps_with_summary
    user = get_user_model().objects.create_user(username="staff", password="pw")
    already_sent = DraftMessage.objects.create(csp=a, text="Hi", status=DraftMessage.Status.SENT)

    assert approve_draft_message(already_sent, user) is False
    already_sent.refresh_from_db()
    assert already_sent.status == DraftMessage.Status.SENT  # untouched


@pytest.mark.django_db
def test_reject_draft_message_refuses_to_touch_sent(two_csps_with_summary):
    a, _, _ = two_csps_with_summary
    user = get_user_model().objects.create_user(username="staff", password="pw")
    sent = DraftMessage.objects.create(csp=a, text="Hi", status=DraftMessage.Status.SENT)

    assert reject_draft_message(sent, user) is False
    sent.refresh_from_db()
    assert sent.status == DraftMessage.Status.SENT
