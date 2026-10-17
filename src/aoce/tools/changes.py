"""recent_changes and get_diff: what changed in the application repo around an incident (AOCE-28).

Most incidents follow a change, so the agent should ask "what shipped just before this started?" and,
when a change looks suspicious, read its diff. Both tools are read-only GET calls to the GitHub REST
API for settings.github_repo. github_token is optional for a public repo (60 requests/hour without
it); use a fine-grained token with read-only Contents and Deployments access.
"""

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from aoce.tools import Tool, ToolError

API_URL = "https://api.github.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
# A sha, branch or tag. No "..", so a ref cannot turn into a compare range or a path.
REF_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_.\-/]{1,200}$")
PATH_RE = re.compile(r"^(?!.*\.\.)[A-Za-z0-9_.\-/]{1,300}$")

MAX_CHANGES = 30
MAX_FILES = 20
PATCH_CHARS = 2000
TOTAL_PATCH_CHARS = 12000

RECENT_CHANGES_DESCRIPTION = """\
List what changed in the application repository in the window before an incident: commits, \
deployments and releases, newest first, each with its time, sha, author and link. Use it early to ask \
"what shipped just before this started?"; a change minutes before the first alert is the first \
suspect. Pass path to see only commits touching part of the repo (e.g. a service's directory or a \
config file). Follow up with get_diff on a suspicious sha."""

RECENT_CHANGES_SCHEMA = {
    "type": "object",
    "properties": {
        "window_minutes": {
            "type": "integer",
            "description": "How far back from `end` to look, 5-1440. 60 covers most incidents.",
        },
        "end": {
            "type": ["string", "null"],
            "description": "End of the window as an ISO-8601 timestamp (e.g. the alert time). Null for now.",
        },
        "path": {
            "type": ["string", "null"],
            "description": "Only commits touching this file or directory, e.g. 'src/product-catalog'. Null for all.",
        },
    },
    "required": ["window_minutes", "end", "path"],
    "additionalProperties": False,
}

GET_DIFF_DESCRIPTION = f"""\
Show the diff of one commit (base null) or of everything between two refs (base..ref), trimmed for \
reading: per changed file its status, line counts and patch, at most {MAX_FILES} files and \
{PATCH_CHARS} characters of patch per file. Use it after recent_changes to check whether a suspicious \
change could cause the symptom you see (a lowered timeout, a removed index, a changed flag default)."""

GET_DIFF_SCHEMA = {
    "type": "object",
    "properties": {
        "ref": {"type": "string", "description": "Commit sha (full or short), branch or tag to inspect."},
        "base": {
            "type": ["string", "null"],
            "description": "Compare base..ref instead, e.g. the previous release tag. Null for the single commit `ref`.",
        },
    },
    "required": ["ref", "base"],
    "additionalProperties": False,
}


