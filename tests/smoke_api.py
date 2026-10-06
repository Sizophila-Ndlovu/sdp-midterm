"""Smoke test: hand-verified expectations against the server with fixture repo 1.

Usage: generate the fixture with tests/make_fixture.sh, ingest the resulting
repo as the FIRST repository (id 1) into a fresh database, start the server and
run this script. It mutates state (merges authors at the end), so run it once
against a fresh ingest.
"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5000/api"
ok = 0


def call(path, data=None, method=None):
    url = BASE + path
    if data is not None:
        req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
    else:
        req = urllib.request.Request(url, method=method or "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HTTP {e.code} on {path}: {e.read().decode()}")


def check(label, got, want):
    global ok
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    ok += 1
    print(f"ok {label}: {got!r}")


# 1. whole repo, no filters
d = call("/repos/1/metrics", {})
check("h_count", d["h_count"], 10)
o = d["object"]
check("root added/removed/churn/n",
      (o["added"], o["removed"], o["churn"], o["modifications"]), (11, 2, 13, 8))
check("files total", d["files"]["total"], 8)

files = {f["path"]: f for f in d["files"]["rows"]}
check("a.txt", (files["a.txt"]["added"], files["a.txt"]["removed"],
                files["a.txt"]["modifications"]), (4, 1, 2))
check("c.txt", (files["c.txt"]["added"], files["c.txt"]["removed"],
                files["c.txt"]["modifications"]), (1, 1, 2))
check("d.txt (edit after rename lands on new path)",
      (files["d.txt"]["added"], files["d.txt"]["removed"]), (1, 0))
check("binary excluded", "bin.dat" in files, False)

dirs = {x["path"]: x for x in d["dirs"]["dirs"]}
check("sub dir", (dirs["sub"]["added"], dirs["sub"]["removed"], dirs["sub"]["modifications"]),
      (1, 0, 1))
child_files = {f["path"]: f for f in d["dirs"]["files"]}
check("bin.dat listed with zero metrics",
      (child_files["bin.dat"]["churn"], child_files["bin.dat"]["modifications"]), (0, 0))

auth = {a["name"]: a for a in d["authors"]}
check("authors", sorted(auth), ["Alice Proper", "Bob"])
check("alice churn/n",
      (auth["Alice Proper"]["churn"], auth["Alice Proper"]["modifications"]), (9, 4))
check("bob churn/n", (auth["Bob"]["churn"], auth["Bob"]["modifications"]), (4, 4))
check("ownership sums to 1", round(sum(a["ownership"] for a in d["authors"]), 6), 1.0)
check("timeline churn", sum(p["churn"] for p in d["timeline"]["points"]), 13)
check("timeline bucket", d["timeline"]["bucket"], "day")

# 2. object: file a.txt
f = call("/repos/1/metrics", {"object": "a.txt", "type": "file"})
check("a.txt object", (f["object"]["added"], f["object"]["removed"],
                       f["object"]["modifications"]), (4, 1, 2))
check("a.txt owner", f["authors"][0]["name"], "Alice Proper")
check("a.txt ownership", f["authors"][0]["ownership"], 1.0)

# 3. object: dir sub
s = call("/repos/1/metrics", {"object": "sub", "type": "dir"})
check("sub object", (s["object"]["added"], s["object"]["modifications"]), (1, 1))

# 4. author filter + mailmap alias bookkeeping
authors_payload = call("/repos/1/authors")
check("has_mailmap", authors_payload["has_mailmap"], True)
bob = next(a for a in authors_payload["authors"] if a["name"] == "Bob")
alice = next(a for a in authors_payload["authors"] if a["name"] == "Alice Proper")
check("mailmap merged raw alias", alice["merged_from"], ["Alice <alice@x.com>"])
b = call("/repos/1/metrics", {"author": bob["id"]})
check("bob filter h/churn", (b["h_count"], b["object"]["churn"]), (4, 4))

# 5. time filter after every commit -> empty set
t = call("/repos/1/metrics", {"since": 1791292209 + 1})
check("empty filter", (t["h_count"], t["object"]["churn"], t["authors"][0]["ownership"]),
      (0, 0, 0.0))

# 6. manual commit selection: the "edit+add" commit -> 3 added, 1 removed
page = call("/repos/1/commits?q=edit")
row = next(c for c in page["rows"] if c["summary"] == "edit+add")
m = call("/repos/1/metrics", {"commits": str(row["id"])})
check("single commit", (m["h_count"], m["object"]["added"], m["object"]["removed"]),
      (1, 3, 1))

# 7. tree picker
tree = call("/repos/1/tree")
check("tree root dirs", tree["dirs"], ["sub"])
sub_tree = call("/repos/1/tree?path=sub")
check("tree sub", sub_tree, {"dirs": [], "files": ["sub/b.txt"]})

# 8. merge Bob into Alice -> one author, ownership 1
res = call("/repos/1/authors/merge", {"target": alice["id"], "sources": [bob["id"]]})
check("merge ok", res, {"ok": True})
after = call("/repos/1/authors")["authors"]
check("one author after merge", [(a["name"], a["commits"]) for a in after],
      [("Alice Proper", 10)])
check("merged aliases", sorted(after[0]["merged_from"]),
      ["Alice <alice@x.com>", "Bob <bob@x.com>"])
d2 = call("/repos/1/metrics", {})
check("post-merge churn unchanged", d2["object"]["churn"], 13)
check("post-merge single owner", d2["authors"][0]["ownership"], 1.0)

print(f"\nALL {ok} CHECKS PASSED")
