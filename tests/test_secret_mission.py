"""The status page reads saved threads without resuming them, and serves them over HTTP."""
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import status_page
from test_step4 import run


def test_status_page_mid_approval(tmp_path, monkeypatch):
    run(tmp_path, "start", "--thread", "inc-page", "--alert", "INC-2041")
    run(tmp_path, "resume", "--thread", "inc-page", "--team", "payments", "--decision", "approve")
    monkeypatch.chdir(tmp_path)
    db_before = (tmp_path / "triage_checkpoints.db").read_bytes()

    incident = status_page.read_incident("inc-page")
    assert incident["severity"] == "SEV2"
    assert incident["alert"]["id"] == "INC-2041"
    assert [(a["team"], a["decision"]) for a in incident["approvals"]] == [("payments", "approve")]
    assert [(w["team"], w["action"]) for w in incident["waiting"]] == [
        ("platform-data", "Add a low-connection alert.")
    ]
    assert incident["next"] == ["approval_gate"]
    assert incident["runbook"] is None
    assert status_page.read_incident("no-such-thread") is None
    assert (tmp_path / "triage_checkpoints.db").read_bytes() == db_before


def test_status_page_serves_published_runbook(tmp_path, monkeypatch):
    run(tmp_path, "start", "--thread", "inc-done", "--alert", "INC-2041")
    run(tmp_path, "resume", "--thread", "inc-done", "--team", "payments", "--decision", "approve")
    run(tmp_path, "resume", "--thread", "inc-done", "--team", "platform-data", "--decision", "reject")
    run(tmp_path, "start", "--thread", "inc-new", "--alert", "INC-2041")
    monkeypatch.chdir(tmp_path)

    assert status_page.list_threads() == ["inc-new", "inc-done"]

    server = ThreadingHTTPServer(("127.0.0.1", 0), status_page.StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/?thread=inc-done"
        page = urllib.request.urlopen(url).read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()

    assert "<meta http-equiv='refresh' content='5'>" in page
    assert "inc-new" in page and "inc-done" in page
    assert "approve" in page and "reject" in page
    assert "Not published yet." not in page
    assert "Roll back d-1042." in page


def test_status_page_before_first_checkpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert status_page.list_threads() == []
    (tmp_path / "triage_checkpoints.db").touch()  # created, but no tables yet
    assert status_page.list_threads() == []
