#!/usr/bin/env python3
"""lfarc - Looking For A Repo to Contribute.

Finds GitHub repos that many people use but few people maintain (i.e. where your contribution matters),
and ranks them with a score. Standard library only. Token: GITHUB_TOKEN env var or `gh auth login`.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
SCORE_VERSION = 7  # Bump whenever the scoring formula or filters change (recorded in snapshots)


def get_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


TOKEN = get_token()


def _get(path, params=None):
    """GET a GitHub API path and return (parsed JSON, response headers)."""
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "lfarc"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r), r.headers
        except urllib.error.HTTPError as e:
            # On rate limit, wait until the reset time and retry (for unattended runs)
            limited = e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After"))
            if not limited or attempt == 3:
                raise
            wait = int(e.headers.get("Retry-After") or max(0, int(e.headers.get("X-RateLimit-Reset", "0")) - time.time()) + 1)
            time.sleep(min(wait, 3700))


def api(path, params=None):
    return _get(path, params)[0]


def api_count(path, params=None):
    """Total number of items in a list endpoint, read from the Link header of a per_page=1 request (1 call)."""
    data, headers = _get(path, {**(params or {}), "per_page": 1})
    m = re.search(r'[?&]page=(\d+)>; rel="last"', headers.get("Link") or "")
    return int(m.group(1)) if m else len(data)


def star_buckets(lo, hi, n):
    """Split [lo, hi] into n log-scale buckets (non-overlapping integer ranges)."""
    n = max(1, min(n, hi - lo + 1))
    edges = [round(lo * (hi / lo) ** (i / n)) for i in range(n + 1)]
    edges[-1] = hi + 1
    out = [(edges[i], edges[i + 1] - 1) for i in range(n) if edges[i] < edges[i + 1]]
    return out


def search_repos(args, since):
    """Search per star bucket so the candidate pool spreads evenly across the whole star range.

    Each bucket gets pool/n_buckets slots; the unused share of buckets that run out of
    repos is redistributed to buckets that still have more (until the pool is full).
    """
    base = [f"pushed:>={since}", "archived:false", "fork:false"]
    if args.language:
        base.append(f"language:{args.language}")
    if args.topic:
        base.append(f"topic:{args.topic}")
    buckets = star_buckets(args.min_stars, args.max_stars, args.buckets)
    state = [{"q": " ".join([f"stars:{lo}..{hi}"] + base), "items": [], "page": 1, "done": False}
             for lo, hi in buckets]

    def fill(st, target):
        while len(st["items"]) < target and not st["done"]:
            if st["page"] > 10:  # GitHub search returns at most 1000 results per query
                st["done"] = True
                break
            data = api("/search/repositories", {"q": st["q"], "sort": "stars", "order": "desc",
                                                "per_page": 100, "page": st["page"]})
            items = data.get("items", [])
            st["page"] += 1
            st["items"] += items
            if len(items) < 100:
                st["done"] = True

    targets = [-(-args.pool // len(state))] * len(state)  # ceil
    for _ in range(6):
        for st, t in zip(state, targets):
            fill(st, t)
        leftover = args.pool - sum(min(len(st["items"]), t) for st, t in zip(state, targets))
        more = [i for i, st in enumerate(state) if len(st["items"]) > targets[i] or not st["done"]]
        if leftover <= 0 or not more:
            break
        extra = -(-leftover // len(more))
        for i in more:
            targets[i] += extra
    repos = [r for st, t in zip(state, targets) for r in st["items"][:t]]
    return repos[: args.pool]


DOC_TOPICS = {"awesome", "awesome-list", "tutorial", "tutorials", "interview", "interview-questions", "cheatsheet",
              "cheat-sheet", "roadmap", "handbook", "books", "book", "curated-list", "resources", "learning-resources",
              "leetcode", "coding-interview", "internships", "jobs", "course", "courses", "free-programming-books",
              "list", "lists", "collection", "documentation", "docs", "guide", "guides", "examples", "samples",
              "dotfiles", "exercises"}
DOC_NAME = re.compile(r"(^|[-_.])(awesome|tutorials?|handbook|cheat-?sheets?|roadmap|interview|internships?|jobs?|"
                      r"books?|resources|courses?|leetcode|notes|lists?|guides?|examples?|samples?|snippets|dotfiles|"
                      r"exercises|challenges|katas?)([-_.\d]|$)", re.I)
DOC_DESC = re.compile(r"curated list|list of |collection of |cheat ?sheet|roadmap|handbook|interview (question|prep)|"
                      r"internship|tutorial|study (guide|notes)|learning resources|\bguide (to|for)\b|"
                      r"\bexamples? (of|for)\b|\bexercises\b|\bmirror\b|(from|for) the book|\bbook\b.*\bsource code\b|"
                      r"\bsource code\b.*\bbook\b", re.I)


def is_docs_repo(r):
    """Does this look like something you cannot usefully contribute code to? (heuristic)

    Docs, curated lists, tutorials, guides, example/sample collections, dotfiles and read-only mirrors.
    """
    if not r.get("language"):  # No code language -> usually docs/data
        return True
    if r.get("mirror_url"):
        return True
    if set(r.get("topics") or []) & DOC_TOPICS:
        return True
    if DOC_NAME.search(r["name"]):
        return True
    return bool(DOC_DESC.search(r.get("description") or ""))


# Known big-company and government orgs (lowercase). Excluded because the company roadmap drives them even when they accept contributions.
CORP_ORGS = {
    "microsoft", "azure", "azure-samples", "microsoftdocs", "dotnet", "google", "googleapis", "googlecloudplatform",
    "google-deepmind", "google-research", "google-ai-edge", "googlechrome", "firebase", "facebook", "facebookresearch",
    "facebookincubator", "meta-llama", "meta-pytorch", "apple", "aws", "awslabs", "aws-samples", "amzn", "openai",
    "anthropics", "nvidia", "nvidiagameworks", "nvlabs", "intel", "amd", "ibm", "oracle", "salesforce", "vmware",
    "alibaba", "tencent", "bytedance", "baidu", "paddlepaddle", "vercel", "netlify", "hashicorp", "elastic", "mongodb",
    "cockroachdb", "grafana", "datadog", "stripe", "shopify", "uber", "netflix", "airbnb", "linkedin", "spotify",
    "slackhq", "atlassian", "jetbrains", "github", "gitlab-org", "tesla", "samsung", "sony", "cloudflare",
    "unity-technologies", "epicgames", "huawei", "xiaomi", "tiktok", "alibaba-damo-academy", "qwenlm",
    "android", "androidx", "flutter", "angular", "tensorflow", "chromium", "dart-lang", "golang", "jax-ml", "kotlin",
    "nationalsecurityagency", "nasa", "cisagov", "uswds", "18f",
}
PERSONAL_MAIL = {"gmail.com", "googlemail.com", "users.noreply.github.com", "outlook.com", "hotmail.com", "live.com",
                 "yahoo.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "pm.me", "hey.com", "qq.com",
                 "163.com", "126.com", "sina.com", "naver.com", "daum.net", "kakao.com", "hanmail.net", "gmx.com",
                 "gmx.de", "mail.com", "example.com", "localhost", ""}


def is_personal_domain(d):
    """True for personal-mail and university domains (excluded from company-domain detection)."""
    return d in PERSONAL_MAIL or d.endswith(".edu") or ".edu." in d or ".ac." in d or d.endswith(".local")


def is_bot(login):
    l = login.lower()
    return l.endswith("[bot]") or l.endswith("bot") or l.endswith("-bot")


def recent_activity(full_name, since_iso, pages, until=None):
    """Sample up to pages*100 commits from the window and compute contribution concentration (bots excluded).

    The first request takes the latest 100 commits. If that page is full, the remaining requests are spread evenly
    back through the window (the 100 commits before each cut-off) instead of taking the next-latest pages; otherwise
    a very active repo would be judged on its last few days only.

    contributors: number of distinct contributors in the sample
    eff:          effective contributors = 1/sum(commit share^2); one person doing 90% gives ~1.2
    top_share:    commit share of the top contributor
    capped=True:  the window holds more commits than were sampled -> commit/contributor counts are lower bounds
    core:         logins with >= 40% of the sampled commits (dominant committers are maintainers even when
                  GitHub does not label them as members; internal use only). The bar is high on purpose: in a
                  small repo a successful outside contributor quickly reaches 10-30% of recent commits.
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    start = datetime.strptime(since_iso, fmt)
    end = datetime.strptime(until, fmt) if until else datetime.now(timezone.utc).replace(tzinfo=None)
    counts, seen, capped = {}, set(), False
    dom_commits, dom_authors = {}, {}
    for k in range(pages):
        params = {"since": since_iso, "per_page": 100}
        if k:
            params["until"] = (end - (end - start) * k / pages).strftime(fmt)
        elif until:
            params["until"] = until
        try:
            commits = api(f"/repos/{full_name}/commits", params)
        except urllib.error.HTTPError:
            if k == 0:
                return None
            break
        for c in commits:
            if c.get("sha") in seen:
                continue
            seen.add(c.get("sha"))
            login = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("email")
            if login and not is_bot(login):
                counts[login] = counts.get(login, 0) + 1
                email = (c.get("commit", {}).get("author") or {}).get("email") or ""
                dom = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
                dom_commits[dom] = dom_commits.get(dom, 0) + 1
                dom_authors.setdefault(dom, set()).add(login)
        if len(commits) == 100:
            capped = True
        if k == 0 and len(commits) < 100:
            break  # the whole window fit in one page
    total = len(seen)
    n = sum(counts.values())
    eff = 1 / sum((v / n) ** 2 for v in counts.values()) if n else 0.0
    # Company-driven? One non-personal, non-university email domain covers 50%+ of commits, from 2+ distinct authors
    corp_domain = ""
    if n:
        cand = [(v, d) for d, v in dom_commits.items() if not is_personal_domain(d)]
        if cand:
            v, d = max(cand)
            if v / n >= 0.5 and len(dom_authors[d]) >= 2:
                corp_domain = d
    top_login = max(counts, key=counts.get) if counts else None
    return {"commits": total, "contributors": len(counts), "eff": round(eff, 1),
            "top_share": round(max(counts.values()) / n, 2) if n else 0.0, "capped": capped,
            "corp_domain": corp_domain, "top_login": top_login if top_login and "@" not in top_login else None,
            "core": sorted(k for k, v in counts.items() if "@" not in k and v / n >= 0.4)}


