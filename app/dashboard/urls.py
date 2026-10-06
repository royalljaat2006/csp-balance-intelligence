from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="dashboard/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(next_page="dashboard:login"), name="logout"),
    path("", views.home, name="home"),
    path("balance-intelligence/", views.balance_intelligence, name="balance_intelligence"),
    path("trends/", views.trends, name="trends"),
    path("comparisons/", views.csp_comparisons, name="csp_comparisons"),
    path("csps/", views.csp_directory, name="csp_directory"),
    path("transactions/", views.transactions, name="transactions"),
    path("api-docs/", views.api_docs, name="api_docs"),
    path("admin-console/", views.admin_embed, name="admin_embed"),
    path("system-health/", views.system_health, name="system_health"),
    path("pipeline/", views.pipeline, name="pipeline"),
    path("pipeline/events", views.pipeline_events, name="pipeline_events"),
    path("pipeline/history", views.pipeline_history, name="pipeline_history"),
    path("pipeline/node/<str:node_key>", views.pipeline_node_detail, name="pipeline_node_detail"),
    path("messaging/", views.messaging, name="messaging"),
    path("ai/", views.ai_operations, name="ai_operations"),
    path("agent-monitoring/", views.agent_monitoring, name="agent_monitoring"),
    path("risk/", views.risk, name="risk"),
    path("agent-findings/", views.agent_findings, name="agent_findings"),
    path("verification/", views.verification, name="verification"),
    path("actions/", views.actions, name="actions"),
    path("approvals/", views.approvals, name="approvals"),
    path("ai-recommendations/", views.ai_recommendations, name="ai_recommendations"),
    path("audit/", views.audit, name="audit"),
    path("csp/<str:csp_code>/", views.csp_detail, name="csp_detail"),
    path("csp/<str:csp_code>/activity.json", views.csp_activity_json, name="csp_activity_json"),
]
