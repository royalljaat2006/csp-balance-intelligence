"""
The Assistant activity page and its API list: every model call with what it was shown and what it
answered, the drafts it produced, and what people changed. Staff only; the API never carries text.
"""

import datetime as dt

import pytest
from api.auth import generate_raw_key, hash_key
from api.models import ApiConsumer, ApiKey
from autopilot import activity
from autopilot.models import AgentInteraction, DraftMessage
from csp.models import Csp
from django.contrib.auth import get_user_model
from django.utils import timezone

SECRET_CONTEXT = '{"csp_code": "1A850001", "gap": 501.0, "name": "Alpha Secret Name"}'


@pytest.fixture
def staff_client(client, db):
    get_user_model().objects.create_user(username="ops", password="pw12345", is_staff=True)
    client.login(username="ops", password="pw12345")
    return client


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850001", name="Alpha CSP", account_count=300)


def _call(purpose="nudge_drafts", outcome="ok", **kw):
    defaults = dict(model="m-1", system_prompt="Write short SMS drafts.", context=SECRET_CONTEXT,
                    raw_output='{"drafts": []}', prompt_tokens=100, completion_tokens=40,
                    finish_reason="stop", latency_ms=900)
    return AgentInteraction.objects.create(purpose=purpose, outcome=outcome, **{**defaults, **kw})


# ------------------------------------------------------------------ access
@pytest.mark.django_db
def test_requires_login_and_staff(client):
    assert client.get("/dashboard/agent-activity/").status_code == 302
    get_user_model().objects.create_user(username="viewer", password="pw12345")
    client.login(username="viewer", password="pw12345")
    assert client.get("/dashboard/agent-activity/").status_code == 403


# ------------------------------------------------------------------ the page
@pytest.mark.django_db
def test_empty_page_says_so(staff_client):
    page = staff_client.get("/dashboard/agent-activity/")
    assert page.status_code == 200 and b"No model calls in this period" in page.content
    assert b"No reworded drafts yet" in page.content


@pytest.mark.django_db
def test_a_call_shows_what_it_saw_and_what_it_said(staff_client):
    _call(raw_output='{"drafts": [{"csp_code": "1A850001", "text": "Hello"}]}')
    html = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert "nudge_drafts" in html and "Answered" in html and "140 tokens" in html
    assert "Write short SMS drafts." in html          # the instructions
    assert "Alpha Secret Name" in html and "501.0" in html  # the figures it was shown
    assert "&quot;text&quot;: &quot;Hello&quot;" in html  # the answer, escaped and re-indented


@pytest.mark.django_db
def test_failed_calls_show_what_went_wrong(staff_client):
    _call(outcome="unavailable", raw_output="", prompt_tokens=None, completion_tokens=None,
          error="NotFoundError: model not available for this account")
    html = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert "Unavailable" in html and "What went wrong" in html
    assert "model not available for this account" in html and "No answer came back." in html


@pytest.mark.django_db
def test_page_text_is_escaped(staff_client):
    _call(system_prompt="<script>alert(1)</script>", raw_output="<img src=x onerror=alert(1)>")
    html = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert "<script>alert(1)</script>" not in html and "<img src=x" not in html


@pytest.mark.django_db
def test_filters_and_period(staff_client):
    _call(purpose="daily_briefing")
    _call(purpose="nudge_drafts", outcome="unavailable")
    old = _call(purpose="priority_calls")
    long_ago = timezone.now() - dt.timedelta(days=60)
    AgentInteraction.objects.filter(pk=old.pk).update(created_at=long_ago)
    base = "/dashboard/agent-activity/"
    html = staff_client.get(base + "?purpose=daily_briefing").content.decode()
    assert "daily_briefing" in html and "<b>nudge_drafts</b>" not in html
    html = staff_client.get(base + "?outcome=unavailable").content.decode()
    assert "<b>nudge_drafts</b>" in html and "<b>daily_briefing</b>" not in html
    assert "<b>priority_calls</b>" not in staff_client.get(base).content.decode()  # 60 days old
    assert "<b>priority_calls</b>" in staff_client.get(base + "?days=90").content.decode()
    assert staff_client.get(base + "?days=abc&page=zz&outcome=bogus").status_code == 200