INSIDER = {"OWNER", "MEMBER", "COLLABORATOR"}


ACADEMIC = re.compile(r"univers|institut|academ|college|school|research", re.I)
COMPANY_NAME = re.compile(r"\b(inc\.?|llc|ltd\.?|gmbh|corp(oration)?|technologies|solutions|labs)\b", re.I)


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def assess_owner(owner, owner_type, top_login):
    """Classify the owning organization: {"company": bool, "academic": bool}. Users are neither.

    company (only orgs with a website/blog are considered), true when either
      - the org's display name or description reads like a company (Inc, Labs, Ltd, GmbH, Solutions, ...; the
        singular "Lab" is left out because university labs use it) and does not also look academic, or
      - the top committer's profile company contains the org name (e.g. company "@serverless" on serverless/*).
    academic: the org's name or description mentions a university, institute, research, ...
    The API hides `is_verified` from non-members, so verified-org checks cannot work here.
    """
    out = {"company": False, "academic": False}
    if owner_type != "Organization":
        return out
    try:
        org = api(f"/orgs/{owner}")
    except urllib.error.HTTPError:
        return out
    text = f"{org.get('name') or ''} {org.get('description') or ''}"
    out["academic"] = bool(ACADEMIC.search(text))
    if not (org.get("blog") or "").strip():
        return out
    if COMPANY_NAME.search(text) and not out["academic"]:
        out["company"] = True
    elif top_login and len(_norm(owner)) >= 4:
        try:
            company = _norm(api(f"/users/{top_login}").get("company"))
        except urllib.error.HTTPError:
            return out
        out["company"] = bool(company) and _norm(owner) in company
    return out


