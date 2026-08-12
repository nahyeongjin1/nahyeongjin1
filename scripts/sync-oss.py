#!/usr/bin/env python3
"""Regenerate the Open Source section of README.md from the GitHub API.

Discovery is automatic; the wording is curated in scripts/oss.config.json.
Anything without curated copy falls back to a cleaned-up title and is listed
as needing copy, so new work shows up rather than being silently dropped.

Usage:
    scripts/sync-oss.py            # rewrite README.md in place
    scripts/sync-oss.py --check    # exit 1 if README.md is out of date
"""

from __future__ import annotations

import argparse
import json
import os
import re
import http.client
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "scripts" / "oss.config.json"
README = ROOT / "README.md"
START = "<!-- oss:start -->"
END = "<!-- oss:end -->"

# key, detail heading, detail column header, summary label.
# "Open" rather than "In Review": a PR sitting untouched for six months should
# not be advertised as actively under review.
SECTIONS = [
    ("merged", "Merged", "Contribution", "merged"),
    ("review", "Open", "Contribution", "open"),
    ("reported", "Reported", "Issue", "reported"),
]


def token() -> str:
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    found = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True
    )
    if found.returncode == 0 and found.stdout.strip():
        return found.stdout.strip()
    sys.exit("no GitHub token: set GITHUB_TOKEN or run `gh auth login`")


RETRYABLE = (http.client.IncompleteRead, urllib.error.URLError, TimeoutError)


def api(path: str, auth: str, attempts: int = 4) -> dict:
    """GET a JSON endpoint, retrying truncated reads and rate-limit responses."""
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {auth}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "sync-oss",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code not in (403, 429, 500, 502, 503) or attempt == attempts:
                sys.exit(
                    f"GET {path} failed: {error.code} {error.read().decode()[:200]}"
                )
        except RETRYABLE as error:
            if attempt == attempts:
                sys.exit(f"GET {path} failed after {attempts} attempts: {error}")
        delay = 2**attempt
        print(f"  retrying {path} in {delay}s ({attempt}/{attempts - 1})")
        time.sleep(delay)
    raise AssertionError("unreachable")


def search(query: str, auth: str) -> list[dict]:
    items: list[dict] = []
    for page in range(1, 11):
        encoded = urllib.parse.urlencode(
            {"q": query, "per_page": 100, "page": page, "sort": "created"}
        )
        payload = api(f"/search/issues?{encoded}", auth)
        items.extend(payload["items"])
        total = payload["total_count"]
        if total > 1000:
            sys.exit(
                f"search returned {total} results, above the API cap; "
                "tighten exclude_owners in oss.config.json"
            )
        if len(items) >= total or not payload["items"]:
            break
    return items


def clean_title(title: str) -> str:
    """Strip a conventional-commit prefix and sentence-case the remainder."""
    stripped = re.sub(r"^\w+(\([^)]*\))?!?:\s*", "", title)
    return stripped[:1].upper() + stripped[1:] if stripped else title


def collect(config: dict, auth: str) -> tuple[dict[str, dict], list[str]]:
    author = config["author"]
    owners = {o.lower() for o in config["exclude_owners"]}
    repos = {r.lower() for r in config["exclude_repos"]}
    items = {i.lower() for i in config["exclude_items"]}
    descriptions = config["descriptions"]

    buckets: dict[str, dict[str, list[dict]]] = {s[0]: {} for s in SECTIONS}
    missing: list[str] = []

    # Exclude owners in the query as well as below: it keeps the result set far
    # under the search API's 1000-result cap, and costs fewer paged requests.
    scope = " ".join(f"-user:{o}" for o in config["exclude_owners"])
    raw = search(f"is:pr author:{author} {scope}", auth)
    raw += search(f"is:issue author:{author} {scope}", auth)

    for entry in raw:
        repo = entry["repository_url"].split("/repos/", 1)[1]
        key = f"{repo}#{entry['number']}"
        if repo.split("/")[0].lower() in owners:
            continue
        if repo.lower() in repos or key.lower() in items:
            continue

        pull = entry.get("pull_request")
        if pull:
            if pull.get("merged_at"):
                section = "merged"
            elif entry["state"] == "open":
                section = "review"
            else:
                continue  # closed without merging: not a contribution
        else:
            section = "reported"

        text = descriptions.get(key)
        if not text:
            text = clean_title(entry["title"])
            missing.append(f"{key} — {text}")

        buckets[section].setdefault(repo, []).append(
            {"number": entry["number"], "url": entry["html_url"], "text": text}
        )

    return buckets, sorted(missing)


def stars_label(count: int) -> str:
    if count >= 10000:
        return f"{round(count / 1000)}k"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def render(buckets: dict[str, dict], stars: dict[str, int]) -> str:
    """One summary row per project, with the per-item tables folded away.

    Row count then tracks the number of projects, which grows slowly, rather
    than the number of contributions, which does not.
    """
    def rank(repo: str) -> int:
        """Best status a project reached, so merged work outranks a bug report."""
        return min(i for i, s in enumerate(SECTIONS) if repo in buckets[s[0]])

    order = sorted(
        {r for projects in buckets.values() for r in projects},
        key=lambda r: (rank(r), -stars.get(r, 0), r),
    )
    total = sum(len(e) for p in buckets.values() for e in p.values())

    lines = ["| Project | ★ | Contributions |", "| --- | --: | --- |"]
    for repo in order:
        counts = [
            f"{len(buckets[key][repo])} {label}"
            for key, _, _, label in SECTIONS
            if repo in buckets[key]
        ]
        lines.append(
            f"| **[{repo}](https://github.com/{repo})** "
            f"| {stars_label(stars.get(repo, 0))} "
            f"| {' · '.join(counts)} |"
        )

    lines += ["", "<details>", f"  <summary>All {total} contributions</summary>"]
    lines += ["  <div markdown=\"1\">", ""]
    for key, heading, column, _ in SECTIONS:
        projects = buckets[key]
        if not projects:
            continue
        lines += [f"**{heading}**", "", f"| Project | {column} |", "| --- | --- |"]
        for repo in [r for r in order if r in projects]:
            label = f"**[{repo}](https://github.com/{repo})**"
            for entry in sorted(projects[repo], key=lambda e: -e["number"]):
                cell = f"[#{entry['number']}]({entry['url']}) {entry['text']}"
                lines.append(f"| {label} | {cell} |")
                label = ""
        lines.append("")
    lines += ["  </div>", "</details>"]
    return "\n".join(lines)


def splice(body: str) -> str:
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        sys.exit(f"README.md is missing the {START} / {END} markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n\n{body}\n\n{END}{tail}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    auth = token()
    buckets, missing = collect(config, auth)

    repos = {r for projects in buckets.values() for r in projects}
    stars = {r: api(f"/repos/{r}", auth).get("stargazers_count", 0) for r in repos}

    counts = {s[0]: sum(len(v) for v in buckets[s[0]].values()) for s in SECTIONS}
    print(
        f"{len(repos)} projects · "
        + " · ".join(f"{n} {k}" for k, n in counts.items() if n)
    )

    if missing:
        report = "\n".join(f"  - {m}" for m in missing)
        print(f"{len(missing)} item(s) need curated copy in oss.config.json:")
        print(report)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"### Needs curated copy\n\n{report}\n")

    updated = splice(render(buckets, stars))
    if updated == README.read_text(encoding="utf-8"):
        print("README.md is up to date")
        return 0
    if args.check:
        print("README.md is out of date; run scripts/sync-oss.py")
        return 1
    README.write_text(updated, encoding="utf-8")
    print("README.md updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
