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
SCORE_VERSION = 5  # Bump whenever the scoring formula or filters change (recorded in snapshots)


def get_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None


TOKEN = get_token()


def api(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "lfarc"})
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # On rate limit, wait until the reset time and retry (for unattended runs)
            limited = e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.headers.get("Retry-After"))
            if not limited or attempt == 3:
                raise
            wait = int(e.headers.get("Retry-After") or max(0, int(e.headers.get("X-RateLimit-Reset", "0")) - time.time()) + 1)
            time.sleep(min(wait, 3700))


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
              "list", "lists", "collection", "documentation", "docs"}
DOC_NAME = re.compile(r"(^|[-_.])(awesome|tutorials?|handbook|cheat-?sheets?|roadmap|interview|internships?|jobs?|"
                      r"books?|resources|courses?|leetcode|notes|lists?)([-_.\d]|$)", re.I)
DOC_DESC = re.compile(r"curated list|list of |collection of |cheat ?sheet|roadmap|handbook|interview (question|prep)|"
                      r"internship|tutorial|study (guide|notes)|learning resources", re.I)


def is_docs_repo(r):
    """Does this look like a docs / curated-list / tutorial repo rather than code to contribute to? (heuristic)"""
    if not r.get("language"):  # No code language -> usually docs/data
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
    """Fetch up to pages*100 recent commits and compute contribution concentration (bots excluded).

    contributors: number of distinct contributors
    eff:          effective contributors = 1/sum(commit share^2); one person doing 90% gives ~1.2
    top_share:    commit share of the top contributor
    capped=True:  hit the fetch limit -> commit/contributor counts are lower bounds
    """
    counts, total, capped = {}, 0, False
    dom_commits, dom_authors = {}, {}
    for page in range(1, pages + 1):
        try:
            commits = api(f"/repos/{full_name}/commits", {"since": since_iso, "per_page": 100, "page": page, **({"until": until} if until else {})})
        except urllib.error.HTTPError:
            if page == 1:
                return None
            capped = True
            break
        total += len(commits)
        for c in commits:
            login = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("email")
            if login and not is_bot(login):
                counts[login] = counts.get(login, 0) + 1
                email = (c.get("commit", {}).get("author") or {}).get("email") or ""
                dom = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
                dom_commits[dom] = dom_commits.get(dom, 0) + 1
                dom_authors.setdefault(dom, set()).add(login)
        if len(commits) < 100:
            break
        capped = True if page == pages else capped
    else:
        pass
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
            "corp_domain": corp_domain, "top_login": top_login if top_login and "@" not in top_login else None}


INSIDER = {"OWNER", "MEMBER", "COLLABORATOR"}


ACADEMIC = re.compile(r"univers|institut|academ|college|school|research", re.I)
COMPANY_NAME = re.compile(r"\b(inc\.?|llc|ltd\.?|gmbh|corp(oration)?|technologies|solutions|labs)\b", re.I)


def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def is_company_run(owner, owner_type, top_login):
    """Heuristic: is the repo run by a company that employs its main committer?

    Only organizations with a website (blog) are considered. True when either
      - the top committer's profile company contains the owner org name (e.g. company "@serverless" on serverless/*), or
      - the org's display name or description reads like a company (Inc, Labs, Ltd, GmbH, Solutions, ...; the singular "Lab" is left out because university labs use it),
        unless it also looks academic (university, institute, research, ...).
    The API hides `is_verified` from non-members, so verified-org checks cannot work here.
    """
    if owner_type != "Organization":
        return False
    try:
        org = api(f"/orgs/{owner}")
    except urllib.error.HTTPError:
        return False
    if not (org.get("blog") or "").strip():
        return False
    text = f"{org.get('name') or ''} {org.get('description') or ''}"
    if COMPANY_NAME.search(text) and not ACADEMIC.search(text):  # "Lab" at a university is not a company
        return True
    if top_login and len(_norm(owner)) >= 4:
        try:
            company = _norm(api(f"/users/{top_login}").get("company"))
        except urllib.error.HTTPError:
            return False
        return bool(company) and _norm(owner) in company
    return False


def enrich(full_name):
    """External-PR acceptance rate (merge ratio among recently closed outsider PRs) and the count of entry-level issues (help wanted / good first issue)."""
    accept, ext_n, closed_n = None, 0, 0
    try:
        prs = api(f"/repos/{full_name}/pulls", {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 50})
        ext = [p for p in prs if p.get("author_association") not in INSIDER and (p.get("user") or {}).get("type") != "Bot"]
        ext_n, closed_n = len(ext), len(prs)
        if ext_n >= 3:
            accept = sum(1 for p in ext if p.get("merged_at")) / ext_n
    except urllib.error.HTTPError:
        pass
    entry = 0
    for label in ("help wanted", "good first issue"):
        try:
            items = api(f"/repos/{full_name}/issues", {"labels": label, "state": "open", "per_page": 100})
            entry += sum(1 for x in items if "pull_request" not in x)
        except urllib.error.HTTPError:
            pass
    return {"accept": accept, "ext_prs": ext_n, "closed_prs": closed_n, "entry": entry}


