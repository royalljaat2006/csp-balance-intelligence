from django.contrib.auth import views as auth_views
from django.urls import path

from . import pages, views

app_name = "dashboard"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="dashboard/login.html"),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(next_page="dashboard:login"), name="logout"),
    # the journey
    path("", pages.home, name="home"),
    path("readiness/", pages.data_readiness, name="data_readiness"),
    path("csps/", pages.csp_directory, name="csp_directory"),
    path("csp/<str:csp_code>/", views.csp_detail, name="csp_detail"),
    path("csp/<str:csp_code>/explain/", pages.csp_explain, name="csp_explain"),
    path("csp/<str:csp_code>/activity.json", views.csp_activity_json, name="csp_activity_json"),
    path("comparisons/", views.csp_comparisons, name="csp_comparisons"),
    path("balance-intelligence/", views.balance_intelligence, name="balance_intelligence"),
    path("money/", views.money_movement, name="money_movement"),
    path("transactions/", views.transactions, name="transactions"),
    path("trends/", views.trends, name="trends"),
    path("needs-attention/", pages.needs_attention, name="needs_attention"),
    path("sbi-comparison/", pages.bank_flow, name="bank_flow"),
    path("month-end/", pages.month_end, name="month_end"),
    path("shell/", views.shell_data, name="shell_data"),
    # operations (staff)
    path("more/", views.more, name="more"),
    path("system-health/", views.system_health, name="system_health"),
    path("pipeline/", views.pipeline, name="pipeline"),
    path("pipeline/events", views.pipeline_events, name="pipeline_events"),
    path("pipeline/history", views.pipeline_history, name="pipeline_history"),
    path("pipeline/node/<str:node_key>", views.pipeline_node_detail, name="pipeline_node_detail"),
    path("audit/", views.audit, name="audit"),
    path("messaging/", views.messaging, name="messaging"),
    path("ai/", views.ai_operations, name="ai_operations"),
    path("agent-quality/", views.agent_quality, name="agent_quality"),
    path("agent-activity/", views.agent_activity, name="agent_activity"),
    path("agent-monitoring/", views.agent_monitoring, name="agent_monitoring"),
    path("risk/", views.risk, name="risk"),
    path("agent-findings/", views.agent_findings, name="agent_findings"),
    path("verification/", views.verification, name="verification"),
    path("actions/", views.actions, name="actions"),
    path("approvals/", views.approvals, name="approvals"),
    path("ai-recommendations/", views.ai_recommendations, name="ai_recommendations"),
    path("api-docs/", views.api_docs, name="api_docs"),
    path("admin-console/", views.admin_embed, name="admin_embed"),
    # pages that moved: old links and bookmarks keep working
    path("overview/", pages.legacy_redirect("dashboard:home"), name="overview_classic"),
    path("progress/", pages.legacy_redirect("dashboard:data_readiness"), name="data_progress"),
    path("bank-flow/", pages.legacy_redirect("dashboard:bank_flow"), name="bank_flow_legacy"),
]
