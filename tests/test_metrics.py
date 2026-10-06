"""Pytest suite: the hand-verified fixture expectations as proper tests.

Runs fully in-process: builds the fixture repo with tests/make_fixture.sh,
ingests it as a zip through the Flask test client into a throwaway database,
then checks every metric against the hand-computed values (the same ones
tests/smoke_api.py verifies against a live server).

    pytest tests/test_metrics.py -q

Test order matters: the author-merge test at the bottom mutates state and
must run last.
"""
import io
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))

# point the app at a throwaway data dir before it is imported
os.environ["RAT_DATA_DIR"] = tempfile.mkdtemp(prefix="rat-pytest-")

import app as rat_app          # noqa: E402


def _make_fixture_zip():
    subprocess.run(["bash", str(TESTS / "make_fixture.sh")], check=True,
                   capture_output=True)
    repo = TESTS / "scratch" / "r"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in repo.rglob("*"):
            zf.write(path, path.relative_to(repo.parent))
    buf.seek(0)
    return buf


def _wait_ready(client, rid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/repos/{rid}/status").get_json()
        if status["status"] == "ready":
            return
        if status["status"] == "error":
            raise AssertionError(f"ingest failed: {status['message']}")
        time.sleep(0.2)
    raise AssertionError("ingest did not finish in time")


def _post(client, path, payload):
    resp = client.post(path, json=payload)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _metrics(client, rid, payload):
    return _post(client, f"/api/repos/{rid}/metrics", payload)


def _fixture_client():
    client = rat_app.app.test_client()
    resp = client.post("/api/repos", data={"file": (_make_fixture_zip(), "fixture.zip")},
                       content_type="multipart/form-data")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    rid = resp.get_json()["id"]
    _wait_ready(client, rid)
    return client, rid


# built once for the whole module
CLIENT, RID = _fixture_client()


def test_repo_metrics_no_filters():
    d = _metrics(CLIENT, RID, {})
    assert d["h_count"] == 10
    o = d["object"]
    assert (o["added"], o["removed"], o["churn"], o["modifications"]) == (11, 2, 13, 8)
    assert d["files"]["total"] == 8

    files = {f["path"]: f for f in d["files"]["rows"]}
    assert (files["a.txt"]["added"], files["a.txt"]["removed"],
            files["a.txt"]["modifications"]) == (4, 1, 2)
    assert (files["c.txt"]["added"], files["c.txt"]["removed"],
            files["c.txt"]["modifications"]) == (1, 1, 2)
    # edit after rename lands on the new path
    assert (files["d.txt"]["added"], files["d.txt"]["removed"]) == (1, 0)
    # binary file excluded from line metrics
    assert "bin.dat" not in files

    dirs = {x["path"]: x for x in d["dirs"]["dirs"]}
    assert (dirs["sub"]["added"], dirs["sub"]["removed"],
            dirs["sub"]["modifications"]) == (1, 0, 1)
    child_files = {f["path"]: f for f in d["dirs"]["files"]}
    assert (child_files["bin.dat"]["churn"],
            child_files["bin.dat"]["modifications"]) == (0, 0)

    auth = {a["name"]: a for a in d["authors"]}
    assert sorted(auth) == ["Alice Proper", "Bob"]
    assert (auth["Alice Proper"]["churn"],
            auth["Alice Proper"]["modifications"]) == (9, 4)
    assert (auth["Bob"]["churn"], auth["Bob"]["modifications"]) == (4, 4)
    assert round(sum(a["ownership"] for a in d["authors"]), 6) == 1.0

    assert sum(p["churn"] for p in d["timeline"]["points"]) == 13
    assert d["timeline"]["bucket"] == "day"


def test_file_object():
    f = _metrics(CLIENT, RID, {"object": "a.txt", "type": "file"})
    assert (f["object"]["added"], f["object"]["removed"],
            f["object"]["modifications"]) == (4, 1, 2)
    assert f["authors"][0]["name"] == "Alice Proper"
    assert f["authors"][0]["ownership"] == 1.0


def test_dir_object():
    s = _metrics(CLIENT, RID, {"object": "sub", "type": "dir"})
    assert (s["object"]["added"], s["object"]["modifications"]) == (1, 1)


def test_author_filter_and_mailmap():
    payload = CLIENT.get(f"/api/repos/{RID}/authors").get_json()
    assert payload["has_mailmap"] is True
    bob = next(a for a in payload["authors"] if a["name"] == "Bob")
    alice = next(a for a in payload["authors"] if a["name"] == "Alice Proper")
    assert alice["merged_from"] == ["Alice <alice@x.com>"]

    b = _metrics(CLIENT, RID, {"author": bob["id"]})
    assert (b["h_count"], b["object"]["churn"]) == (4, 4)


def test_time_filters():
    now = int(time.time())
    # every commit happened in the past -> the whole history is inside the window
    all_commits = _metrics(CLIENT, RID, {"since": now - 3600})
    assert (all_commits["h_count"], all_commits["object"]["churn"]) == (10, 13)
    # nothing after "now" -> empty set, and ownership of an empty set is 0
    empty = _metrics(CLIENT, RID, {"since": now + 3600})
    assert (empty["h_count"], empty["object"]["churn"],
            empty["authors"][0]["ownership"]) == (0, 0, 0.0)


def test_manual_commit_selection():
    page = CLIENT.get(f"/api/repos/{RID}/commits?q=edit").get_json()
    row = next(c for c in page["rows"] if c["summary"] == "edit+add")
    m = _metrics(CLIENT, RID, {"commits": str(row["id"])})
    assert (m["h_count"], m["object"]["added"], m["object"]["removed"]) == (1, 3, 1)


def test_tree_picker():
    tree = CLIENT.get(f"/api/repos/{RID}/tree").get_json()
    assert tree["dirs"] == ["sub"]
    sub_tree = CLIENT.get(f"/api/repos/{RID}/tree?path=sub").get_json()
    assert sub_tree == {"dirs": [], "files": ["sub/b.txt"]}


def test_author_merge_mutates_state_keep_last():
    """Destructive: must run after every read-only test above."""
    payload = CLIENT.get(f"/api/repos/{RID}/authors").get_json()
    bob = next(a for a in payload["authors"] if a["name"] == "Bob")
    alice = next(a for a in payload["authors"] if a["name"] == "Alice Proper")

    res = _post(CLIENT, f"/api/repos/{RID}/authors/merge",
                {"target": alice["id"], "sources": [bob["id"]]})
    assert res == {"ok": True}

    after = CLIENT.get(f"/api/repos/{RID}/authors").get_json()["authors"]
    assert [(a["name"], a["commits"]) for a in after] == [("Alice Proper", 10)]
    assert sorted(after[0]["merged_from"]) == ["Alice <alice@x.com>", "Bob <bob@x.com>"]

    d2 = _metrics(CLIENT, RID, {})
    assert d2["object"]["churn"] == 13
    assert d2["authors"][0]["ownership"] == 1.0
