import argparse
import json
import os
import operator
from pathlib import Path
from typing import Annotated, Literal, TypedDict

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from specialists import SPECIALISTS, build_model, load_json, run_specialist

DB_PATH = "triage_checkpoints.db"
SEVERITY_ORDER = ["SEV4", "SEV3", "SEV2", "SEV1"]
RUNBOOK_DIR = Path("runbooks")
RESTRICTED_TERMS = (
    "delete",
    "disable",
    "drop",
    "production",
    "restart",
    "rotate",
)

ANALYSIS_SCHEMA = {
    "title": "IncidentAnalysis",
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["SEV1", "SEV2", "SEV3", "SEV4"],
        },
        "summary": {"type": "string"},
        "proposed_action": {"type": "string"},
        "risk": {
            "type": "string",
            "enum": ["low", "high"],
        },
        "rationale": {"type": "string"},
    },
    "required": [
        "severity",
        "summary",
        "proposed_action",
        "risk",
        "rationale",
    ],
}

ROUTER_SCHEMA = {
    "title": "TriageDecision",
    "type": "object",
    "properties": {
        "severity": {
            "type": "string",
            "enum": ["SEV1", "SEV2", "SEV3", "SEV4"],
        },
        "specialists": {
            "type": "array",
            "items": {"type": "string", "enum": list(SPECIALISTS)},
        },
        "reason": {"type": "string"},
    },
    "required": ["severity", "specialists", "reason"],
}


def highest_severity(current: str, new: str) -> str:
    if current not in SEVERITY_ORDER:
        return new
    return max(current, new, key=SEVERITY_ORDER.index)


class IncidentState(TypedDict, total=False):
    incident_id: str
    incident: str
    alert: dict
    severity: Annotated[str, highest_severity]
    specialists: list[str]
    findings: Annotated[list[dict], operator.add]
    analysis: dict[str, str]
    requires_approval: bool
    policy_reason: str
    decision: bool
    status: str
    audit_log: list[str]
    runbook_path: str


class SpecialistTask(TypedDict):
    specialist: str
    brief: str


def build_analyst():
    return build_model().with_structured_output(
        schema=ANALYSIS_SCHEMA,
        method="json_schema",
    )


def build_router():
    return build_model().with_structured_output(
        schema=ROUTER_SCHEMA,
        method="json_schema",
    )


def safe_incident_id(incident_id: str) -> str:
    safe_id = "".join(
        character
        for character in incident_id
        if character.isalnum() or character in {"-", "_"}
    )
    if not any(character.isalnum() for character in safe_id):
        raise ValueError("Incident ID must contain a letter or number.")
    return safe_id


def incident_brief(state: IncidentState) -> str:
    alert = state["alert"]
    services = load_json("services.json")
    service = alert["service"]
    related = [service] + services[service]["depends_on"]
    return (
        f"Alert {alert['id']} fired at {alert['fired_at']} on {service}.\n"
        f"{alert['title']}: {alert['description']}\n"
        f"Services to check: {', '.join(related)}."
    )


def route_incident(state: IncidentState):
    decision = build_router().invoke(
        [
            (
                "system",
                "You are the incident commander. Set a first severity and choose "
                "which specialists investigate: logs, deploys, metrics. For SEV1 "
                "or SEV2, send every specialist. For SEV3 or SEV4, send only the "
                "one or two most likely to explain the symptom.",
            ),
            ("human", incident_brief(state)),
        ]
    )
    print(f"Router: {decision['severity']}, sending {', '.join(decision['specialists'])}")
    return {
        "severity": decision["severity"],
        "specialists": decision["specialists"],
        "status": "routed",
        "audit_log": state["audit_log"]
        + [f"Router chose {decision['specialists']}: {decision['reason']}"],
    }


def dispatch_specialists(state: IncidentState):
    brief = incident_brief(state)
    return [
        Send("investigate", {"specialist": name, "brief": brief})
        for name in state["specialists"]
    ]


def investigate(task: SpecialistTask):
    name = task["specialist"]
    print(f"  {name} specialist investigating...")
    finding = run_specialist(name, task["brief"])
    print(f"  {name} specialist done: {finding['severity']}, {finding['suspected_cause']}")
    return {
        "findings": [{"specialist": name, **finding}],
        "severity": finding["severity"],
    }


def analyze_incident(state: IncidentState):
    analyst = build_analyst()
    findings = json.dumps(state["findings"], indent=2)
    analysis = analyst.invoke(
        [
            (
                "system",
                "You are an SRE incident analyst. Combine the specialists' "
                "findings into one diagnosis, name the most likely cause with its "
                "evidence, and propose one reversible remediation step. Keep "
                "every field concise.",
            ),
            ("human", f"{state['incident']}\n\nFindings:\n{findings}"),
        ]
    )
    return {
        "analysis": analysis,
        "severity": analysis["severity"],
        "status": "analyzed",
        "audit_log": state["audit_log"]
        + [f"Model combined {len(state['findings'])} findings into one analysis."],
    }


