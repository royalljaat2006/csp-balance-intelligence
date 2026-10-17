"""
Navigation: what the sidebar shows, which section a page belongs to, and the tabs inside a section.

The sidebar follows the operational journey (see the page order below). Every route keeps its own
permission check in the view; this module only decides what to show, and hides staff-only entries
from non-staff so there is never a link that leads to "forbidden".
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

#: url_name -> section key
SECTION_OF: dict[str, str] = {
    "home": "overview", "shell_data": "overview", "heat_data": "overview",
    "overview_classic": "overview",
    "data_readiness": "readiness", "data_progress": "readiness",
    "csp_directory": "csps", "csp_detail": "csps", "csp_explain": "csps",
    "csp_activity_json": "csps", "csp_comparisons": "csps", "balance_intelligence": "csps",
    "money_movement": "money", "transactions": "money", "trends": "money",
    "needs_attention": "review", "risk": "review", "actions": "review", "approvals": "review",
    "verification": "review", "agent_findings": "review",
    "bank_flow": "sbi",
    "month_end": "outlook",
    "more": "ops", "messaging": "ops", "ai_operations": "ops", "ai_recommendations": "ops",
    "agent_monitoring": "ops", "system_health": "ops", "pipeline": "ops",
    "pipeline_events": "ops", "pipeline_history": "ops", "pipeline_node_detail": "ops",
    "api_docs": "ops", "audit": "ops", "admin_embed": "ops",
}

#: pages that show the "today versus yesterday" strip (today is the present IST day)
COMPARE_PAGES = frozenset({
    "home", "money_movement", "transactions", "csp_comparisons", "trends", "csp_directory",
    "balance_intelligence", "needs_attention", "month_end", "overview_classic",
})


@dataclass(frozen=True)
class Tab:
    label: str
    url_name: str
    staff_only: bool = False
    active_names: tuple[str, ...] = ()


#: (key, label, url_name, icon, staff_only, group)
PRIMARY = [
    ("overview", "Overview", "home", "home", False, "Work"),
    ("readiness", "Data readiness", "data_readiness", "check", False, "Work"),
    ("csps", "CSP balances", "csp_directory", "users", False, "Work"),
    ("money", "Money movement", "money_movement", "flow", False, "Work"),
    ("review", "Review", "needs_attention", "flag", False, "Work"),
    ("sbi", "SBI comparison", "bank_flow", "scale", True, "Validate"),
    ("outlook", "Month-end outlook", "month_end", "calendar", False, "Validate"),
    ("ops", "Operations", "more", "gear", False, "Tools"),
]

TABS: dict[str, list[Tab]] = {
    "csps": [
        Tab("All CSPs", "csp_directory",
            active_names=("csp_directory", "csp_detail", "csp_explain")),
        Tab("Day by day", "csp_comparisons"),
        Tab("Incentive slabs", "balance_intelligence"),
    ],
    "money": [
        Tab("Summary", "money_movement"),
        Tab("Transaction workbook", "transactions"),
        Tab("Trends", "trends"),
    ],
    "review": [
        Tab("Data to review", "needs_attention"),
        Tab("Risk", "risk", staff_only=True),
        Tab("Assistant findings", "agent_findings", staff_only=True),
        Tab("Verification", "verification", staff_only=True),
        Tab("Proposed actions", "actions", staff_only=True),
        Tab("Approvals", "approvals", staff_only=True),
    ],
}

#: Operations hub: (group title, [(label, url_name, staff_only, description)])
MORE_GROUPS: list[tuple[str, list[tuple[str, str, bool, str]]]] = [
    ("Health and history", [
        ("System health", "system_health", True,
         "Freshness, failures and the manual runs staff may start."),
        ("Live pipeline", "pipeline", True, "Where each job is right now."),
        ("Audit trail", "audit", True, "Every data run, with its outcome."),
        ("Data readiness", "data_readiness", False, "What arrived, and whether it is enough."),
    ]),
    ("Assistants", [
        ("AI operations", "ai_operations", True, "Automated daily analysis and the agent."),
        ("AI recommendations", "ai_recommendations", True, "Suggested next steps."),
        ("Draft messages", "messaging", True, "Drafts for CSPs. Nothing is ever sent from here."),
        ("Assistant monitoring", "agent_monitoring", True, "How the assistants are doing."),
    ]),
    ("Reference", [
        ("Incentive slabs", "balance_intelligence", False, "Rule #19 positions (production)."),
        ("API documentation", "api_docs", True, "For people connecting other systems."),
        ("Administration", "admin_embed", True, "Users, API keys and raw records."),
    ]),
]


def build(url_name: str | None, is_staff: bool) -> dict:
    section = SECTION_OF.get(url_name or "", "")
    groups: dict[str, list[dict]] = {}
    for key, label, target, icon, staff_only, group in PRIMARY:
        if staff_only and not is_staff:
            continue
        groups.setdefault(group, []).append({
            "key": key, "label": label, "href": reverse(f"dashboard:{target}"),
            "active": key == section, "icon": icon,
        })
    tabs = []
    for t in TABS.get(section, []):
        if t.staff_only and not is_staff:
            continue
        names = t.active_names or (t.url_name,)
        tabs.append({"label": t.label, "href": reverse(f"dashboard:{t.url_name}"),
                     "active": url_name in names})
    return {"nav_groups": list(groups.items()), "nav_section": section, "nav_tabs": tabs}


def more_groups(is_staff: bool) -> list[dict]:
    out = []
    for title, items in MORE_GROUPS:
        visible = [
            {"label": label, "href": reverse(f"dashboard:{name}"), "desc": desc}
            for label, name, staff_only, desc in items
            if is_staff or not staff_only
        ]
        if visible:
            out.append({"title": title, "items": visible})
    return out
