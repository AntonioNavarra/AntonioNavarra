#!/usr/bin/env python3
"""
today.py

Generates two SVG cards (dark_mode.svg / light_mode.svg) with live GitHub
stats for a given user: total stars, commits, pull requests, issues,
repositories, followers, and lines of code added/removed across owned,
non-fork repositories.

Data sources:
- GitHub GraphQL API v4 for contribution/social counts.
- GitHub REST API "stats/contributors" endpoint for LOC additions/deletions.

Meant to run inside GitHub Actions on a schedule (see
.github/workflows/main.yml), authenticated with a personal access token
stored as the METRICS_TOKEN secret. The token is read from the environment
only; it is never logged or written to disk.
"""

import os
import sys
import time
from datetime import datetime, timezone
from string import Template

import requests

GITHUB_USERNAME = os.environ.get("METRICS_USERNAME", "AntonioNavarra")
TOKEN = os.environ.get("METRICS_TOKEN")

if not TOKEN:
    print("ERROR: METRICS_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

GRAPHQL_URL = "https://api.github.com/graphql"
REST_ROOT = "https://api.github.com"
HEADERS = {
    "Authorization": f"bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(SCRIPT_DIR, "templates")
OUTPUT_DIR = SCRIPT_DIR


def graphql(query, variables=None):
    resp = requests.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def get_account_created_at(username):
    query = """
    query($login: String!) {
      user(login: $login) {
        createdAt
        followers { totalCount }
        repositories(first: 1, ownerAffiliations: OWNER, isFork: false) {
          totalCount
        }
      }
    }
    """
    data = graphql(query, {"login": username})["user"]
    created_at = datetime.strptime(data["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return created_at, data["followers"]["totalCount"], data["repositories"]["totalCount"]


def get_contribution_totals(username, created_at):
    """Sum commit/PR/issue contributions year by year since account creation,
    since contributionsCollection only covers a max ~1 year window per call."""
    now = datetime.now(timezone.utc)
    total_commits = 0
    total_prs = 0
    total_issues = 0
    total_reviews = 0

    year_start = created_at
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
          totalPullRequestContributions
          totalIssueContributions
          totalPullRequestReviewContributions
        }
      }
    }
    """
    while year_start < now:
        year_end = min(year_start.replace(year=year_start.year + 1), now)
        variables = {
            "login": username,
            "from": year_start.isoformat().replace("+00:00", "Z"),
            "to": year_end.isoformat().replace("+00:00", "Z"),
        }
        data = graphql(query, variables)["user"]["contributionsCollection"]
        total_commits += data["totalCommitContributions"] + data["restrictedContributionsCount"]
        total_prs += data["totalPullRequestContributions"]
        total_issues += data["totalIssueContributions"]
        total_reviews += data["totalPullRequestReviewContributions"]
        year_start = year_end

    return total_commits, total_prs, total_issues, total_reviews


def get_owned_repos(username):
    query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { name stargazerCount defaultBranchRef { name } }
        }
      }
    }
    """
    repos = []
    after = None
    while True:
        data = graphql(query, {"login": username, "after": after})["user"]["repositories"]
        repos.extend(data["nodes"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        after = data["pageInfo"]["endCursor"]
    return repos


def get_loc_stats(username, repos):
    """Sum additions/deletions credited to `username` via the REST
    stats/contributors endpoint. GitHub computes these stats asynchronously;
    a 202 response means "try again shortly", so we retry with backoff."""
    total_additions = 0
    total_deletions = 0

    for repo in repos:
        if not repo.get("defaultBranchRef"):
            continue  # empty repo, nothing to count
        url = f"{REST_ROOT}/repos/{username}/{repo['name']}/stats/contributors"
        for attempt in range(5):
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 202:
                time.sleep(2 + attempt * 2)
                continue
            resp.raise_for_status()
            break
        else:
            print(f"WARNING: stats never became ready for {repo['name']}, skipping", file=sys.stderr)
            continue

        if not resp.text.strip():
            # Some repos (very small or just-created) return an empty body
            # instead of a proper 202/200 with data. Nothing to count.
            continue

        try:
            stats = resp.json()
        except ValueError:
            print(f"WARNING: non-JSON stats response for {repo['name']}, skipping", file=sys.stderr)
            continue

        for contributor in stats:
            if contributor.get("author") and contributor["author"].get("login") == username:
                for week in contributor.get("weeks", []):
                    total_additions += week.get("a", 0)
                    total_deletions += week.get("d", 0)

    return total_additions, total_deletions


def format_number(n):
    return f"{n:,}"


def render_svg(template_name, output_name, values):
    template_path = os.path.join(TEMPLATES_DIR, template_name)
    with open(template_path, "r", encoding="utf-8") as f:
        template = Template(f.read())
    svg = template.substitute(values)
    output_path = os.path.join(OUTPUT_DIR, output_name)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"Wrote {output_path}")


def main():
    created_at, followers, repo_count = get_account_created_at(GITHUB_USERNAME)
    commits, prs, issues, reviews = get_contribution_totals(GITHUB_USERNAME, created_at)
    repos = get_owned_repos(GITHUB_USERNAME)
    stars = sum(r["stargazerCount"] for r in repos)
    additions, deletions = get_loc_stats(GITHUB_USERNAME, repos)

    values = {
        "username": GITHUB_USERNAME,
        "stars": format_number(stars),
        "commits": format_number(commits),
        "prs": format_number(prs),
        "issues": format_number(issues),
        "reviews": format_number(reviews),
        "repos": format_number(repo_count),
        "followers": format_number(followers),
        "loc_added": format_number(additions),
        "loc_deleted": format_number(deletions),
        "loc_net": format_number(additions - deletions),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

    render_svg("dark_mode.svg", "dark_mode.svg", values)
    render_svg("light_mode.svg", "light_mode.svg", values)


if __name__ == "__main__":
    main()
