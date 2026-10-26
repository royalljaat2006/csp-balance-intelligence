"""
The protected API and the Agent Learning Centre page: who may read, who may write, what is shown
when the evidence is not there, and that no invented accuracy figure ever appears.

SYNTHETIC data (invented CSPs and amounts).
"""

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from django.contrib.auth import get_user_model
from learning import candidates, feedback, registry
from learning.models import LivePrediction, PredictionFeedback

from tests.learning_helpers import d, make_csp, stored_outcome, stored_prediction, synth

pytestmark = pytest.mark.django_db
API = "/api/v1/learning"


def _key(scopes: str) -> dict:
    consumer = ApiConsumer.objects.create(name=f"learn-{generate_raw_key()[:8]}")
    raw = generate_raw_key()
    ApiKey.objects.create(consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw),
                          scopes=scopes)
    return {"X-API-Key": raw}


@pytest.fixture
def pred():
    make_csp()
    p = stored_prediction(estimate=d(2026, 2, 3), first_seen=d(2026, 2, 1), predicted="1010")
    return p


# ------------------------------------------------------------------ API: who may read
READS = ["/predictions", "/predictions/1", "/evaluation", "/models", "/feedback"]


@pytest.mark.parametrize("path", READS)
def test_every_read_needs_a_key_with_the_report_scope(client, pred, path):
    assert client.get(API + path).status_code == 401
    assert client.get(API + path, headers=_key("csp:read")).status_code == 403
    assert client.get(API + path, headers=_key("feedback:write")).status_code == 403
    ok = client.get(API + path.replace("/1", f"/{pred.pk}"), headers=_key("report:read"))
    assert ok.status_code == 200


def test_a_revoked_or_unknown_key_is_refused(client, pred):
    assert client.get(API + "/predictions", headers={"X-API-Key": "nope"}).status_code == 401


def test_predictions_show_the_observed_baseline_the_estimate_and_the_outcome_apart(client, pred):
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    body = client.get(API + "/predictions", headers=_key("report:read")).json()
    assert body["pagination"]["total"] == 1
    row = body["data"][0]
    assert row["observed_baseline"]["avg_balance"] == 1000.0
    assert row["observed_baseline"]["first_seen"] == "2026-02-01"
    assert row["prediction"]["avg_balance"] == 1010.0
    assert row["prediction"]["carry_forward"] == 1000.0
    assert row["flow"]["net_pool_flow"] == -row["flow"]["net_customer_movement"]
    out = row["outcome"]
    assert out["observed_avg_balance"] == 1020.0 and out["error"] == -10.0
    assert out["error_direction"] == "UNDER" and out["horizon_days"] == 5
    assert out["data_quality"] == "OK"
    assert row["model"] == "movement" and row["horizon"] == "next_sbi_refresh"


def test_predictions_can_be_filtered_and_unscored_ones_have_no_outcome(client, pred):
    make_csp("1A850002")
    stored_prediction("1A850002", estimate=d(2026, 2, 4), first_seen=d(2026, 2, 1))
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    key = _key("report:read")
    get = lambda q: client.get(f"{API}/predictions?{q}", headers=key).json()  # noqa: E731
    assert get("scored=true")["pagination"]["total"] == 1
    unscored = get("scored=false")
    assert unscored["pagination"]["total"] == 1 and unscored["data"][0]["outcome"] is None
    assert get("csp=1A850002")["pagination"]["total"] == 1
    assert get("date_from=2026-02-04")["pagination"]["total"] == 1
    assert get("limit=99999")["pagination"]["limit"] == 500
    assert client.get(f"{API}/predictions?date_from=2020-01-01&date_to=2026-01-01",
                      headers=key).status_code == 422  # span over a year


def test_one_prediction_and_a_missing_one(client, pred):
    key = _key("report:read")
    assert client.get(f"{API}/predictions/{pred.pk}", headers=key).json()["data"]["id"] == pred.pk
    assert client.get(f"{API}/predictions/999999", headers=key).status_code == 404


def test_evaluation_says_insufficient_evidence_and_gives_no_figure(client, pred):
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    data = client.get(f"{API}/evaluation", headers=_key("report:read")).json()["data"]
    assert data["accuracy_claim"] == "INSUFFICIENT_EVIDENCE" and data["metrics"] is None
    assert data["evaluated"] == 1 and data["headline_outcomes"] == 1


def test_evaluation_gives_the_figures_once_the_evidence_bar_is_met(client):
    synth(months=8, csps=4)
    data = client.get(f"{API}/evaluation", headers=_key("report:read")).json()["data"]
    assert data["accuracy_claim"] == "VALIDATED" and data["metrics"]["n"] == 32
    assert data["metrics"]["mae"] == pytest.approx(5.0)
    assert data["data_quality"]["ok"] == 32


