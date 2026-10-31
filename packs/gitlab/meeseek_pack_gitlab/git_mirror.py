"""Host-side shallow mirrors of the allowlisted GitLab repos.

Code reads (tools/code_read.py) go through them. Keeping a bare mirror per repo
means a commit already fetched costs nothing, and a branch that moved costs only
the objects that changed — instead of a GitLab API call per file.

The GitLab token reaches git through GIT_CONFIG_* environment variables, so it
is in neither argv nor any file on disk.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

from meeseek_pack_kit import data_dir, secret

from meeseek_pack_gitlab.settings import settings


def mirrors_dir() -> Path:
    return data_dir() / "git"


_FETCH_TIMEOUT = 300
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Ref:
    project: str
    sha: str
    fetch: str          # what to fetch: a branch, refs/merge-requests/<iid>/head, or a sha
    desc: str           # "MR !165 (title)" / "branch main"
    default_branch: bool


def _lock(project: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project, threading.Lock())


def _env() -> dict[str, str]:
    token = secret("GITLAB_TOKEN")
    auth = base64.b64encode(f"oauth2:{token}".encode()).decode()
    pairs = [("http.extraHeader", f"Authorization: Basic {auth}")]
    if settings().skip_ssl_verify:
        pairs.append(("http.sslVerify", "false"))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(len(pairs))}
    for i, (k, v) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    return env


def _git(repo: Path, *args: str, timeout: int = 60, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          timeout=timeout, env=env)


def mirror_path(project: str) -> Path:
    return mirrors_dir() / (re.sub(r"[^A-Za-z0-9_.-]+", "_", project) + ".git")


_RESOLVE_TTL = 30.0
_resolved: dict[tuple[str, str], tuple[float, Ref]] = {}


def resolve(project: str, ref: str) -> Ref:
    """resolve_uncached, remembered for 30 s: one review reads and searches the
    same ref many times in a row, and should see one commit while it does."""
    key = (project, ref.strip())
    hit = _resolved.get(key)
    if hit and time.monotonic() - hit[0] < _RESOLVE_TTL:
        return hit[1]
    r = resolve_uncached(project, ref)
    if len(_resolved) > 200:
        _resolved.clear()
    _resolved[key] = (time.monotonic(), r)
    return r


def resolve_uncached(project: str, ref: str) -> Ref:
    """An MR IID ("165", "!165"), a branch, or a commit sha → the commit to fetch.
    Empty means the project's default branch.

    Raises RuntimeError with what GitLab said when the ref does not exist.
    """
    from meeseek_pack_gitlab.gitlab import _gitlab_get

    pid = quote_plus(project)
    meta = _gitlab_get(f"projects/{pid}")
    default = meta.get("default_branch", "main") if isinstance(meta, dict) else "main"
    ref = ref.strip() or default
    if m := re.fullmatch(r"!?(\d+)", ref):
        iid = m.group(1)
        mr = _gitlab_get(f"projects/{pid}/merge_requests/{iid}")
        if not isinstance(mr, dict) or not mr.get("sha"):
            raise RuntimeError(f"Cannot resolve MR !{iid} in {project}: "
                               f"{mr.get('error', 'no head commit') if isinstance(mr, dict) else mr}")
        return Ref(project, mr["sha"], f"refs/merge-requests/{iid}/head",
                   f"MR !{iid} ({mr.get('title', '')})", False)
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        return Ref(project, ref, ref, f"commit {ref[:8]}", False)
    branch = _gitlab_get(f"projects/{pid}/repository/branches/{quote_plus(ref)}")
    if not isinstance(branch, dict) or not (branch.get("commit") or {}).get("id"):
        raise RuntimeError(f"Cannot resolve branch {ref!r} in {project}: "
                           f"{branch.get('error', 'not found') if isinstance(branch, dict) else branch}")
    return Ref(project, branch["commit"]["id"], ref, f"branch {ref}", ref == default)


def ensure(ref: Ref) -> Path:
    """The mirror, holding ref.sha — fetched shallowly only if it is not there yet."""
    repo = mirror_path(ref.project)
    with _lock(ref.project):
        if not repo.exists():
            repo.parent.mkdir(parents=True, exist_ok=True)
            init = subprocess.run(["git", "init", "-q", "--bare", str(repo)], capture_output=True)
            if init.returncode != 0:
                raise RuntimeError(f"git init failed: {init.stderr.decode(errors='replace')}")
        if _git(repo, "cat-file", "-e", f"{ref.sha}^{{commit}}").returncode == 0:
            return repo
        target = f"refs/sbx/{hashlib.sha1(ref.fetch.encode()).hexdigest()[:12]}"
        url = f"{settings().base_url}/{ref.project}.git"
        proc = _git(repo, "fetch", "-q", "--depth", "1", url, f"+{ref.fetch}:{target}",
                    timeout=_FETCH_TIMEOUT, env=_env())
        if proc.returncode != 0:
            raise RuntimeError(f"git fetch {ref.desc} from {ref.project} failed: "
                               f"{proc.stderr.decode(errors='replace').strip()[:400]}")
        if _git(repo, "cat-file", "-e", f"{ref.sha}^{{commit}}").returncode != 0:
            raise RuntimeError(f"{ref.desc} moved while it was being fetched; try again")
    return repo


def read(repo: Path, sha: str, path: str) -> bytes | None:
    """One file at `sha`, or None if the commit has no such file."""
    proc = _git(repo, "show", f"{sha}:{path}")
    return proc.stdout if proc.returncode == 0 else None