RESEARCH_TOPICS = {"research", "science", "scientific-computing", "scientific-machine-learning", "arxiv", "paper",
                   "papers", "physics", "astrophysics", "astronomy", "biology", "chemistry", "genomics",
                   "bioinformatics", "neuroscience", "mathematics", "symbolic-regression", "computational-biology",
                   "computational-chemistry", "computational-physics", "climate", "materials-science"}
RESEARCH_TEXT = re.compile(r"\b(research|scientific|arxiv|paper|academic|physics|astrophysics|astronomy|biology|"
                           r"chemistry|genomics|bioinformatics|neuroscience)\b", re.I)
RESEARCH_HOME = re.compile(r"arxiv\.org|doi\.org|\.edu(/|$|\.)|\.ac\.[a-z]{2}(/|$)", re.I)
RESEARCH_BONUS = 1.05  # deliberately small: research projects get a slight nudge, not a different ranking


def is_research(topics, description, homepage, academic_owner):
    """Does this look like a research / scientific project? (heuristic used only for a small bonus)"""
    return bool(academic_owner or set(topics or []) & RESEARCH_TOPICS or RESEARCH_TEXT.search(description or "")
                or RESEARCH_HOME.search(homepage or ""))


ZERO_ACCEPT_FACTOR = 0.4  # score multiplier when outsiders sent a few PRs and none was merged


