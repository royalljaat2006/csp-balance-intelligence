"""
DraftMessage's admin actions are the one deliberate human gate autopilot
has (see models.DraftMessage docstring) — these tests exist specifically
to prove that gate actually changes status/audit fields, and that nothing
in this app can move a message to SENT on its own (there's no action that
does that; SENT is reachable only by direct DB/future-integration writes).
"""

import pytest
from autopilot.admin import AnomalyFlagAdmin, DraftMessageAdmin
from autopilot.models import AnomalyFlag, DraftMessage
from csp.models import Csp, IngestLog
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory


@pytest.fixture
def admin_user(db):
    return get_user_model().objects.create_superuser(
        username="admin", password="pw12345", email="admin@example.com"
    )


@pytest.fixture
def csp(db):
    return Csp.objects.create(csp_code="1A850001", name="Test CSP")


def _request_with_messages(admin_user):
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    # Django admin actions call self.message_user(), which needs the
    # messages framework wired onto the request.
    from django.contrib.messages.storage.fallback import FallbackStorage

    request.session = {}
    request._messages = FallbackStorage(request)
    return request


@pytest.mark.django_db
def test_approve_drafts_moves_status_and_sets_reviewer(csp, admin_user):
    draft = DraftMessage.objects.create(csp=csp, text="Hello", status=DraftMessage.Status.DRAFT)
    admin = DraftMessageAdmin(DraftMessage, AdminSite())
    request = _request_with_messages(admin_user)

    admin.approve_drafts(request, DraftMessage.objects.filter(pk=draft.pk))

    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.APPROVED
    assert draft.reviewed_by_id == admin_user.id
    assert draft.reviewed_at is not None


@pytest.mark.django_db
def test_approve_drafts_does_not_touch_already_sent(csp, admin_user):
    sent = DraftMessage.objects.create(
        csp=csp, text="Already sent", status=DraftMessage.Status.SENT
    )
    admin = DraftMessageAdmin(DraftMessage, AdminSite())
    request = _request_with_messages(admin_user)

    admin.approve_drafts(request, DraftMessage.objects.filter(pk=sent.pk))

    sent.refresh_from_db()
    assert sent.status == DraftMessage.Status.SENT  # untouched, not silently re-approved


@pytest.mark.django_db
def test_reject_drafts_moves_status(csp, admin_user):
    draft = DraftMessage.objects.create(csp=csp, text="Hello", status=DraftMessage.Status.DRAFT)
    admin = DraftMessageAdmin(DraftMessage, AdminSite())
    request = _request_with_messages(admin_user)

    admin.reject_drafts(request, DraftMessage.objects.filter(pk=draft.pk))

    draft.refresh_from_db()
    assert draft.status == DraftMessage.Status.REJECTED


@pytest.mark.django_db
def test_draft_message_admin_has_no_add_permission(admin_user):
    admin = DraftMessageAdmin(DraftMessage, AdminSite())
    request = _request_with_messages(admin_user)
    assert admin.has_add_permission(request) is False


@pytest.mark.django_db
def test_mark_reviewed_sets_reviewer(admin_user):
    log = IngestLog.objects.create(source="calling_sheet")
    flag = AnomalyFlag.objects.create(ingest_log=log, description="Odd", severity="high")
    admin = AnomalyFlagAdmin(AnomalyFlag, AdminSite())
    request = _request_with_messages(admin_user)

    admin.mark_reviewed(request, AnomalyFlag.objects.filter(pk=flag.pk))

    flag.refresh_from_db()
    assert flag.reviewed_at is not None
    assert flag.reviewed_by_id == admin_user.id
