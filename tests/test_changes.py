import json
from datetime import UTC, datetime

import httpx
import pytest

from aoce.config import Settings
from aoce.tools import ToolRegistry
from aoce.tools.changes import MAX_FILES, PATCH_CHARS, TOTAL_PATCH_CHARS, get_diff_tool, recent_changes_tool

END = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
REPO = "acme/shop"


def commit(sha: str, at: str, message: str, login: str = "dev") -> dict:
    return {
        "sha": sha,
        "html_url": f"https://github.com/{REPO}/commit/{sha}",
        "author": {"login": login},
        "commit": {"message": message, "author": {"name": login}, "committer": {"date": at}},
    }


def fake_github(routes: dict[str, object]):
    """MockTransport answering GET paths under /repos/acme/shop; records every request."""
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix(f"/repos/{REPO}")
        body = routes.get(path)
        if body is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    return httpx.MockTransport(respond), seen


def registry(routes, **settings) -> tuple[ToolRegistry, list[httpx.Request]]:
    transport, seen = fake_github(routes)
    s = Settings(github_repo=REPO, github_token="t0ken", **settings)
    return ToolRegistry([recent_changes_tool(s, transport), get_diff_tool(s, transport)]), seen


def changes_args(**overrides):
    return {"window_minutes": 60, "end": END.isoformat(), "path": None} | overrides


ROUTES = {
    "/commits": [
        commit("a" * 40, "2026-10-13T08:50:00Z", "Lower product-catalog DB timeout\n\nlong body"),
        commit("b" * 40, "2026-10-13T08:10:00Z", "Bump frontend"),
    ],
    "/deployments": [
        {"sha": "a" * 40, "ref": "main", "environment": "prod", "created_at": "2026-10-13T08:55:00Z",
         "creator": {"login": "ci"}, "url": "https://api.github.com/x", "description": "rollout"},
        {"sha": "c" * 40, "ref": "main", "environment": "prod", "created_at": "2026-10-12T08:55:00Z"},
    ],
    "/releases": [
        {"tag_name": "v1.4.0", "name": "1.4.0", "published_at": "2026-10-13T08:56:00Z", "draft": False,
         "html_url": "https://github.com/acme/shop/releases/v1.4.0", "author": {"login": "ci"}},
        {"tag_name": "v1.5.0-rc", "published_at": "2026-10-13T08:57:00Z", "draft": True},
    ],
}


async def test_recent_changes_merges_commits_deploys_and_releases_newest_first():
    reg, seen = registry(ROUTES)
    content, is_error = await reg.run("recent_changes", changes_args())

    assert not is_error, content
    out = json.loads(content)
    kinds = [(c["kind"], c["time"]) for c in out["changes"]]
    assert kinds == [
        ("release", "2026-10-13T08:56:00Z"),
        ("deployment", "2026-10-13T08:55:00Z"),
        ("commit", "2026-10-13T08:50:00Z"),
        ("commit", "2026-10-13T08:10:00Z"),
    ]
    assert out["changes"][2]["title"] == "Lower product-catalog DB timeout"
    assert out["changes"][2]["sha"] == "a" * 12
    assert "deploy main to prod: rollout" == out["changes"][1]["title"]
    params = seen[0].url.params
    assert params["since"] == "2026-10-13T08:00:00Z" and params["until"] == "2026-10-13T09:00:00Z"
    assert seen[0].headers["authorization"] == "Bearer t0ken"
    assert all(r.method == "GET" for r in seen)
    assert "commits?since=" in out["evidence"]["commits_url"]


async def test_path_filter_only_lists_commits():
    reg, seen = registry(ROUTES)
    out = json.loads((await reg.run("recent_changes", changes_args(path="src/product-catalog")))[0])
    assert seen[0].url.params["path"] == "src/product-catalog"
    assert {c["kind"] for c in out["changes"]} == {"commit"}
    assert len(seen) == 1  # no deployment or release calls spent on a path query


async def test_a_full_page_of_commits_is_flagged():
    commits = [commit(f"{i:040d}", "2026-10-13T08:50:00Z", f"c{i}") for i in range(30)]
    reg, _ = registry({**ROUTES, "/commits": commits})
    out = json.loads((await reg.run("recent_changes", changes_args()))[0])
    assert out["more_commits"] is True
    out = json.loads((await registry(ROUTES)[0].run("recent_changes", changes_args()))[0])
    assert out["more_commits"] is False


async def test_no_token_sends_no_auth_header():
    transport, seen = fake_github(ROUTES)
    tool = recent_changes_tool(Settings(github_repo=REPO, github_token=None), transport)
    await ToolRegistry([tool]).run("recent_changes", changes_args())
    assert "authorization" not in seen[0].headers


