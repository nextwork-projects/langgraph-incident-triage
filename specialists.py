# Import the CLI, fixture, and agent helpers used throughout this file.
import argparse
import json
import os
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

# Keep every specialist on one model and one fixture directory.
MODEL_ID = "gemini-3.7-flash"
DATA_DIR = Path(__file__).parent / "data"
LEVELS = {"INFO": 0, "WARN": 1, "ERROR": 2}


def load_json(name: str):
    # Load one fixture file relative to specialists.py.
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def build_model():
    # Read the active project selected in Cloud Shell.
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set.")

    # Use Vertex AI with bounded retries and request time.
    return ChatGoogleGenerativeAI(
        model=MODEL_ID,
        project=project_id,
        location="global",
        vertexai=True,
        max_retries=2,
        timeout=180,
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