@pytest.mark.django_db
def test_pagination(staff_client):
    for _ in range(17):
        _call()
    first = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert first.count('<details class="more call">') == 15 and "Page 1 of 2" in first
    second = staff_client.get("/dashboard/agent-activity/?page=2").content.decode()
    assert second.count('<details class="more call">') == 2


@pytest.mark.django_db
def test_drafts_made_by_a_call_and_the_words_people_changed(staff_client, csp):
    call = _call()
    user = get_user_model().objects.get(username="ops")
    DraftMessage.objects.create(
        csp=csp, interaction=call, status="approved", reviewed_by=user,
        text="You are Rs 400 away from S2.", original_text="You are Rs 500 away from S2.",
        edit_distance=1, edited_at=timezone.now())
    html = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert "Drafts this call produced" in html
    assert "reworded by a person (1 character changed)" in html
    assert "<del><span class=\"sr-only\">removed: </span>500</del>" in html
    assert "<ins><span class=\"sr-only\">added: </span>400</ins>" in html
    assert "ops" in html  # who made the change


@pytest.mark.django_db
def test_long_text_is_shortened_on_the_page_only(staff_client):
    row = _call(context="x" * (activity.SHOW_CHARS + 500))
    html = staff_client.get("/dashboard/agent-activity/").content.decode()
    assert "Shortened here; the full text is stored." in html
    row.refresh_from_db()
    assert len(row.context) == activity.SHOW_CHARS + 500


@pytest.mark.django_db
def test_summary_counts(csp):
    ok = _call()
    _call(outcome="unavailable", prompt_tokens=None, completion_tokens=None)
    DraftMessage.objects.create(csp=csp, interaction=ok, text="a", original_text="a")
    DraftMessage.objects.create(
        csp=csp, interaction=ok, text="b", original_text="c", edit_distance=1)
    s = activity.summary(30)
    assert (s["calls"], s["answered"], s["unavailable"], s["bad_json"]) == (2, 1, 1, 0)
    assert (s["drafts_written"], s["drafts_edited"]) == (2, 1)
    assert s["mean_tokens"] == 140 and s["last_call"] is not None


@pytest.mark.django_db
def test_summary_with_nothing_is_empty_not_zero_tokens():
    s = activity.summary(30)
    assert s["calls"] == 0 and s["mean_tokens"] is None and s["last_call"] is None


# ------------------------------------------------------------------ the API list
def _key(scopes):
    consumer = ApiConsumer.objects.create(name=f"act-{generate_raw_key()[:8]}")
    raw = generate_raw_key()
    ApiKey.objects.create(
        consumer=consumer, key_prefix=raw[:12], key_hash=hash_key(raw), scopes=scopes)
    return raw


@pytest.mark.django_db
def test_api_lists_metadata_only(client):
    _call()
    url = "/api/v1/reports/agent-interactions"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-API-Key": _key("csp:read")}).status_code == 403
    ok = client.get(url, headers={"X-API-Key": _key("report:read")})
    assert ok.status_code == 200
    body = ok.json()
    assert body["pagination"]["total"] == 1
    row = body["data"][0]
    assert row["purpose"] == "nudge_drafts" and row["total_tokens"] == 140
    for hidden in (b"Alpha Secret Name", b"Write short SMS drafts.", b'{\\"drafts\\"'):
        assert hidden not in ok.content
    assert not {"system_prompt", "context", "raw_output", "error"} & set(row)


@pytest.mark.django_db
def test_api_filters_and_paging(client):
    for _ in range(3):
        _call()
    _call(purpose="daily_briefing", outcome="unavailable")
    key = {"X-API-Key": _key("report:read")}
    url = "/api/v1/reports/agent-interactions"
    only_briefing = client.get(url + "?purpose=daily_briefing", headers=key).json()
    assert only_briefing["pagination"]["total"] == 1
    assert client.get(url + "?outcome=ok&limit=2", headers=key).json()["pagination"] == {
        "limit": 2, "offset": 0, "total": 3}
    assert len(client.get(url + "?limit=2&offset=2", headers=key).json()["data"]) == 2
    assert client.get(url + "?limit=99999", headers=key).json()["pagination"]["limit"] == 500
