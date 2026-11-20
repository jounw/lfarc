#!/usr/bin/env python3
"""Backtest for the lfarc scoring system.

Checks whether features from the earlier 90 days (P1) predict whether outside contributions were
actually accepted in the most recent 90 days (P2).
  python3 tune.py collect --n 300   # many API calls (~4 per repo). Output: tune_data.json
  python3 tune.py analyze           # per-feature AUC from the saved data (no API calls)

Outcomes (P2):
  viable   = 3+ outsider PRs were closed and 50%+ of them were merged
  newcomer = at least one PR from a first-time contributor (FIRST_TIME_CONTRIBUTOR/FIRST_TIMER/NONE) was merged
Limitation: the marginal value of a contribution (scarcity) has no ground truth and cannot be validated;
this only validates "does the repo accept contributions at all".
"""
import argparse
import json
import math
import types
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import lfarc

OUT = "tune_data.json"
NEWCOMER = {"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(x):
    return datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def collect_one(r, now, days):
    t1, t0 = now - timedelta(days=days), now - timedelta(days=2 * days)
    name = r["full_name"]
    past = lfarc.recent_activity(name, iso(t0), 2, until=iso(t1))
    if past is None or past["commits"] == 0:
        return None
    prs = []
    try:
        for page in (1, 2):
            batch = lfarc.api(f"/repos/{name}/pulls", {"state": "closed", "sort": "updated", "direction": "desc",
                                                       "per_page": 100, "page": page})
            prs += batch
            if len(batch) < 100:
                break
    except urllib.error.HTTPError:
        return None
    slim = []
    for p in prs:
        if not p.get("closed_at") or (p.get("user") or {}).get("type") == "Bot":
            continue
        assoc = p.get("author_association")
        slim.append({"t": p["closed_at"], "merged": bool(p.get("merged_at")),
                     "ext": assoc not in lfarc.INSIDER, "new": assoc in NEWCOMER})
    covered = len(prs) < 200 or min(ts(p["closed_at"]) for p in prs if p.get("closed_at")) <= t0
    def win(a, b):
        w = [p for p in slim if a <= ts(p["t"]) < b]
        ext = [p for p in w if p["ext"]]
        return {"closed": len(w), "ext": len(ext), "ext_merged": sum(p["merged"] for p in ext),
                "new_merged": sum(p["merged"] and p["new"] for p in ext)}
    return {"repo": name, "stars": r["stargazers_count"], "forks": r["forks_count"],
            "issues": r["open_issues_count"], "lang": r["language"], "past": past, "covered": covered,
            "p1": win(t0, t1), "p2": win(t1, now + timedelta(days=1))}


def collect(args):
    now = datetime.now(timezone.utc)
    sa = types.SimpleNamespace(min_stars=2500, max_stars=1_000_000, buckets=8, pool=args.n,
                               language=None, topic=None)
    repos = lfarc.search_repos(sa, (now - timedelta(days=args.days)).strftime("%Y-%m-%d"))
    repos = [r for r in repos if not lfarc.is_docs_repo(r)]
    corp = lfarc.verified_orgs(repos)
    repos = [r for r in repos if r["owner"]["login"] not in corp]
    print(f"Collecting {len(repos)} repos...")
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = [x for x in ex.map(lambda r: collect_one(r, now, args.days), repos) if x]
    json.dump(rows, open(OUT, "w"), ensure_ascii=False)
    print(f"Saved {len(rows)} repos -> {OUT}")


def auc(xs, ys):
    """Mann-Whitney AUC (ties get the average rank). 0.5 = no signal, 1 = perfect, 0 = perfectly inverted."""
    pairs = sorted(zip(xs, ys))
    ranks, i = [0.0] * len(pairs), 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2 + 1
        i = j + 1
    pos = [rk for rk, (_, y) in zip(ranks, pairs) if y]
    n1, n0 = len(pos), len(pairs) - len(pos)
    return (sum(pos) - n1 * (n1 + 1) / 2) / (n1 * n0) if n1 and n0 else float("nan")


def analyze(_):
    rows = [r for r in json.load(open(OUT)) if r["covered"]]
    feats = {
        "log_stars": lambda r: math.log10(r["stars"]),
        "forks/stars": lambda r: r["forks"] / r["stars"],
        "eff (lower = scarcer)": lambda r: -r["past"]["eff"],
        "top1_share": lambda r: r["past"]["top_share"],
        "commits_p1": lambda r: math.log10(1 + r["past"]["commits"]),
        "contributors_p1": lambda r: -r["past"]["contributors"],
        "ext_share_p1": lambda r: r["p1"]["ext"] / r["p1"]["closed"] if r["p1"]["closed"] else 0.5,
        "accept_p1": lambda r: r["p1"]["ext_merged"] / r["p1"]["ext"] if r["p1"]["ext"] >= 3 else 0.5,
        "ext_prs_p1": lambda r: math.log10(1 + r["p1"]["ext"]),
        "issues/eff": lambda r: math.log10(1 + r["issues"] / max(r["past"]["eff"], 1)),
        "current base score": lambda r: lfarc.score({"stargazers_count": r["stars"], "open_issues_count": r["issues"]},
                                                 r["past"])[0],
    }
    viable_rows = [r for r in rows if r["p2"]["ext"] >= 3]
    outcomes = {
        "viable (50%+ accepted)": (viable_rows, lambda r: r["p2"]["ext_merged"] / r["p2"]["ext"] >= 0.5),
        "newcomer merged >= 1": (rows, lambda r: r["p2"]["new_merged"] >= 1),
    }
    print(f"{len(rows)} samples ({len(viable_rows)} evaluable for viable)")
    for oname, (rs, f) in outcomes.items():
        ys = [f(r) for r in rs]
        print(f"\n== {oname}: {sum(ys)}/{len(ys)} positive")
        res = sorted(((auc([fn(r) for r in rs], ys), n) for n, fn in feats.items()), reverse=True)
        for a, n in res:
            print(f"  AUC {a:.3f}  {n}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["collect", "analyze"])
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--days", type=int, default=90)
    a = p.parse_args()
    {"collect": collect, "analyze": analyze}[a.cmd](a)