def enrich(full_name, core, since_iso, limits=None):
    """Second-stage signals for one repo, mostly from its pull requests.

    accept:           merge ratio among recently closed outsider PRs. An outsider is not a bot, not an
                      OWNER/MEMBER/COLLABORATOR and not a hidden maintainer (a dominant committer whose own PRs are
                      nearly always merged; people whose org membership is private show up as plain contributors,
                      which used to inflate this). Needs >= 3 PRs from >= 3 authors.
    crowd:            distinct outsider authors whose PRs were closed or merged inside the recent window: how many
                      other people are already contributing (competition).
    pr_authors:       distinct authors of PRs merged inside the window (informational).
    open_prs:         open pull requests opened by humans (review backlog); bot PRs such as dependency bumps are not counted.
    contributors_all: all-time contributors.
    merged_recent:    human PRs merged inside the window (at most 200 are looked at).
    reason:           why the repo should be dropped ("backlog", "crowded", "closed"), or None.
    limits = (max_open_prs, max_all_contributors); None disables the red-ocean checks.
    """
    x = {"accept": None, "ext_prs": 0, "ext_authors": 0, "closed_prs": 0, "crowd": 0, "pr_authors": 0,
         "merged_recent": 0, "open_prs": None, "contributors_all": None, "entry": 0, "reason": None}
    core = {c.lower() for c in core or []}
    closed = []
    try:
        for page in (1, 2):
            batch = api(f"/repos/{full_name}/pulls", {"state": "closed", "sort": "updated", "direction": "desc",
                                                       "per_page": 100, "page": page})
            closed += batch
            if len(batch) < 100:
                break
    except urllib.error.HTTPError:
        pass
    login = lambda p: ((p.get("user") or {}).get("login") or "").lower()
    is_human = lambda p: (p.get("user") or {}).get("type") != "Bot" and login(p) and not is_bot(login(p))
    human = [p for p in closed if is_human(p)]
    # A dominant committer counts as a maintainer only if their own PRs are (almost) always merged; an outside
    # contributor who happens to author much of a small repo's recent commits still gets PRs rejected.
    stats = {}
    for p in human:
        if login(p) in core:
            n_closed, n_merged = stats.get(login(p), (0, 0))
            stats[login(p)] = (n_closed + 1, n_merged + bool(p.get("merged_at")))
    core = {l for l, (n_closed, n_merged) in stats.items() if n_closed >= 5 and n_merged / n_closed >= 0.8}
    ext = [p for p in human if p.get("author_association") not in INSIDER and login(p) not in core]
    x["closed_prs"], x["ext_prs"], x["ext_authors"] = len(human), len(ext), len({login(p) for p in ext})
    if x["ext_prs"] >= 3 and x["ext_authors"] >= 3:
        x["accept"] = sum(1 for p in ext if p.get("merged_at")) / x["ext_prs"]
    # ISO-8601 UTC strings compare chronologically
    x["crowd"] = len({login(p) for p in ext if (p.get("closed_at") or "") >= since_iso})
    merged_recent = [p for p in human if (p.get("merged_at") or "") >= since_iso]
    x["pr_authors"], x["merged_recent"] = len({login(p) for p in merged_recent}), len(merged_recent)
    try:
        opened = api(f"/repos/{full_name}/pulls", {"state": "open", "per_page": 100})
        mine = sum(1 for p in opened if is_human(p))
        if len(opened) < 100:
            x["open_prs"] = mine
        else:  # more than one page: scale the total by the human share of the first page
            x["open_prs"] = round(api_count(f"/repos/{full_name}/pulls", {"state": "open"}) * mine / 100)
    except urllib.error.HTTPError:
        pass
    try:
        x["contributors_all"] = api_count(f"/repos/{full_name}/contributors", {"anon": "true"})
    except urllib.error.HTTPError:
        pass  # GitHub refuses to list contributors for a few very large repos

    if limits:
        max_open, max_all = limits
        # Backlog: many open PRs AND more of them than were merged in the whole window (a queue of 3+ months).
        # A popular repo that merges faster than PRs pile up is not a backlog, however many are open.
        if x["open_prs"] is not None and x["open_prs"] > max_open and x["open_prs"] > x["merged_recent"]:
            x["reason"] = "backlog"
        elif x["contributors_all"] is not None and x["contributors_all"] > max_all:
            x["reason"] = "crowded"
        elif is_closed_project(x):
            x["reason"] = "closed"
        if x["reason"]:
            return x  # skip the remaining calls for repos that are dropped anyway

    for label in ("help wanted", "good first issue"):
        try:
            items = api(f"/repos/{full_name}/issues", {"labels": label, "state": "open", "per_page": 100})
            x["entry"] += sum(1 for i in items if "pull_request" not in i)
        except urllib.error.HTTPError:
            pass
    return x


