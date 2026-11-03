"""Minimal GitHub REST client for the governance gates (standard library only).

Failures are never hidden: any HTTP error, malformed JSON or broken
pagination raises ApiError, so callers can fail closed. A paginated listing is
returned only when every page was read successfully.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
MAX_PAGES = 50


class ApiError(Exception):
    """A GitHub API call failed or returned unusable data."""


class GitHubClient:
    def __init__(self, token: str, repository: str, api_url: str = "https://api.github.com") -> None:
        if not token:
            raise ApiError("missing token")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or ""):
            raise ApiError("invalid repository name")
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(
            os.environ.get("GITHUB_TOKEN", ""),
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )

    def _url(self, path: str, params: dict | None = None) -> str:
        if not path.startswith("/"):
            raise ApiError("path must be absolute")
        url = self.api_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    def _request(self, method: str, url: str, body: dict | None = None):
        if not url.startswith(self.api_url + "/"):
            raise ApiError("refusing to follow a link outside the API base URL")
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "linux-health-reporter-governance")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                link = response.headers.get("Link", "")
                status = response.status
        except urllib.error.HTTPError as exc:
            return exc.code, None, ""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError("network error") from exc
        try:
            decoded = json.loads(payload) if payload else None
        except ValueError as exc:
            raise ApiError("invalid JSON") from exc
        return status, decoded, link

    def get(self, path: str, params: dict | None = None):
        status, data, _ = self._request("GET", self._url(path, params))
        if status != 200:
            raise ApiError(f"GET {path} returned {status}")
        return data

    def get_optional(self, path: str, params: dict | None = None):
        """Like get(), but a 404 returns None instead of raising."""
        status, data, _ = self._request("GET", self._url(path, params))
        if status == 404:
            return None
        if status != 200:
            raise ApiError(f"GET {path} returned {status}")
        return data

    def get_all(self, path: str, params: dict | None = None, key: str | None = None) -> list:
        """Every item of a paginated endpoint, or ApiError.

        With ``key``, each page is an object and the items are ``page[key]``
        (for example ``check_runs`` or ``workflow_runs``).
        """
        query = dict(params or {})
        query.setdefault("per_page", 100)
        url = self._url(path, query)
        items: list = []
        for _ in range(MAX_PAGES):
            status, data, link = self._request("GET", url)
            if status != 200:
                raise ApiError(f"GET {path} page returned {status}")
            if key is not None:
                if not isinstance(data, dict) or not isinstance(data.get(key), list):
                    raise ApiError(f"GET {path} page has no list {key!r}")
                data = data[key]
            if not isinstance(data, list):
                raise ApiError(f"GET {path} page is not a list")
            items.extend(data)
            match = _NEXT_LINK.search(link or "")
            if not match:
                return items
            url = match.group(1)
        raise ApiError(f"GET {path}: too many pages")

    def create_status(self, sha: str, state: str, context: str, description: str, target_url: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise ApiError("invalid sha")
        body = {
            "state": state,
            "context": context,
            "description": description[:140],
            "target_url": target_url,
        }
        status, _, _ = self._request("POST", self._url(f"/repos/{self.repository}/statuses/{sha}"), body)
        if status != 201:
            raise ApiError(f"POST status returned {status}")