async def test_get_diff_of_one_commit_trims_patches():
    files = [{"filename": f"f{i}.py", "status": "modified", "additions": 1, "deletions": 1, "patch": "x" * 5000}
             for i in range(MAX_FILES + 5)]
    files[1] = {"filename": "logo.png", "status": "added", "additions": 0, "deletions": 0}
    routes = {f"/commits/{'a' * 7}": {
        "sha": "a" * 40, "html_url": "https://github.com/acme/shop/commit/aaa",
        "commit": {"message": "Lower timeout", "committer": {"date": "2026-10-13T08:50:00Z"}},
        "author": {"login": "dev"}, "stats": {"total": 50}, "files": files,
    }}
    reg, seen = registry(routes)
    content, is_error = await reg.run("get_diff", {"ref": "a" * 7, "base": None})

    assert not is_error, content
    out = json.loads(content)
    assert out["sha"] == "a" * 12 and out["author"] == "dev"
    assert len(out["files"]) == MAX_FILES and out["files_truncated"] == 5
    assert len(out["files"][0]["patch"]) == PATCH_CHARS and out["files"][0]["patch_truncated"]
    assert out["files"][1]["patch"] is None
    assert sum(len(f["patch"] or "") for f in out["files"]) <= TOTAL_PATCH_CHARS
    assert out["evidence"]["url"] == "https://github.com/acme/shop/commit/aaa"


async def test_get_diff_compares_two_refs():
    routes = {"/compare/v1.3.0...v1.4.0": {
        "ahead_by": 2, "html_url": "https://github.com/acme/shop/compare/v1.3.0...v1.4.0",
        "commits": [commit("b" * 40, "2026-10-13T08:40:00Z", ""), commit("a" * 40, "2026-10-13T08:50:00Z", "Lower timeout")],
        "files": [{"filename": "config.yaml", "status": "modified", "additions": 1, "deletions": 1,
                   "patch": "-timeout: 5s\n+timeout: 50ms"}],
    }}
    reg, _ = registry(routes)
    out = json.loads((await reg.run("get_diff", {"ref": "v1.4.0", "base": "v1.3.0"}))[0])
    assert out["ahead_by"] == 2 and [c["title"] for c in out["commits"]] == ["", "Lower timeout"]
    assert out["files"][0]["patch"].endswith("+timeout: 50ms")
    assert out["files"][0]["patch_truncated"] is False


@pytest.mark.parametrize(
    "tool, tool_args, message",
    [
        ("get_diff", {"ref": "main..evil", "base": None}, "invalid ref"),
        ("get_diff", {"ref": "abc", "base": "x y"}, "invalid base"),
        ("get_diff", {"ref": "a//b", "base": None}, "invalid ref"),
        ("get_diff", {"ref": "/x", "base": None}, "invalid ref"),
        ("get_diff", {"ref": "main/", "base": None}, "invalid ref"),
        ("get_diff", {"ref": ".", "base": None}, "invalid ref"),
        ("get_diff", {"ref": "a/./b", "base": None}, "invalid ref"),
        ("get_diff", {"ref": "v1.4.0", "base": "v1."}, "invalid base"),
        ("recent_changes", changes_args(path="../etc"), "invalid path"),
        ("recent_changes", changes_args(end="yesterday"), "not an ISO-8601"),
        ("recent_changes", changes_args(window_minutes="abc"), "must be an integer"),
    ],
)
async def test_bad_input_is_reported_without_calling_github(tool, tool_args, message):
    reg, seen = registry(ROUTES)
    content, is_error = await reg.run(tool, tool_args)
    assert is_error and message in content
    assert not seen


async def test_unconfigured_repo_is_a_tool_error():
    tool = recent_changes_tool(Settings(github_repo=None))
    content, is_error = await ToolRegistry([tool]).run("recent_changes", changes_args())
    assert is_error and "AOCE_GITHUB_REPO" in content


async def test_github_errors_are_tool_errors():
    limited = httpx.Response(403, json={"message": "API rate limit exceeded"}, headers={"x-ratelimit-remaining": "0"})
    reg, _ = registry({"/commits": limited})
    content, is_error = await reg.run("recent_changes", changes_args())
    assert is_error and "rate limit" in content and "AOCE_GITHUB_TOKEN" in content

    reg, _ = registry({})
    content, is_error = await reg.run("get_diff", {"ref": "deadbeef", "base": None})
    assert is_error and "404" in content and "Not Found" in content


async def test_unreachable_github_is_a_tool_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    tool = get_diff_tool(Settings(github_repo=REPO), httpx.MockTransport(boom))
    content, is_error = await ToolRegistry([tool]).run("get_diff", {"ref": "abc", "base": None})
    assert is_error and "unreachable" in content


def test_definitions_are_strict():
    reg, _ = registry(ROUTES)
    assert [d["name"] for d in reg.definitions()] == ["recent_changes", "get_diff"]
    assert all(d["strict"] is True for d in reg.definitions())
