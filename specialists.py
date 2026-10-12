import json
import os
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

MODEL_ID = "gemini-3.7-flash"
DATA_DIR = Path(__file__).parent / "data"
LEVELS = {"INFO": 0, "WARN": 1, "ERROR": 2}


def load_json(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def build_model():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set.")

    return ChatGoogleGenerativeAI(
        model=MODEL_ID,
        project=project_id,
        location="global",
        vertexai=True,
        max_retries=2,
        timeout=60,
    )


@tool
def search_logs(service: str, min_level: str = "WARN") -> str:
    """Return log lines for one service at or above a level: INFO, WARN, or ERROR."""
    threshold = LEVELS.get(min_level.upper(), 1)
    lines = []
    for raw in (DATA_DIR / "logs.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(raw)
        if entry["service"] == service and LEVELS[entry["level"]] >= threshold:
            lines.append(f"{entry['ts']} {entry['level']} {entry['message']}")
    return "\n".join(lines[-25:]) or f"No {min_level} logs for {service}."


@tool
def recent_deploys(service: str = "") -> str:
    """List recent deploys, newest first. Pass a service name, or leave it empty for every service."""
    deploys = sorted(
        load_json("deploys.json"), key=lambda d: d["deployed_at"], reverse=True
    )
    if service:
        deploys = [d for d in deploys if d["service"] == service]
    return "\n".join(
        f"{d['deployed_at']} {d['id']} {d['service']} ({d['team']}): {d['change']}"
        for d in deploys
    ) or f"No deploys for {service}."


@tool
def metric_window(service: str) -> str:
    """Return every metric time series for one service."""
    if os.environ.get("TRIAGE_FAIL") == "metrics":
        raise RuntimeError("Metrics backend timed out.")
    series = load_json("metrics.json").get(service)
    if not series:
        return f"No metrics for {service}."
    return "\n".join(
        f"{name}: " + ", ".join(f"{p['ts'][11:16]}={p['value']}" for p in points)
        for name, points in series.items()
    )


SPECIALISTS = {
    "logs": {
        "tools": [search_logs],
        "prompt": "You are the logs investigator on an incident response team. "
        "Search the logs of the alerting service and its dependencies for "
        "errors that explain the alert, and note when they started.",
    },
    "deploys": {
        "tools": [recent_deploys],
        "prompt": "You are the change investigator on an incident response team. "
        "Find deploys to the alerting service or its dependencies shortly "
        "before the alert, and judge which change could cause it.",
    },
    "metrics": {
        "tools": [metric_window],
        "prompt": "You are the metrics investigator on an incident response team. "
        "Read the metrics of the alerting service and its dependencies and "
        "describe what changed, when, and by how much.",
    },
}

FINDING_SCHEMA = {
    "title": "Finding",
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "suspected_cause": {"type": "string"},
        "severity": {"type": "string", "enum": ["SEV1", "SEV2", "SEV3", "SEV4"]},
    },
    "required": ["summary", "evidence", "suspected_cause", "severity"],
}


def build_specialist(name: str):
    spec = SPECIALISTS[name]
    return create_agent(
        build_model(),
        tools=spec["tools"],
        system_prompt=spec["prompt"]
        + " Use your tools before answering. Quote exact evidence. "
        "Rate severity from SEV1 (worst) to SEV4 from what you found.",
        response_format=FINDING_SCHEMA,
        name=f"{name}_specialist",
    )


def run_specialist(name: str, brief: str) -> dict:
    result = build_specialist(name).invoke({"messages": [("human", brief)]})
    return result["structured_response"]