def test_models_lists_the_active_method_the_candidates_and_the_history(client):
    registry.ensure_registry()
    candidates.evaluate_candidates()
    data = client.get(f"{API}/models", headers=_key("report:read")).json()["data"]
    assert data["active"]["model"] == "movement"
    by_name = {m["name"]: m for m in data["models"]}
    assert set(by_name) == {"carry_forward", "movement", "flow_only", "account_count_only"}
    assert by_name["account_count_only"]["blocked_reason"]
    assert by_name["flow_only"]["latest_evaluation"]["verdict"] == "INSUFFICIENT_EVIDENCE"
    assert [h["kind"] for h in data["history"]] == ["REGISTERED"] * 4


# ------------------------------------------------------------------ API: who may write
def _post(client, pk, body, headers):
    return client.post(f"{API}/predictions/{pk}/feedback", body, content_type="application/json",
                       headers=headers)


def test_only_the_write_scope_can_submit_feedback(client, pred):
    body = {"verdict": "LOOKS_WRONG", "reason": "MISSING_DATA", "note": "no statement for 3 Feb"}
    assert _post(client, pred.pk, body, {}).status_code == 401
    assert _post(client, pred.pk, body, _key("report:read")).status_code == 403  # read-only key
    assert _post(client, pred.pk, body, _key("report:read,csp:read")).status_code == 403
    assert PredictionFeedback.objects.count() == 0
    ok = _post(client, pred.pk, body, _key("feedback:write"))
    assert ok.status_code == 201
    fb = PredictionFeedback.objects.get()
    assert fb.submitted_by.startswith("api:learn-") and fb.status == "OPEN"
    assert ok.json()["data"]["id"] == fb.pk
    assert fb.events.count() == 1  # the audit trail starts at submission


def test_api_feedback_is_validated_and_screened(client, pred):
    key = _key("feedback:write")
    bad = [
        {"verdict": "AMAZING"},
        {"verdict": "LOOKS_WRONG"},                       # no reason
        {"verdict": "USEFUL", "reason": "BECAUSE"},
        {"verdict": "UNSURE", "reason": "OTHER", "note": "ring 9876543210"},
        {"verdict": "UNSURE", "reason": "OTHER", "note": "a@b.com"},
        {"verdict": "UNSURE", "reason": "OTHER", "note": "x" * 501},
    ]
    for body in bad:
        assert _post(client, pred.pk, body, key).status_code == 422, body
    assert _post(client, 999999, {"verdict": "USEFUL"}, key).status_code == 404
    assert PredictionFeedback.objects.count() == 0


def test_api_feedback_cannot_carry_a_value_and_never_changes_the_outcome(client, pred):
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    key = _key("feedback:write")
    r = _post(client, pred.pk, {"verdict": "LOOKS_WRONG", "reason": "MODEL_ERROR",
                                "actual_value": 5000, "observed": 5000}, key)
    assert r.status_code == 201  # unknown fields are ignored, not stored
    out = client.get(f"{API}/predictions/{pred.pk}", headers=_key("report:read")).json()["data"]
    assert out["outcome"]["observed_avg_balance"] == 1020.0
    assert not hasattr(PredictionFeedback.objects.get(), "actual_value")


def test_feedback_is_listed_with_its_trail(client, pred):
    fb = feedback.submit(pred, "ops", "UNSURE", "OTHER", "check this")
    staff = get_user_model().objects.create_user(username="r", password="x", is_staff=True)
    feedback.set_status(fb, staff, "IN_REVIEW", "looking")
    body = client.get(f"{API}/feedback?status=IN_REVIEW", headers=_key("report:read")).json()
    assert body["pagination"]["total"] == 1
    row = body["data"][0]
    assert row["status"] == "IN_REVIEW" and [e["action"] for e in row["events"]] == [
        "SUBMITTED", "STATUS_CHANGED"]
    assert client.get(f"{API}/feedback?status=OPEN", headers=_key("report:read")).json()[
        "pagination"]["total"] == 0


def test_there_is_no_write_endpoint_for_outcomes_models_or_status(client, pred):
    key = _key("feedback:write,report:read")
    for path in ("/predictions", "/evaluation", "/models", "/feedback"):
        assert client.post(API + path, {}, content_type="application/json",
                           headers=key).status_code in (404, 405)
    assert client.delete(f"{API}/predictions/{pred.pk}", headers=key).status_code in (404, 405)
    assert client.patch(f"{API}/predictions/{pred.pk}", {}, content_type="application/json",
                        headers=key).status_code in (404, 405)


# ------------------------------------------------------------------ the dashboard page
@pytest.fixture
def staff_client(client):
    get_user_model().objects.create_user(username="ops", password="pw12345", is_staff=True)
    client.login(username="ops", password="pw12345")
    return client


PAGE = "/dashboard/learning/"


def test_the_page_is_staff_only(client):
    assert client.get(PAGE).status_code == 302
    get_user_model().objects.create_user(username="viewer", password="pw12345")
    client.login(username="viewer", password="pw12345")
    assert client.get(PAGE).status_code == 403


