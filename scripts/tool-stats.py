#!/usr/bin/env python3
"""Per-tool call counts and error rates from the Nexus database.

    scripts/tool-stats.py [--since DAYS] [--errors N]

Reads `$NEXUS10_HOME/nexus.db` (default `~/.nexus10`) read-only. Run it before
and after a change to a tool or the system prompt to see what moved.
"""
import argparse
import os
import sqlite3
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
parser.add_argument("--since", type=float, help="only calls from the last DAYS days")
parser.add_argument("--errors", type=int, default=0, help="also show the N most recent error messages")
args = parser.parse_args()

home = Path(os.environ.get("NEXUS10_HOME", Path.home() / ".nexus"))
db = sqlite3.connect(f"file:{home / 'nexus.db'}?mode=ro", uri=True)
where = "where started >= ?" if args.since else ""
params = (time.time() - args.since * 86400,) if args.since else ()

print(f"{'tool':<24}{'calls':>7}{'errors':>8}{'rate':>7}{'avg out':>10}")
for name, calls, errors, avg in db.execute(
    f"select name, count(*), sum(is_error), avg(length(output)) from turn_tool {where} group by name order by 2 desc",
    params,
):
    print(f"{name:<24}{calls:>7}{errors:>8}{errors / calls:>7.0%}{avg or 0:>10.0f}")

(cd_total,) = db.execute(
    f"select count(*) from turn_tool {where + ' and ' if where else 'where '}name = 'bash' and arguments like '{{%' "
    "and trim(json_extract(arguments, '$.command')) like 'cd %'", params).fetchone()
(bash_total,) = db.execute(
    f"select count(*) from turn_tool {where + ' and ' if where else 'where '}name = 'bash'", params).fetchone()
print(f"\nbash calls that start with `cd`: {cd_total} of {bash_total}")

if args.errors:
    print()
    rows = db.execute(
        f"select name, replace(substr(output, 1, 160), char(10), ' ') from turn_tool "
        f"{where + ' and ' if where else 'where '}is_error = 1 order by started desc limit ?", (*params, args.errors))
    for name, text in rows:
        print(f"{name:<12}{text}")