def is_closed_project(x):
    """Outsiders effectively cannot contribute: 90%+ of recently closed PRs are from insiders, or the outsider PR acceptance rate is below 20%."""
    if x["closed_prs"] >= 10 and x["ext_prs"] / x["closed_prs"] < 0.1:
        return True
    return x["accept"] is not None and x["accept"] < 0.2


def final_score(base, extra):
    """base x acceptance factor (0.25-1.0) x entry-issue bonus (up to +10%).

    factor = 0.25 + 0.75 * acceptance rate. With fewer than 3 outsider PRs (not enough data) the rate is
    conservatively taken as 0.4.
    (tune.py backtest: past acceptance was the best predictor of future acceptance, and the top
     of a scarcity-only ranking was actually accepted less often -> the factor is kept strong)
    """
    acc = 0.4 if extra["accept"] is None else extra["accept"]
    accept_factor = 0.25 + 0.75 * acc
    entry_bonus = 1 + 0.1 * min(1.0, extra["entry"] / 5)
    return round(base * accept_factor * entry_bonus, 1)


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
    p.add_argument("--include-closed", action="store_true",
                   help="also include corporate (known company / company email / company-run) repos and repos closed to outside contributions")
    p.add_argument("--include-docs", action="store_true", help="also include docs / curated-list / tutorial repos")
    p.add_argument("--enrich", type=int, default=40,
                   help="re-score the top N by base score after also fetching PR acceptance and entry-level issues (default 40)")
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
                     "_owner": r["owner"]["login"], "_owner_type": r["owner"]["type"], "_top": a["top_login"]})
    if n_corp:
        print(f"Excluded {n_corp} repos whose commit authors concentrate on one company email domain", file=sys.stderr)
    rows.sort(key=lambda x: x["score"], reverse=True)
    head = rows[: max(args.enrich, args.top)]
    print(f"Fetching PR acceptance and entry-level issues for the top {len(head)}...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=8) as ex:
        extras = list(ex.map(lambda r: enrich(r["repo"]), head))
        company = list(ex.map(lambda r: is_company_run(r["_owner"], r["_owner_type"], r["_top"]), head)) \
            if not args.include_closed else [False] * len(head)
    for r, x in zip(head, extras):
        r["accept"], r["external_prs"], r["entry_issues"] = x["accept"], x["ext_prs"], x["entry"]
        r["score"] = final_score(r["base_score"], x)
    if not args.include_closed:
        n0 = len(head)
        kept = [(r, x) for r, x, c in zip(head, extras, company) if not is_closed_project(x) and not c]
        print(f"Excluded {n0 - len(kept)} repos closed to outsiders or run by a company", file=sys.stderr)
        head = [r for r, _ in kept]
    for r in head:  # internal keys are not part of the output
        for k in ("_owner", "_owner_type", "_top"):
            r.pop(k, None)
    rows = sorted(head, key=lambda x: x["score"], reverse=True)[: args.top]
    return rows


def main():
    args = make_parser().parse_args()
    rows = run(args)

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    d = args.days
    print(f"\n{'#':>2} {'SCORE':>5}  {'REPO':<38} {'★':>7} {'ISSUES':>6} {f'COMMIT/{d}d':>10} {'CONTRIB':>7} {'EFF':>5} {'TOP1%':>5} {'★/EFF':>7} {'ISS/EFF':>7} {'ACCEPT':>6} {'HW/GFI':>6}  LANG")
    for i, r in enumerate(rows, 1):
        plus = "+" if r["capped"] else ""
        b = r["breakdown"]
        print(f"{i:>2} {r['score']:>5}  {r['repo']:<38} {r['stars']:>7} {r['open_issues']:>6} "
              f"{str(r[f'commits_{d}d']) + plus:>10} {r[f'contributors_{d}d']:>7} {r['effective_contributors']:>5} "
              f"{round(r['top_share'] * 100):>5} {b['stars_per']:>7} {b['issues_per']:>7} "
              f"{(str(round(r['accept'] * 100)) + '%') if r['accept'] is not None else '?':>6} {r['entry_issues']:>6}  {r['language'] or '-'}")
    print(f"\nScore = scarcity(1/(1+(EFF-1)/2.5)) x (0.4 + 0.4*popularity(stars) + 0.2*issue demand) x outsider-PR acceptance factor "
          f"(ACCEPT; '?' = not enough data, taken as 0.4) x entry-issue bonus (HW/GFI = help wanted + good first issue count). "
          f"EFF = effective contributors (commit distribution over the last {d} days; one person doing everything ~ 1), "
          f"TOP1% = commit share of the top contributor. '+' = lower bound (hit the fetch limit of {args.pages * 100} commits). "
          f"Excluded: EFF > {args.max_contributors:g}, fewer than {args.min_commits} commits, docs/curated lists, "
          f"corporate repos (known company list / company email domain / company-run org), and repos closed to outside contributions.\n")


if __name__ == "__main__":
    main()