def test_before_anything_is_saved_the_page_explains_how_to_start(staff_client):
    html = staff_client.get(PAGE).content.decode()
    assert "The learning loop has not been started" in html and "freeze_predictions" in html


def test_with_too_little_evidence_the_page_shows_no_accuracy_number(staff_client, pred):
    registry.ensure_registry()
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    html = staff_client.get(PAGE).content.decode()
    assert "Not enough evidence yet" in html
    assert "We cannot say how accurate the estimate is yet" in html
    assert "Typical miss" not in html and "Leans high or low" not in html
    assert "Shadow estimate" in html and "Approved for production" in html
    assert "Not granted" in html and "carry-forward" in html.lower()
    assert "Estimated versus what SBI reported" in html and "1,010" in html and "1,020" in html


def test_with_enough_evidence_the_page_shows_the_figures_and_their_basis(staff_client):
    registry.ensure_registry()
    synth(months=8, csps=4)
    html = staff_client.get(PAGE).content.decode()
    assert "Evidence bar met" in html and "Typical miss (average)" in html
    assert "32 (one per CSP per SBI refresh" in html
    assert "Does it lean high or low?" in html and "Not enough checks" in html


def test_data_problems_are_explained_apart_from_model_error(staff_client, pred):
    registry.ensure_registry()
    bad = stored_prediction(estimate=d(2026, 2, 4), first_seen=d(2026, 2, 1), state="provisional")
    stored_outcome(bad, 1020, event=d(2026, 2, 8))
    html = staff_client.get(PAGE).content.decode()
    assert "made from incomplete data" in html and "left out of the accuracy figures" in html


def test_candidate_methods_are_listed_with_why_they_cannot_be_switched_on(staff_client):
    registry.ensure_registry()
    candidates.evaluate_candidates()
    html = staff_client.get(PAGE).content.decode()
    for label in ("Account growth and transaction flow", "Transaction flow only",
                  "Account count only", "Carry-forward"):
        assert label in html
    assert "Blocked: Business rule" in html and "Cannot be switched on yet" in html
    assert "Method change history" in html and "Registered" in html


def test_feedback_can_be_given_and_reviewed_from_the_page(staff_client, pred):
    registry.ensure_registry()
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    r = staff_client.post(PAGE, {"action": "feedback", "prediction_id": pred.pk,
                                 "verdict": "LOOKS_WRONG", "reason": "UNUSUAL_ACTIVITY",
                                 "note": "festival week"})
    assert r.status_code == 302
    fb = PredictionFeedback.objects.get()
    assert fb.submitted_by == "ops" and fb.reason == "UNUSUAL_ACTIVITY"
    assert "festival week" in staff_client.get(PAGE).content.decode()
    r = staff_client.post(PAGE, {"action": "feedback_status", "feedback_id": fb.pk,
                                 "status": "RESOLVED", "note": "confirmed with the field team"})
    assert r.status_code == 302
    fb.refresh_from_db()
    assert fb.status == "RESOLVED" and fb.resolved_by == "ops"
    assert "festival week" not in staff_client.get(PAGE).content.decode()  # no longer waiting


def test_a_bad_form_shows_the_reason_and_saves_nothing(staff_client, pred):
    registry.ensure_registry()
    r = staff_client.post(PAGE, {"action": "feedback", "prediction_id": pred.pk,
                                 "verdict": "LOOKS_WRONG", "reason": "OTHER",
                                 "note": "call 9876543210"})
    assert r.status_code == 200 and b"remove account numbers" in r.content
    assert PredictionFeedback.objects.count() == 0
    r = staff_client.post(PAGE, {"action": "nonsense"})
    assert r.status_code == 200 and b"not available" in r.content


def test_the_page_never_offers_a_way_to_promote_or_edit_history(staff_client, pred):
    registry.ensure_registry()
    html = staff_client.get(PAGE).content.decode()
    assert "promote" not in html.lower() and 'name="actual_value"' not in html
    before = LivePrediction.objects.count()
    staff_client.post(PAGE, {"action": "promote", "model": "flow_only"})
    assert registry.active_model().name == "movement" and LivePrediction.objects.count() == before


def test_the_page_is_reachable_from_the_navigation(staff_client):
    assert b"Agent learning centre" in staff_client.get("/dashboard/more/").content


def test_the_page_uses_everyday_words_not_the_internal_codes(staff_client, pred):
    registry.ensure_registry()
    candidates.evaluate_candidates()
    stored_outcome(pred, 1020, event=d(2026, 2, 8))
    html = staff_client.get(PAGE).content.decode()
    assert "Not tested yet" in html and "NOT_RUN" not in html
    for jargon in ("INSUFFICIENT_EVIDENCE", "pairs over", "CI above", "MAE gain",
                   "monthly cycle(s)"):
        assert jargon not in html, jargon
    assert "Only 1 check over 1 SBI refresh day in 1 month so far" in html
    assert "A business rule stops this method being used" in html