def is_closed_project(x):
    """Outsiders effectively cannot contribute.

    Either none of at least 5 outsider PRs got merged, or there are plenty of closed PRs (>= 20) but fewer
    than two distinct outsider authors among them, i.e. the PR flow is the maintainers' own.
    """
    if x["accept"] == 0 and x["ext_prs"] >= 5:
        return True
    return x["closed_prs"] >= 20 and x["ext_authors"] < 2


def crowd_factor(n):
    """Penalty for competition: 1.0 up to 5 recent outsider PR authors, then 1/(1+(n-5)/20) (25 -> 0.5, 45 -> 0.33)."""
    return 1 / (1 + max(0, n - 5) / 20)


def final_score(base, extra):
    """base x crowd factor x entry-issue bonus (up to +10%) x research bonus (+5%), x0.4 if no outsider PR was merged.

    The outsider acceptance rate is deliberately ignored unless it is zero: any merged outsider PR shows the door is
    open, and the exact ratio says more about who happened to send PRs than about the project. Zero of 3-4 PRs
    costs ZERO_ACCEPT_FACTOR; zero of 5 or more removes the repo (see is_closed_project).
    """
    accept_factor = ZERO_ACCEPT_FACTOR if extra["accept"] == 0 else 1.0
    entry_bonus = 1 + 0.1 * min(1.0, extra["entry"] / 5)
    research_bonus = RESEARCH_BONUS if extra.get("research") else 1.0
    return round(base * accept_factor * crowd_factor(extra.get("crowd", 0)) * entry_bonus * research_bonus, 1)


def clamp(x):
    return max(0.0, min(1.0, x))


