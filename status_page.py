"""Read-only incident status page, served for Cloud Shell Web Preview."""
import html
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# read_incident() uses these to load a saved thread.
from langgraph.checkpoint.sqlite import SqliteSaver

from app import DB_PATH, RUNBOOK_DIR, build_graph, safe_incident_id

PORT = 8080
REFRESH_SECONDS = 5


def read_incident(thread_id: str) -> dict | None:
    """Return one saved incident for the page, or None if the thread doesn't exist.

    The page reads these keys: alert, status, severity, analysis, findings,
    approvals, waiting, next, and runbook.
    """
    # Load the latest checkpoint without running or resuming the graph.
    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(DB_PATH) as checkpointer:
        snapshot = build_graph(checkpointer).get_state(config)
    values = snapshot.values
    if not values:
        return None

    # Show the runbook once the graph has published it.
    runbook = RUNBOOK_DIR / f"{safe_incident_id(thread_id)}.md"
    return {
        "alert": values.get("alert", {}),
        "status": values.get("status", "unknown"),
        "severity": values.get("severity", "unknown"),
        "analysis": values.get("analysis", {}),
        "findings": values.get("findings", []),
        "approvals": values.get("approvals", []),
        "waiting": [pending.value for pending in snapshot.interrupts],
        "next": list(snapshot.next),
        "runbook": runbook.read_text(encoding="utf-8") if runbook.exists() else None,
    }


