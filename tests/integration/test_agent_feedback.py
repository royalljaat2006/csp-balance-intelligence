"""
The assistant's feedback trail: every model call is recorded as it happened, a person's edit to
a draft is kept next to the original with its Levenshtein distance, and the quality measures
read only those rows. The model itself is never called (the OpenAI client is stubbed).
"""

import datetime as dt
import decimal
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from autopilot import quality
from autopilot.models import AgentInteraction, AgentTask, DraftMessage
from autopilot.nemotron_client import NemotronUnavailable, chat_json, last_interaction_id
from autopilot.services import (
    approve_draft_message,
    draft_csp_nudges,
    edit_and_approve_draft,
    reject_draft_message,
)
from csp.models import Csp, MonthlySummary
from django.contrib.auth import get_user_model
from django.utils import timezone

GOLDEN = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "golden" / "autopilot_nudge_cases.json")
    .read_text(encoding="utf-8")
)


def _reply(content: str, *, prompt=120, completion=40, finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def _client_returning(content: str, **kw):
    client = SimpleNamespace()

    def create(**_):
        return _reply(content, **kw)

    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    return client


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="ops", password="pw12345", is_staff=True)


@pytest.fixture
def csps(db):
    month = dt.date.today().strftime("%Y-%m")
    out = []
    for code, name, slab, gap in (("1A850001", "Alpha CSP", MonthlySummary.Slab.NIL, "501.00"),
                                  ("1A850002", "Beta CSP", MonthlySummary.Slab.S2, "1000.00")):
        c = Csp.objects.create(csp_code=code, name=name, account_count=300)
        nil = slab == MonthlySummary.Slab.NIL
        MonthlySummary.objects.create(
            csp=c, month=month, days_in_month=30, days_with_data=10,
            mtd_mab=decimal.Decimal("2000.00"), slab=slab, incentive_rate_pa=decimal.Decimal("0"),
            gap_to_min=decimal.Decimal(gap if nil else "0"),
            gap_to_next_slab=None if nil else decimal.Decimal(gap),
            trend_flag=MonthlySummary.Trend.STABLE, is_eligible=True,
        )
        out.append(c)
    return month, out


def _draft(csp, text="Hello there", *, status="draft", original=None, **kw):
    return DraftMessage.objects.create(
        csp=csp, text=text, original_text=text if original is None else original,
        status=status, **kw)


# ------------------------------------------------------------------ every call is recorded
@pytest.mark.django_db
def test_a_successful_call_is_recorded_with_what_the_model_was_shown():
    with patch("autopilot.nemotron_client._client", return_value=_client_returning('{"ok": 1}')):
        result = chat_json("system words", '{"gap": 500}', purpose="nudge_drafts")
    assert result == {"ok": 1}
    row = AgentInteraction.objects.get()
    assert (row.purpose, row.outcome) == ("nudge_drafts", "ok")
    assert row.system_prompt == "system words" and row.context == '{"gap": 500}'
    assert row.raw_output == '{"ok": 1}'
    assert (row.prompt_tokens, row.completion_tokens, row.finish_reason) == (120, 40, "stop")
    assert last_interaction_id() == row.pk


@pytest.mark.django_db
def test_a_failed_call_is_recorded_and_still_raises():
    with patch("autopilot.nemotron_client._client", side_effect=NemotronUnavailable("no key")):
        with pytest.raises(NemotronUnavailable):
            chat_json("s", "{}", purpose="daily_briefing")
    row = AgentInteraction.objects.get()
    assert row.outcome == "unavailable" and row.purpose == "daily_briefing" and row.raw_output == ""
    assert last_interaction_id() == row.pk


@pytest.mark.django_db
def test_unreadable_output_is_recorded_with_the_raw_text():
    with patch("autopilot.nemotron_client._client", return_value=_client_returning("not json")):
        with pytest.raises(NemotronUnavailable):
            chat_json("s", "{}")
    row = AgentInteraction.objects.get()
    assert row.outcome == "bad_json" and row.raw_output == "not json"


@pytest.mark.django_db
def test_recording_failure_never_breaks_the_call():
    with patch("autopilot.nemotron_client._client", return_value=_client_returning('{"a": 1}')), \
            patch.object(AgentInteraction.objects, "create", side_effect=RuntimeError("db down")):
        assert chat_json("s", "{}") == {"a": 1}
    assert last_interaction_id() is None


@pytest.mark.django_db
def test_a_stale_interaction_id_is_not_carried_into_the_next_call():
    with patch("autopilot.nemotron_client._client", return_value=_client_returning('{"a": 1}')):
        chat_json("s", "{}")
    with patch("autopilot.nemotron_client._client", side_effect=NemotronUnavailable("x")), \
            patch.object(AgentInteraction.objects, "create", side_effect=RuntimeError("down")):
        with pytest.raises(NemotronUnavailable):
            chat_json("s", "{}")
    assert last_interaction_id() is None