def policy_check(state: IncidentState):
    analysis = state["analysis"]
    searchable_text = (
        f"{state['incident']} {analysis['proposed_action']}"
    ).lower()
    severity_requires_review = state["severity"] in {"SEV1", "SEV2"}
    term_requires_review = any(
        term in searchable_text for term in RESTRICTED_TERMS
    )
    requires_approval = severity_requires_review or term_requires_review
    reason = (
        "High severity or restricted operational language detected."
        if requires_approval
        else "No high-risk policy rule matched."
    )
    return {
        "requires_approval": requires_approval,
        "policy_reason": reason,
        "status": "policy_checked",
        "audit_log": state["audit_log"] + [f"Policy check: {reason}"],
    }


def policy_route(state: IncidentState) -> Literal["approval_gate", "publish_runbook"]:
    return "approval_gate" if state["requires_approval"] else "publish_runbook"


def approval_gate(state: IncidentState):
    decision = interrupt(
        {
            "question": "Approve this remediation proposal?",
            "incident_id": state["incident_id"],
            "incident": state["incident"],
            "analysis": state["analysis"],
            "policy_reason": state["policy_reason"],
            "allowed_decisions": ["approve", "reject"],
        }
    )
    if decision not in ("approve", "reject"):
        raise ValueError("Decision must be 'approve' or 'reject'.")
    approved = decision == "approve"
    return {
        "decision": approved,
        "status": "approved" if approved else "rejected",
        "audit_log": state["audit_log"]
        + ["Human approved the proposal." if approved else "Human rejected the proposal."],
    }


def approval_route(state: IncidentState) -> Literal["publish_runbook", "reject_runbook"]:
    return "publish_runbook" if state["decision"] else "reject_runbook"


def publish_runbook(state: IncidentState):
    RUNBOOK_DIR.mkdir(exist_ok=True)
    path = RUNBOOK_DIR / f"{safe_incident_id(state['incident_id'])}.md"
    analysis = state["analysis"]
    approval_outcome = "Approved" if state.get("decision") else "Not required by policy"
    audit_log = state["audit_log"] + [f"Published runbook to {path}."]
    audit_lines = "\n".join(f"- {entry}" for entry in audit_log)

    path.write_text(
        "# Policy-Controlled Incident Runbook\n\n"
        f"Incident ID: {state['incident_id']}\n\n"
        f"Incident: {state['incident']}\n\n"
        f"Severity: {state['severity']}\n\n"
        f"Summary: {analysis['summary']}\n\n"
        f"Proposed action: {analysis['proposed_action']}\n\n"
        f"Rationale: {analysis['rationale']}\n\n"
        f"Model risk: {analysis['risk']}\n\n"
        f"Policy result: {state['policy_reason']}\n\n"
        f"Approval outcome: {approval_outcome}\n\n"
        "Audit log:\n"
        f"{audit_lines}\n",
        encoding="utf-8",
    )
    return {
        "status": "published",
        "runbook_path": str(path),
        "audit_log": audit_log,
    }


def reject_runbook(state: IncidentState):
    return {
        "status": "rejected_without_publish",
        "audit_log": state["audit_log"] + ["Publishing was skipped."],
    }


def build_graph(checkpointer: SqliteSaver):
    builder = StateGraph(IncidentState)
    builder.add_node("route_incident", route_incident)
    builder.add_node("investigate", investigate)
    builder.add_node("analyze_incident", analyze_incident, defer=True)
    builder.add_node("policy_check", policy_check)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("publish_runbook", publish_runbook)
    builder.add_node("reject_runbook", reject_runbook)

    builder.add_edge(START, "route_incident")
    builder.add_conditional_edges(
        "route_incident", dispatch_specialists, ["investigate"]
    )
    builder.add_edge("investigate", "analyze_incident")
    builder.add_edge("analyze_incident", "policy_check")
    builder.add_conditional_edges("policy_check", policy_route)
    builder.add_conditional_edges("approval_gate", approval_route)
    builder.add_edge("publish_runbook", END)
    builder.add_edge("reject_runbook", END)

    return builder.compile(checkpointer=checkpointer)


def display_stream(stream):
    final_state = stream.output
    if stream.interrupted:
        print("WORKFLOW PAUSED")
        print(json.dumps(stream.interrupts[0].value, indent=2))
        return

    print("WORKFLOW COMPLETE")
    print(json.dumps(final_state, indent=2))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a resumable incident-triage workflow."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--thread", required=True)
    start_parser.add_argument("--alert", required=True)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--thread", required=True)
    resume_parser.add_argument(
        "--decision",
        choices=["approve", "reject"],
        required=True,
    )

    return parser.parse_args()


def main():
    args = parse_args()
    config = {"configurable": {"thread_id": args.thread}}

    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        graph = build_graph(checkpointer)

        if args.command == "start":
            alerts = {alert["id"]: alert for alert in load_json("alerts.json")}
            alert = alerts[args.alert]
            graph_input = {
                "incident_id": args.thread,
                "incident": f"{alert['title']}: {alert['description']}",
                "alert": alert,
                "status": "new",
                "audit_log": [],
            }
        else:
            graph_input = Command(resume=args.decision)

        stream = graph.stream_events(
            graph_input,
            config=config,
            durability="sync",
            version="v3",
        )
        display_stream(stream)


if __name__ == "__main__":
    main()

