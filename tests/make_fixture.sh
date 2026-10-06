#!/usr/bin/env bash
# Scratch fixture repo used to pin down the exact `git log --numstat` output format.
set -e
cd "$(dirname "$0")"
rm -rf scratch && mkdir -p scratch && cd scratch

git init -q -b main r
cd r
git config user.name "Alice"
git config user.email "alice@x.com"

printf 'one\ntwo\n' > a.txt
mkdir -p sub && printf 'x\n' > sub/b.txt
git add -A && git commit -qm "initial"

printf 'one\nTWO\nthree\n' > a.txt
printf 'new\n' > c.txt
git add -A && git commit -qm "edit+add"

git mv a.txt sub/a2.txt
git commit -qm "pure rename"

printf 'one\nTWO\nthree\nfour\n' > sub/a2.txt
git mv sub/a2.txt d.txt
git add -A && git commit -qm "edit+rename"

printf '\000\001\002' > bin.dat
git add bin.dat && git commit -qm "binary"

git rm -q c.txt && git commit -qm "delete"

git config user.name "Bob"
git config user.email "bob@x.com"
printf 'b1\n' > b.txt && git add b.txt && git commit -qm "bob commit"

printf 'Alice Proper <alice@proper.com> Alice <alice@x.com>\n' > .mailmap
git add .mailmap && git commit -qm "add mailmap"

git checkout -q -b feat && printf 'f1\n' > f.txt && git add f.txt && git commit -qm "feat work"
git checkout -q main && printf 'm1\n' > m.txt && git add m.txt && git commit -qm "main work"
git merge -q --no-edit feat

echo "=== NUMSTAT FORMAT ==="
git -c core.quotePath=false log --no-merges --numstat -M50% \
  --format="HDR|%H|%P|%an|%ae|%aN|%aE|%ct|%s" | head -85 | cat -A
echo "=== MAILMAP: no flag ==="
git log --format="%h|%an|%ae|%aN|%aE" | head -12
echo "=== MAILMAP: --use-mailmap ==="
git log --use-mailmap --format="%h|%an|%ae|%aN|%aE" | head -12
