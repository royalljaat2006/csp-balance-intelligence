"""
Primary navigation: five items, with sibling pages shown as tabs inside a section.

Home | My CSPs | Money Movement | Needs Attention | More

Every existing route stays reachable and keeps its own permission check; this module only decides
which section a page belongs to and which sibling tabs to show. Staff-only pages are hidden from
non-staff here exactly as the old sidebar did (the views still enforce access themselves).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

#: url_name -> section key
SECTION_OF: dict[str, str] = {
    "home": "home",
    "csp_directory": "csps", "csp_detail": "csps", "csp_activity_json": "csps",
    "balance_intelligence": "csps",
    "money_movement": "money", "transactions": "money", "csp_comparisons": "money",
    "trends": "money", "bank_flow": "money", "data_progress": "money",
    "needs_attention": "attention", "risk": "attention", "actions": "attention",
    "approvals": "attention", "verification": "attention", "agent_findings": "attention",
    "more": "more", "overview_classic": "more", "messaging": "more", "ai_operations": "more",
    "ai_recommendations": "more", "agent_monitoring": "more", "system_health": "more",
    "pipeline": "more", "pipeline_events": "more", "pipeline_history": "more",
    "pipeline_node_detail": "more", "api_docs": "more", "audit": "more", "admin_embed": "more",
}


#: pages that show the Today-vs-yesterday strip. Today is the present IST day, yesterday the day
#: before; every page that shows data about the network or its CSPs belongs here (CSP detail has
#: its own per-CSP version; admin, audit, messaging and API pages show no such data).
COMPARE_PAGES = frozenset({
    "home", "money_movement", "transactions", "csp_comparisons", "trends", "bank_flow",
    "csp_directory", "balance_intelligence", "needs_attention", "overview_classic",
})


@dataclass(frozen=True)
class Tab:
    label: str
    url_name: str
    staff_only: bool = False
    active_names: tuple[str, ...] = ()  # extra url_names that light this tab up


#: section -> primary-nav entry
PRIMARY = [
    ("home", "Home", "home"),
    ("csps", "My CSPs", "csp_directory"),
    ("money", "Money Movement", "money_movement"),
    ("attention", "Needs Attention", "needs_attention"),
    ("more", "More", "more"),
]

TABS: dict[str, list[Tab]] = {
    "csps": [
        Tab("CSP Directory", "csp_directory"),
        Tab("CSP 360°", "csp_directory", active_names=("csp_detail",)),
        Tab("Balance Intelligence", "balance_intelligence"),
    ],
    "money": [
        Tab("Overview", "money_movement"),
        Tab("Data progress", "data_progress"),
        Tab("Transactions", "transactions"),
        Tab("Daily Comparison", "csp_comparisons"),
        Tab("Trends & Analytics", "trends"),
        Tab("Bank-Flow Validation", "bank_flow", staff_only=True),
    ],
    "attention": [
        Tab("All items", "needs_attention"),
        Tab("Risk", "risk", staff_only=True),
        Tab("Actions", "actions", staff_only=True),
        Tab("Approvals", "approvals", staff_only=True),
        Tab("Verification", "verification", staff_only=True),
        Tab("Agent Findings", "agent_findings", staff_only=True),
    ],
}

#: "More" hub: (group title, [(label, url_name, staff_only, one-line description)])
MORE_GROUPS: list[tuple[str, list[tuple[str, str, bool, str]]]] = [
    ("Reports and incentives", [
        ("Classic overview", "overview_classic", False,
         "The previous home page: incentive slabs, quick wins and top movers."),
        ("Balance Intelligence", "balance_intelligence", False,
         "Incentive slabs, who is close to the next slab, and gaps."),
        ("Trends & Analytics", "trends", False, "Network activity over time."),
    ]),
    ("Messages and AI tools", [
        ("Messaging", "messaging", True, "Draft messages to CSPs."),
        ("AI Operations", "ai_operations", True, "Automated daily analysis and its history."),
        ("AI Recommendations", "ai_recommendations", True, "Suggested next steps."),
        ("Agent Monitoring", "agent_monitoring", True, "How the automated assistants are doing."),
    ]),
    ("Checks on the data", [
        ("Data progress", "data_progress", False,
         "What ran today and yesterday, and when each figure was last updated."),
        ("Bank-Flow Validation", "bank_flow", True,
         "Technical checks on bank statements and the estimate."),
        ("Data Pipeline", "pipeline", True, "Where data comes from and when it last arrived."),
        ("System Health", "system_health", True, "Is everything running?"),
    ]),
    ("Administration", [
        ("Audit", "audit", True, "Who did what, and when."),
        ("API Documentation", "api_docs", True, "For developers connecting other systems."),
        ("Administration", "admin_embed", True, "Users, keys and raw records."),
    ]),
]


def build(url_name: str | None, is_staff: bool) -> dict:
    section = SECTION_OF.get(url_name or "", "")
    primary = [
        {"key": key, "label": label, "href": reverse(f"dashboard:{target}"),
         "active": key == section}
        for key, label, target in PRIMARY
    ]
    tabs = []
    for t in TABS.get(section, []):
        if t.staff_only and not is_staff:
            continue
        active = url_name in t.active_names if t.active_names else url_name == t.url_name
        tabs.append({"label": t.label, "href": reverse(f"dashboard:{t.url_name}"),
                     "active": active})
    return {"nav_primary": primary, "nav_section": section, "nav_tabs": tabs,
            "nav_back_to_more": section == "more" and url_name != "more"}


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