def lognorm(x, lo, hi):
    """Log-scale normalization: lo -> 0, hi -> 1 (clamped)."""
    return clamp((math.log10(max(x, 1)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


def logpos(x, lo, hi):
    """Log scale, lo -> 0, hi -> 1. Clipped to 0 below, not clipped above (avoids ties)."""
    return max(0.0, (math.log10(max(x, 1)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


def sat(x):
    """0 -> 0, converging smoothly toward 1 (no hard cap, so no ties)."""
    return 1 - math.exp(-x)


def score(repo, act):
    """0-100. Scarcity (multiplicative) x (base floor + popularity + demand).

    scarcity = 1 / (1 + (EFF-1)/2.5)              EFF 1->1.0, 2->0.71, 4->0.45, 6->0.33, 10->0.22
    pop      = sat(logpos(stars; 500->0, 50,000->1))   more stars is better, with diminishing returns
    demand   = sat(logpos(issues/EFF; 1->0, 100->1))
    score    = 100 * scarcity * (0.4 + 0.4*pop + 0.2*demand)
    -> However many stars, many effective contributors means a low score; a repo carried by 1-2 people
       scores high even with a moderate star count.
    Effective contributors (EFF) = 1/sum(commit share^2), at least 1. Zero commits scores 0.
    """
    if act["commits"] == 0 or act["eff"] == 0:
        return 0.0, {"stars_per": 0, "issues_per": 0}
    e = max(act["eff"], 1.0)
    issues_per = repo["open_issues_count"] / e
    scarcity = 1 / (1 + (e - 1) / 2.5)
    pop = sat(logpos(repo["stargazers_count"], 500, 50_000))
    demand = sat(logpos(issues_per, 1, 100))
    parts = {"stars_per": round(repo["stargazers_count"] / e), "issues_per": round(issues_per, 1)}
    return round(100 * scarcity * (0.4 + 0.4 * pop + 0.2 * demand), 1), parts


def make_parser():
    p = argparse.ArgumentParser(description="Find GitHub repos that many people use but few people maintain")
    p.add_argument("-l", "--language", help="e.g. python, rust")
    p.add_argument("-t", "--topic", help="e.g. cli, machine-learning")
    p.add_argument("--min-stars", type=int, default=2500)
    p.add_argument("--max-stars", type=int, default=1_000_000, help="star upper bound (default 1,000,000 = effectively none)")
    p.add_argument("--days", type=int, default=90, help="what counts as recent, in days (default 90)")
    p.add_argument("--buckets", type=int, default=8, help="number of log-scale buckets to split the star range into (default 8)")
    p.add_argument("--pool", type=int, default=100, help="number of candidate repos to analyze (default 100; at least 1 API call each)")
    p.add_argument("--max-contributors", type=float, default=10,
                   help="exclude repos whose effective contributors exceed this, as red oceans (default 10)")
    p.add_argument("--pages", type=int, default=3, help="commit pages fetched per repo (100 commits/page, default 3)")
    p.add_argument("--min-commits", type=int, default=5,
                   help="exclude repos with fewer commits than this in the recent window, as effectively dead (default 5)")
    p.add_argument("--max-open-prs", type=int, default=50,
                   help="exclude repos with more open (human) PRs than this when that also exceeds the PRs merged in the window: a review backlog (default 50)")
    p.add_argument("--max-all-contributors", type=int, default=1000,
                   help="exclude repos with more all-time contributors than this, as crowded (default 1000)")
    p.add_argument("--include-closed", action="store_true",
                   help="also include corporate repos, repos closed to outside contributions, and crowded / backlogged repos")
    p.add_argument("--include-docs", action="store_true", help="also include docs / curated-list / tutorial repos")
    p.add_argument("--enrich", type=int, default=40,
                   help="re-score the top N by base score with the second-stage (pull request) signals (default 40)")
    p.add_argument("-n", "--top", type=int, default=20, help="number of results to print")
    p.add_argument("--json", action="store_true")
    return p


def run(args):
    """Run the whole pipeline and return the repos (list of dicts) sorted by score."""
    if not TOKEN:
        print("Warning: no token found, so the rate limit is very low. Set GITHUB_TOKEN or run `gh auth login`.", file=sys.stderr)

    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pushed_since = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")

    repos = search_repos(args, pushed_since)
    if not args.include_docs:
        n0 = len(repos)
        repos = [r for r in repos if not is_docs_repo(r)]
        print(f"Excluded {n0 - len(repos)} docs/curated-list repos", file=sys.stderr)
    if not args.include_closed:
        n0 = len(repos)
        repos = [r for r in repos if r["owner"]["login"].lower() not in CORP_ORGS]
        print(f"Excluded {n0 - len(repos)} corporate repos (known company list)", file=sys.stderr)
    print(f"Analyzing {len(repos)} candidates...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=8) as ex:
        acts = list(ex.map(lambda r: recent_activity(r["full_name"], since_iso, args.pages), repos))

    rows, n_corp = [], 0
    for r, a in zip(repos, acts):
        if a is None or a["commits"] < args.min_commits or a["eff"] > args.max_contributors:
            continue
        if a["corp_domain"] and not args.include_closed:
            n_corp += 1
            continue
        s, parts = score(r, a)
        rows.append({"repo": r["full_name"], "score": s, "stars": r["stargazers_count"], "forks": r["forks_count"],
                     "open_issues": r["open_issues_count"], f"commits_{args.days}d": a["commits"],
                     f"contributors_{args.days}d": a["contributors"], "effective_contributors": a["eff"], "top_share": a["top_share"], "capped": a["capped"], "language": r["language"],
                     "url": r["html_url"], "description": (r.get("description") or "")[:200],
                     "breakdown": parts, "base_score": s,
                     "_owner": r["owner"]["login"], "_owner_type": r["owner"]["type"], "_top": a["top_login"],
                     "_core": a["core"], "_topics": r.get("topics") or [], "_homepage": r.get("homepage") or ""})
    if n_corp:
        print(f"Excluded {n_corp} repos whose commit authors concentrate on one company email domain", file=sys.stderr)
    rows.sort(key=lambda x: x["score"], reverse=True)
    head = rows[: max(args.enrich, args.top)]
    print(f"Second stage (pull requests, contributors, owner) for the top {len(head)}...", file=sys.stderr)
    limits = None if args.include_closed else (args.max_open_prs, args.max_all_contributors)
    ckey = f"commits_{args.days}d"

    def stage2(r):
        x = enrich(r["repo"], r["_core"], since_iso, limits)
        owner = {"company": False, "academic": False} if x["reason"] else assess_owner(r["_owner"], r["_owner_type"], r["_top"])
        return x, owner

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(stage2, head))

    kept, dropped = [], {}
    for r, (x, owner) in zip(head, results):
        reason = x["reason"] or ("company" if owner["company"] and not args.include_closed else None)
        eff = r["effective_contributors"]
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        # open_issues_count from the API includes open PRs; subtract them to get real issues
        issues = max(0, r["open_issues"] - x["open_prs"]) if x["open_prs"] is not None else r["open_issues"]
        base, parts = score({"stargazers_count": r["stars"], "open_issues_count": issues}, {"commits": r[ckey], "eff": eff})
        x["research"] = is_research(r["_topics"], r["description"], r["_homepage"], owner["academic"])
        r.update({"open_issues": issues, "open_prs": x["open_prs"], "contributors_all": x["contributors_all"],
                  "merged_pr_authors": x["pr_authors"], "outsider_pr_authors": x["crowd"],
                  "accept": x["accept"], "external_prs": x["ext_prs"],
                  "entry_issues": x["entry"], "research": x["research"], "breakdown": parts, "base_score": base,
                  "score": final_score(base, x)})
        kept.append(r)
    if dropped:
        print("Excluded in the second stage: " + ", ".join(f"{v} {k}" for k, v in sorted(dropped.items())), file=sys.stderr)
    for r in kept:  # internal keys are not part of the output
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
    return sorted(kept, key=lambda x: x["score"], reverse=True)[: args.top]


def main():
    args = make_parser().parse_args()
    rows = run(args)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    d = args.days
    show = lambda v: "?" if v is None else str(v)
    print(f"\n{'#':>2} {'SCORE':>5}  {'REPO':<38} {'★':>7} {'ISSUES':>6} {'OPEN_PR':>7} {'ALL_CONTRIB':>11} {'CROWD':>5} {f'COMMIT/{d}d':>10} "
          f"{'EFF':>5} {'TOP1%':>5} {'ACCEPT':>6} {'HW/GFI':>6}  LANG")
    for i, r in enumerate(rows, 1):
        plus = "+" if r["capped"] else ""
        print(f"{i:>2} {r['score']:>5}  {r['repo'] + (' [research]' if r['research'] else ''):<38} {r['stars']:>7} {r['open_issues']:>6} "
              f"{show(r['open_prs']):>7} {show(r['contributors_all']):>11} {r['outsider_pr_authors']:>5} {str(r[f'commits_{d}d']) + plus:>10} "
              f"{r['effective_contributors']:>5} {round(r['top_share'] * 100):>5} "
              f"{(str(round(r['accept'] * 100)) + '%') if r['accept'] is not None else '?':>6} {r['entry_issues']:>6}  {r['language'] or '-'}")
    print(f"\nScore = scarcity(1/(1+(EFF-1)/2.5)) x (0.4 + 0.4*popularity(stars) + 0.2*issue demand) x crowd factor "
          f"(CROWD = outsider PR authors in the last {d} days; no penalty up to 5) x entry-issue bonus (HW/GFI) x research bonus (+5%). "
          f"ACCEPT (outsider PRs merged) is informational and only matters at 0%: x0.4, or excluded with 5+ outsider PRs; "
          f"'?' = not enough outsider PRs/authors. "
          f"EFF = effective contributors from a commit sample spread over the last {d} days, "
          f"TOP1% = commit share of the top contributor, ISSUES excludes pull requests, OPEN_PR excludes bots. "
          f"'+' = lower bound (the window holds more than the {args.pages * 100} sampled commits). "
          f"Excluded: EFF > {args.max_contributors:g}, fewer than {args.min_commits} commits, docs/guides/examples/mirrors, corporate repos, "
          f"repos closed to outsiders, PR backlogs (more than {args.max_open_prs} open PRs and more open than merged in {d} days), "
          f"more than {args.max_all_contributors} all-time contributors.\n")

if __name__ == "__main__":
    main()
