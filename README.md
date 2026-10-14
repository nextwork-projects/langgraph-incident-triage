# LangGraph Incident Triage: Starter Repo

Starting code for the NextWork Project **Build a Multi-Agent Incident Triage System with LangGraph**.

`app.py` is the finished single-analyst incident workflow from the NextWork Project [Build a Resumable LangGraph Workflow](https://nextwork.ai/projects/f94db9b2-1157-404b-a559-83e8752f2f86). It is included unchanged. One Gemini call analyzes an incident, a deterministic policy decides whether a human must approve, `interrupt()` pauses the run, and `SqliteSaver` lets a new process resume it. You don't need to have done that Project first: this repo gives you the same starting point either way.

In this Project you turn that single analyst into a team: a router, specialist agents that investigate in parallel, recovery when one of them crashes, and an approval gate for each team that owns a fix. The setup code for the specialists and the status page is already here, so you spend your time on the agents and the graph.

## What's in the repo

| Path | What it is |
|---|---|
| `app.py` | The starting workflow: `start` and `resume` commands, one analyst, one approval gate |
| `specialists.py` | Setup code for the specialist agents: the Gemini model connection, a fixture loader, and one tool each for logs, deploys, and metrics. You add the agents themselves in the Project |
| `status_page.py` | The web page for the Secret Mission: HTML, styling, and a small server for Cloud Shell Web Preview. You write the part that reads an incident's saved state |
| `requirements.txt` | Pinned packages. `langchain` is new in this Project (for `create_agent`) |
| `data/alerts.json` | Two alerts to triage: `INC-2041` (checkout errors) and `INC-2042` (search latency) |
| `data/logs.jsonl` | Application logs from the services involved, one JSON object per line |
| `data/deploys.json` | Recent deploys, with the service, owning team, time, and change |
| `data/metrics.json` | Metric time series per service |
| `data/services.json` | Service catalog: owning team, on-call handle, and dependencies |

The data is made up and small on purpose. Your agents read it through tools, so runs are cheap and the answers can be checked.

## Set up (Google Cloud Shell)

You need a Google Cloud project with billing enabled.

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com
git clone https://github.com/nextwork-projects/langgraph-incident-triage.git ~/langgraph-incident-triage-agents
cd ~/langgraph-incident-triage-agents
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If the first model call returns `403 PERMISSION_DENIED`, the API was enabled only moments ago. Wait a few minutes and try again.

## Check that it runs

```bash
python3 app.py start --thread smoke-001 --incident "Checkout API returns HTTP 500 for 1 in 6 requests after a config deploy."
```

You should see `WORKFLOW PAUSED` and an approval payload. The `LangChainBetaWarning` about the v3 streaming protocol is expected.

## Cost

Each run makes a few Gemini 3.7 Flash calls, which costs a few cents at most. Cloud Shell is free.

## Branches

- `main` is the starting point for the Project. Clone it and follow the Project's steps from here.
- `solution` is the finished build, with offline tests (`python3 -m pip install pytest && python3 -m pytest`). Use it to check your work if you get stuck.