# ------------------------------------------------------------------ drafts keep their trail
@pytest.mark.django_db
def test_generated_drafts_keep_the_original_wording_context_and_the_call(csps):
    month, (alpha, _beta) = csps
    reply = json.dumps({"drafts": [{"csp_code": "1A850001", "text": "You are Rs 501 away."}]})
    with patch("autopilot.nemotron_client._client", return_value=_client_returning(reply)):
        created = draft_csp_nudges(month=month)
    draft = DraftMessage.objects.get(csp=alpha)
    assert len(created) == 1
    assert draft.original_text == draft.text == "You are Rs 501 away."
    assert draft.context["csp_code"] == "1A850001" and draft.context["gap"] == 501.0
    assert draft.interaction is not None and draft.interaction.purpose == "nudge_drafts"
    assert draft.edit_distance is None


# ------------------------------------------------------------------ editing in place
@pytest.mark.django_db
def test_editing_then_approving_keeps_the_original_and_records_the_distance(csps, user):
    draft = _draft(csps[1][0], "You are Rs 500 away from S2.")
    assert edit_and_approve_draft(draft, user, "You are Rs 550 away from S2.") is True
    draft.refresh_from_db()
    assert draft.status == "approved" and draft.reviewed_by == user
    assert draft.text == "You are Rs 550 away from S2."
    assert draft.original_text == "You are Rs 500 away from S2."
    assert draft.edit_distance == 1 and draft.edited_at is not None


@pytest.mark.django_db
def test_approving_unchanged_text_through_the_edit_box_counts_as_no_edit(csps, user):
    draft = _draft(csps[1][0], "Same words")
    assert edit_and_approve_draft(draft, user, "Same words") is True
    draft.refresh_from_db()
    assert draft.edit_distance == 0 and draft.edited_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("bad", ["", "   ", "x" * 2001])
def test_empty_or_oversized_edits_change_nothing(csps, user, bad):
    draft = _draft(csps[1][0], "Original")
    assert edit_and_approve_draft(draft, user, bad) is False
    draft.refresh_from_db()
    assert draft.status == "draft" and draft.text == "Original" and draft.edit_distance is None


@pytest.mark.django_db
def test_an_already_decided_draft_cannot_be_edited_again(csps, user):
    draft = _draft(csps[1][0], "Original", status="rejected")
    assert edit_and_approve_draft(draft, user, "Changed") is False
    draft.refresh_from_db()
    assert draft.text == "Original" and draft.status == "rejected"


@pytest.mark.django_db
def test_a_draft_from_before_the_trail_existed_keeps_its_text_as_the_original(csps, user):
    draft = _draft(csps[1][0], "Old draft", original="")
    assert edit_and_approve_draft(draft, user, "Old draft, reworded") is True
    draft.refresh_from_db()
    assert draft.original_text == "Old draft" and draft.edit_distance == len(", reworded")


# ------------------------------------------------------------------ the measures
@pytest.mark.django_db
def test_no_decisions_means_no_data_not_zero():
    q = quality.agent_quality(30)
    assert q["implicit_acceptance"]["implicit_acceptance_rate"] is None
    assert q["implicit_acceptance"]["status"] == "no_data"
    assert q["correction_delta"]["value"] is None
    assert q["call_reliability"]["rate"] is None and q["investigation_completion"]["rate"] is None


@pytest.mark.django_db
def test_acceptance_and_correction_delta_from_real_decisions(csps, user):
    csp = csps[1][0]
    now = timezone.now()
    kept = _draft(csp, "keep me", status="approved", reviewed_at=now)  # approved as written
    _draft(csp, "abcd", status="approved", reviewed_at=now, original="abce",
                    edit_distance=1)
    _draft(csp, "no thanks", status="rejected", reviewed_at=now)
    _draft(csp, "still open")  # undecided: not counted
    _draft(csp, "before the trail", status="approved", reviewed_at=now, original="")  # excluded
    q = quality.agent_quality(30)["implicit_acceptance"]
    counts = (q["decided"], q["kept_as_written"], q["edited_then_kept"], q["rejected"])
    assert counts == (3, 1, 1, 1)
    assert q["implicit_acceptance_rate"] == pytest.approx(1 / 3)
    assert q["status"] == "below_small_sample"
    cd = quality.agent_quality(30)["correction_delta"]
    assert cd["value"] == pytest.approx((0 + 1 / 4) / 2) and cd["n"] == 2
    assert kept.edit_distance is None  # plain approval stores nothing; the measure still sees 0


@pytest.mark.django_db
def test_decisions_outside_the_window_are_ignored(csps):
    old = timezone.now() - dt.timedelta(days=60)
    _draft(csps[1][0], "old", status="approved", reviewed_at=old)
    assert quality.agent_quality(30)["implicit_acceptance"]["decided"] == 0
    assert quality.agent_quality(90)["implicit_acceptance"]["decided"] == 1