def list_threads() -> list[str]:
    # List saved threads, newest first, with a plain SELECT on the checkpoint table.
    if not Path(DB_PATH).exists():
        return []
    with closing(sqlite3.connect(DB_PATH)) as conn:
        try:
            rows = conn.execute(
                "SELECT thread_id, MAX(checkpoint_id) AS latest FROM checkpoints "
                "WHERE checkpoint_ns = '' GROUP BY thread_id ORDER BY latest DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            # The first run hasn't created the checkpoint table yet.
            return []
    return [row[0] for row in rows]


STYLE = """
:root { color-scheme: light dark; --bg: #f6f7f9; --panel: #fff; --text: #1d2330;
  --muted: #5c6575; --line: #dde1e7; --accent: #2f6fde; --warn: #b7791f;
  --bad: #c53030; --good: #2f855a; }
@media (prefers-color-scheme: dark) { :root { --bg: #12151b; --panel: #1b2029;
  --text: #e6e9ef; --muted: #9aa3b2; --line: #2c3340; --accent: #6ea0ff;
  --warn: #e0a84a; --bad: #f07171; --good: #5fc28a; } }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, sans-serif; background: var(--bg); color: var(--text); }
header { padding: 16px 24px; border-bottom: 1px solid var(--line); background: var(--panel);
  display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px; }
header h1 { margin: 0; font-size: 20px; }
header span { color: var(--muted); font-size: 13px; }
.layout { display: grid; grid-template-columns: 220px 1fr; gap: 24px; padding: 24px; }
@media (max-width: 720px) { .layout { grid-template-columns: 1fr; padding: 16px; } }
nav a { display: block; padding: 8px 12px; border-radius: 6px; color: var(--text); text-decoration: none; }
nav a.current { background: var(--panel); border: 1px solid var(--line); font-weight: 600; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 16px 20px; margin-bottom: 16px; }
h2 { margin: 0 0 4px; font-size: 22px; }
h3 { margin: 0 0 8px; font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 13px;
  font-weight: 600; border: 1px solid currentColor; margin-right: 6px; }
.sev { color: var(--bad); } .status { color: var(--accent); }
.approve { color: var(--good); } .reject { color: var(--bad); } .waiting { color: var(--warn); }
table { width: 100%; border-collapse: collapse; }
td, th { text-align: left; padding: 6px 8px; border-top: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 13px; }
td:not(:last-child), th:not(:last-child) { width: 1%; white-space: nowrap; padding-right: 24px; }
pre { white-space: pre-wrap; margin: 0; font-size: 13px; }
.muted { color: var(--muted); }
"""


def esc(value) -> str:
    return html.escape(str(value))


def render_incident(incident: dict) -> str:
    # Turn the dictionary from read_incident() into the page's panels.
    alert = incident.get("alert") or {}
    analysis = incident.get("analysis") or {}
    parts = [
        "<section>",
        f"<h2>{esc(alert.get('title', 'Incident'))}</h2>",
        f"<p class='muted'>{esc(alert.get('service', ''))} {esc(alert.get('fired_at', ''))}</p>",
        f"<span class='pill status'>{esc(incident.get('status'))}</span>",
        f"<span class='pill sev'>{esc(incident.get('severity'))}</span>",
        "</section>",
    ]
    if analysis:
        parts += [
            "<section><h3>Diagnosis</h3>",
            f"<p>{esc(analysis.get('summary', ''))}</p>",
            f"<p class='muted'>Likely cause: {esc(analysis.get('likely_cause', ''))}</p></section>",
        ]

    rows = "".join(
        f"<tr><td>{esc(f.get('specialist'))}</td><td>{esc(f.get('severity'))}</td>"
        f"<td>{esc(f.get('summary'))}</td></tr>"
        for f in incident.get("findings", [])
    )
    parts.append(
        "<section><h3>Findings</h3>"
        + (f"<table><tr><th>Specialist</th><th>Severity</th><th>Summary</th></tr>{rows}</table>"
           if rows else "<p class='muted'>No findings yet.</p>")
        + "</section>"
    )

    decided = "".join(
        f"<tr><td>{esc(a.get('team'))}</td><td>{esc(a.get('service'))}</td>"
        f"<td class='{esc(a.get('decision'))}'>{esc(a.get('decision'))}</td></tr>"
        for a in incident.get("approvals", [])
    )
    waiting = "".join(
        f"<tr><td>{esc(w.get('team'))}</td><td>{esc(w.get('service'))}</td>"
        f"<td class='waiting'>waiting: {esc(w.get('action'))}</td></tr>"
        for w in incident.get("waiting", [])
    )
    parts.append(
        "<section><h3>Team approvals</h3>"
        + (f"<table><tr><th>Team</th><th>Service</th><th>Decision</th></tr>{decided}{waiting}</table>"
           if decided or waiting else "<p class='muted'>No approvals requested yet.</p>")
        + f"<p class='muted'>Next: {esc(', '.join(incident.get('next', [])) or 'finished')}</p>"
        + "</section>"
    )

    runbook = incident.get("runbook")
    parts.append(
        "<section><h3>Runbook</h3>"
        + (f"<pre>{esc(runbook)}</pre>" if runbook else "<p class='muted'>Not published yet.</p>")
        + "</section>"
    )
    return "".join(parts)


def render_page(threads: list[str], selected: str) -> str:
    links = "".join(
        f"<a href='?thread={quote(t)}' class='{'current' if t == selected else ''}'>{esc(t)}</a>"
        for t in threads
    ) or "<p class='muted'>No saved incidents yet.</p>"

    if not selected:
        body = "<section><p>Start an incident with <code>app.py start</code>, then refresh.</p></section>"
    else:
        try:
            incident = read_incident(selected)
            body = (render_incident(incident) if incident
                    else f"<section><p>No saved thread named {esc(selected)}.</p></section>")
        except NotImplementedError as error:
            body = f"<section><p>{esc(error)}</p></section>"

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{REFRESH_SECONDS}'>"
        f"<title>Incident Status</title><style>{STYLE}</style></head><body>"
        "<header><h1>Incident Status</h1>"
        f"<span>Read-only. Refreshes every {REFRESH_SECONDS} seconds.</span></header>"
        f"<div class='layout'><nav>{links}</nav><main>{body}</main></div>"
        "</body></html>"
    )


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        threads = list_threads()
        selected = parse_qs(url.query).get("thread", [threads[0] if threads else ""])[0]
        body = render_page(threads, selected).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Keep the terminal quiet while the page refreshes.
        pass


def main():
    server = ThreadingHTTPServer(("", PORT), StatusHandler)
    print(f"Status page running on port {PORT}. Open Web Preview > Preview on port {PORT}.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStatus page stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