def _parse_end(end: str | None) -> datetime:
    if not end:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"end is not an ISO-8601 timestamp: {end!r}") from e
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHub:
    def __init__(self, settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.repo = settings.github_repo
        self.token = settings.github_token
        self.transport = transport

    def check(self) -> str:
        if not self.repo:
            raise ToolError("change history is not configured (set AOCE_GITHUB_REPO)")
        if not REPO_RE.match(self.repo):
            raise ToolError(f"AOCE_GITHUB_REPO must look like owner/name, got {self.repo!r}")
        return self.repo

    async def get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"/repos/{self.repo}{path}"
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ToolError(f"GitHub unreachable: {e}") from e
        if resp.status_code != 200:
            try:
                message = resp.json().get("message", "")
            except ValueError:
                message = resp.text[:300]
            if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
                message = "rate limit exceeded; set AOCE_GITHUB_TOKEN"
            raise ToolError(f"GitHub error ({resp.status_code}) for {url}: {message}")
        return resp.json()

    def client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return httpx.AsyncClient(base_url=API_URL, headers=headers, timeout=15, transport=self.transport)


def _in_window(when: datetime | None, start: datetime, end: datetime) -> bool:
    return when is not None and start <= when <= end


def recent_changes_tool(settings, transport: httpx.AsyncBaseTransport | None = None) -> Tool:
    gh = GitHub(settings, transport)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        repo = gh.check()
        path = args.get("path") or None
        if path and not PATH_RE.match(path):
            raise ToolError(f"invalid path: {path!r}")
        try:
            minutes = min(max(int(args.get("window_minutes") or 60), 5), 1440)
        except (TypeError, ValueError) as e:
            raise ToolError(f"window_minutes must be an integer: {args.get('window_minutes')!r}") from e
        end = _parse_end(args.get("end"))
        start = end - timedelta(minutes=minutes)

        commit_params: dict[str, Any] = {"since": _iso(start), "until": _iso(end), "per_page": MAX_CHANGES}
        if path:
            commit_params["path"] = path
        async with gh.client() as client:
            if path:
                # Deployments and releases are repo-wide, so a path filter skips them.
                commits, deployments, releases = await gh.get(client, "/commits", commit_params), [], []
            else:
                # Deployments and releases have no time filter; the newest page covers any recent window.
                commits, deployments, releases = await asyncio.gather(
                    gh.get(client, "/commits", commit_params),
                    gh.get(client, "/deployments", {"per_page": MAX_CHANGES}),
                    gh.get(client, "/releases", {"per_page": 10}),
                )

        changes: list[dict[str, Any]] = []
        for c in commits:
            commit = c.get("commit", {})
            when = _parse_time((commit.get("committer") or {}).get("date"))
            changes.append(
                {
                    "kind": "commit",
                    "time": _iso(when) if when else None,
                    "sha": c["sha"][:12],
                    "title": (commit.get("message") or "").splitlines()[0][:200] if commit.get("message") else "",
                    "author": (c.get("author") or {}).get("login") or (commit.get("author") or {}).get("name"),
                    "url": c.get("html_url"),
                }
            )
        for d in deployments:
            when = _parse_time(d.get("created_at"))
            if _in_window(when, start, end):
                changes.append(
                    {
                        "kind": "deployment",
                        "time": _iso(when),
                        "sha": (d.get("sha") or "")[:12],
                        "title": f"deploy {d.get('ref')} to {d.get('environment')}"
                        + (f": {d['description']}" if d.get("description") else ""),
                        "author": (d.get("creator") or {}).get("login"),
                        "url": d.get("url"),
                    }
                )
        for r in releases:
            when = _parse_time(r.get("published_at"))
            if _in_window(when, start, end) and not r.get("draft"):
                changes.append(
                    {
                        "kind": "release",
                        "time": _iso(when),
                        "sha": r.get("tag_name"),
                        "title": r.get("name") or r.get("tag_name"),
                        "author": (r.get("author") or {}).get("login"),
                        "url": r.get("html_url"),
                    }
                )
        changes.sort(key=lambda ch: ch["time"] or "", reverse=True)
        return {
            "repo": repo,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "changes": changes[:MAX_CHANGES],
            "changes_truncated": max(0, len(changes) - MAX_CHANGES),
            # One page of commits; a full page means older commits in the window were not fetched.
            "more_commits": len(commits) >= MAX_CHANGES,
            "no_changes": not changes,
            "evidence": {
                "commits_url": f"https://github.com/{repo}/commits?since={_iso(start)}&until={_iso(end)}"
                + (f"&path={path}" if path else ""),
                "api": f"GET /repos/{repo}/commits since={_iso(start)} until={_iso(end)}"
                + (f" path={path}" if path else "")
                + ("" if path else f"; GET /repos/{repo}/deployments; GET /repos/{repo}/releases"),
            },
        }

    return Tool("recent_changes", RECENT_CHANGES_DESCRIPTION, RECENT_CHANGES_SCHEMA, handler)


def trim_files(files: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep the first MAX_FILES files with patches cut to PATCH_CHARS each and TOTAL_PATCH_CHARS overall."""
    budget = TOTAL_PATCH_CHARS
    out = []
    for f in files[:MAX_FILES]:
        patch = f.get("patch")
        entry = {
            "file": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
        }
        if f.get("previous_filename"):
            entry["previous_file"] = f["previous_filename"]
        if patch is None:
            entry["patch"] = None  # binary or too large for GitHub to inline
        else:
            keep = min(len(patch), PATCH_CHARS, budget)
            entry["patch"] = patch[:keep]
            entry["patch_truncated"] = keep < len(patch)
            budget -= keep
        out.append(entry)
    return out, max(0, len(files) - MAX_FILES)


def get_diff_tool(settings, transport: httpx.AsyncBaseTransport | None = None) -> Tool:
    gh = GitHub(settings, transport)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        repo = gh.check()
        ref = (args.get("ref") or "").strip()
        base = (args.get("base") or "").strip() or None
        for name, value in (("ref", ref), ("base", base)):
            if value is not None and not REF_RE.match(value):
                raise ToolError(f"invalid {name}: {value!r}")

        async with gh.client() as client:
            if base:
                body = await gh.get(client, f"/compare/{base}...{ref}")
                commits = body.get("commits") or []
                summary = {
                    "base": base,
                    "ref": ref,
                    "ahead_by": body.get("ahead_by"),
                    "commits": [
                        {"sha": c["sha"][:12], "title": (c.get("commit", {}).get("message") or "").splitlines()[0][:200]}
                        for c in commits[-MAX_FILES:]
                    ],
                }
                url = body.get("html_url") or f"https://github.com/{repo}/compare/{base}...{ref}"
                api = f"GET /repos/{repo}/compare/{base}...{ref}"
            else:
                body = await gh.get(client, f"/commits/{ref}")
                commit = body.get("commit", {})
                summary = {
                    "sha": body.get("sha", "")[:12],
                    "message": (commit.get("message") or "")[:1000],
                    "author": (body.get("author") or {}).get("login") or (commit.get("author") or {}).get("name"),
                    "time": (commit.get("committer") or {}).get("date"),
                }
                url = body.get("html_url") or f"https://github.com/{repo}/commit/{ref}"
                api = f"GET /repos/{repo}/commits/{ref}"

        files, files_truncated = trim_files(body.get("files") or [])
        return {
            "repo": repo,
            **summary,
            "stats": body.get("stats"),
            "files": files,
            "files_truncated": files_truncated,
            "evidence": {"url": url, "api": api},
        }

    return Tool("get_diff", GET_DIFF_DESCRIPTION, GET_DIFF_SCHEMA, handler)