@pytest.mark.django_db
def test_call_reliability_and_token_efficiency():
    for outcome, p, c in (("ok", 100, 50), ("ok", 80, 40), ("unavailable", None, None),
                          ("bad_json", 10, 5)):
        AgentInteraction.objects.create(outcome=outcome, prompt_tokens=p, completion_tokens=c)
    q = quality.agent_quality(30)
    assert q["call_reliability"]["rate"] == pytest.approx(0.5)
    assert q["call_reliability"]["status"] == "below_small_sample"
    eff = q["context_efficiency"]
    assert eff["calls"] == 2 and eff["mean_tokens_per_call"] == pytest.approx((150 + 120) / 2)


@pytest.mark.django_db
def test_investigation_completion_ignores_running_tasks():
    for status in ("completed", "completed", "needs_data", "failed", "running"):
        AgentTask.objects.create(description="t", status=status)
    run = quality.agent_quality(30)["investigation_completion"]
    assert (run["finished"], run["completed"], run["needs_data"], run["failed"]) == (4, 2, 1, 1)
    assert run["rate"] == pytest.approx(0.5)


@pytest.mark.django_db
def test_unmeasured_things_are_listed_not_invented():
    names = [n["name"] for n in quality.agent_quality(30)["not_measured"]]
    assert "Retrieval precision and recall" in names


# ------------------------------------------------------------------ the pages and the API
@pytest.fixture
def staff_client(client, user):
    client.login(username="ops", password="pw12345")
    return client


@pytest.mark.django_db
def test_quality_page_is_staff_only(client):
    get_user_model().objects.create_user(username="viewer", password="pw12345")
    client.login(username="viewer", password="pw12345")
    assert client.get("/dashboard/agent-quality/").status_code == 403


@pytest.mark.django_db
def test_quality_page_renders_with_and_without_data(staff_client, csps):
    page = staff_client.get("/dashboard/agent-quality/")
    assert page.status_code == 200 and b"No data yet" in page.content
    _draft(csps[1][0], "ok", status="approved", reviewed_at=timezone.now())
    page = staff_client.get("/dashboard/agent-quality/?days=7")
    assert page.status_code == 200 and b"Kept as written" in page.content
    assert staff_client.get("/dashboard/agent-quality/?days=abc").status_code == 200


@pytest.mark.django_db
def test_messaging_edit_and_approve_through_the_page(staff_client, csps):
    draft = _draft(csps[1][0], "You are Rs 500 away.", context={"gap": 500.0, "slab": "S2"})
    page = staff_client.get("/dashboard/messaging/")
    assert b"Why this message?" in page.content and b"Gap to the next slab" in page.content
    assert b"Edit, then approve" in page.content
    response = staff_client.post("/dashboard/messaging/", {
        "message_id": draft.id, "action": "edit_approve", "text": "You are Rs 400 away."})
    assert response.status_code == 302
    draft.refresh_from_db()
    assert draft.status == "approved" and draft.text == "You are Rs 400 away."
    assert draft.edit_distance == 1
    shown = staff_client.get("/dashboard/messaging/")
    assert b"edited by a person" in shown.content
    assert b"What the assistant first wrote" in shown.content


@pytest.mark.django_db
def test_plain_approve_and_reject_still_work(staff_client, csps, user):
    a, b = _draft(csps[1][0], "A"), _draft(csps[1][0], "B")
    assert approve_draft_message(a, user) and reject_draft_message(b, user)
    a.refresh_from_db(), b.refresh_from_db()
    assert (a.status, b.status) == ("approved", "rejected")


def _key(scopes: str) -> str:
    consumer = ApiConsumer.objects.create(name=f"quality-{generate_raw_key()[:8]}")
    raw = generate_raw_key()
    ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                          scopes=scopes)
    return raw


@pytest.mark.django_db
def test_api_needs_a_key_with_report_scope_and_returns_aggregates_only(client, csps):
    url = "/api/v1/reports/agent-quality?days=30"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-API-Key": _key("csp:read")}).status_code == 403
    _draft(csps[1][0], "secret wording", status="approved", reviewed_at=timezone.now())
    ok = client.get(url, headers={"X-API-Key": _key("report:read")})
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["days"] == 30 and data["implicit_acceptance"]["decided"] == 1
    assert b"secret wording" not in ok.content  # counts and rates, never message text


# ------------------------------------------------------------------ golden cases
@pytest.mark.django_db
@pytest.mark.parametrize("case", GOLDEN["cases"], ids=[c["name"] for c in GOLDEN["cases"]])
def test_golden_nudge_replies(csps, case):
    month, _ = csps
    with patch("autopilot.nemotron_client._client",
               return_value=_client_returning(json.dumps(case["reply"]))):
        draft_csp_nudges(month=month)
    assert sorted(DraftMessage.objects.values_list("text", flat=True)) == sorted(case["kept"])
    assert AgentInteraction.objects.filter(purpose="nudge_drafts", outcome="ok").count() == 1


def test_golden_candidates_match_the_fixture():
    assert GOLDEN["candidates"] == ["1A850001", "1A850002"]
