#!/usr/bin/env bash
# Commit the agent's memory (journal, decisions, lessons, state) back to the repo.
git config user.name "trading-agent"
git config user.email "actions@users.noreply.github.com"
git add data/
git diff --cached --quiet && exit 0
git commit -q -m "agent: cycle $(date -u +%F' '%H:%M)"
for i in 1 2 3; do git pull --rebase -q && git push -q && break; sleep 5; done
exit 0
