#!/usr/bin/env python3
"""Safely register and launch the gated single-area BET mfclrtmb workflow."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
REPO = "PacificCommunity/ofp-sam-bet-yft-2026-single-area"
SOURCE_SHA = "5363029b509cacf902aef2866efdc04634c89045"
BRANCH = "mfclrtmb-mcmc-2026-08-12"
WORKFLOW_ID = "single-area-bet-mfclrtmb-mcmc-20260813"
RUNTIME_IMAGE = (
    "ghcr.io/pacificcommunity/tuna-flow@"
    "sha256:7b9dc95f535025a42109ac958c4faa3af96592cd19510ac0be15af4478eccf27"
)
REMOTE_HOST = "suvofpsubmit.corp.spc.int"
SLOT_REQUIREMENTS = (
    'regexp("^suvofp", Machine) && OpSys == "LINUX" && '
    'Arch == "X86_64" && (HasDocker =?= true)'
)
SPARSENUTS_SHA = "2f3f1626219afce68fa2da0d884d4f2dca138117"
SPARSENUTS_VERSION = "1.0.2"
STANESTIMATORS_SHA = "d19186c7079c6a08160bb77db5b577a203254bf1"
STANESTIMATORS_VERSION = "0.3.1"
INPUT_SHA256 = {
    "steps/BET/model/final.par": (
        "MCMC_FINAL_PAR_SHA256",
        "5bfab1a2a57a11e1e91a8da75abfb2e4cc83551bab52504fedaec63638f44e83",
    ),
    "steps/BET/model/bet.frq": (
        "MCMC_BET_FRQ_SHA256",
        "7982eab77ffd337e446c0ae3496c4b8f4a4c88aa3ff54d514cc80021288e6989",
    ),
    "steps/BET/model/bet.ini": (
        "MCMC_BET_INI_SHA256",
        "5ba4fe551494f8d8cc10d53d990355d3e6d9719de499e63d5c32191facc87ab5",
    ),
    "steps/BET/model/bet.tag.txt": (
        "MCMC_BET_TAG_SHA256",
        "e1007498fa893d475a5f39f81a00b1e764600875e142d7999548b2716b684a83",
    ),
    "steps/BET/model/bet.age_length": (
        "MCMC_BET_AGE_LENGTH_SHA256",
        "9118e61f99b7aaba097f4aa9f3ea31ab27419cd27a25781340f6263b41b3ec2f",
    ),
}
CONFIG = {
    "gate": ROOT / "kflow-mfclrtmb-eval-gate.yaml",
    "pilot": ROOT / "kflow-mfclrtmb-mcmc-chain.yaml",
    "chains": ROOT / "kflow-mfclrtmb-mcmc-chain.yaml",
}
SUCCESS_STATUSES = {"completed", "success"}
GATE_ARTIFACTS = (
    "outputs/mfclrtmb-eval-gate/gate-result.csv",
    "outputs/mfclrtmb-eval-gate/gate-details.rds",
    "outputs/mfclrtmb-eval-gate/growth-gradient-parity.csv",
    "outputs/mfclrtmb-eval-gate/input-manifest.csv",
    "outputs/mfclrtmb-eval-gate/mfclrtmb-source-provenance.txt",
)
PILOT_ARTIFACTS = (
    "outputs/mfclrtmb-mcmc-pilot/pilot/pilot-result.csv",
    "outputs/mfclrtmb-mcmc-pilot/pilot/chain-status.csv",
    "outputs/mfclrtmb-mcmc-pilot/pilot/mcmc-run-settings.csv",
    "outputs/mfclrtmb-mcmc-pilot/pilot/mcmc/draws/parameter-draws.rds",
    "outputs/mfclrtmb-mcmc-pilot/pilot/mfclrtmb-source-provenance.txt",
    "outputs/mfclrtmb-mcmc-pilot/pilot/sampler-package-provenance.csv",
    "outputs/mfclrtmb-mcmc-pilot/pilot/chain-provenance.rds",
)
DEFAULT_STATE = (
    Path.home()
    / ".local/state/ofp-sam-bet-yft-2026-single-area"
    / "mfclrtmb-mcmc-20260813.json"
)
SERVER_REPO_METADATA_KEYS = {
    "repo_private",
    "repo_owner_login",
    "repo_owner_type",
    "repo_owner_avatar_url",
}
SERVER_JOB_ENV_KEYS = {
    "KFLOW_CALLBACK_TOKEN",
    "KFLOW_CALLBACK_URL",
}


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that refuses silent duplicate-key replacement."""


def construct_unique_mapping(
    loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise RuntimeError("Kflow YAML contains an unhashable mapping key") from error
        if duplicate:
            raise RuntimeError(
                f"Kflow YAML contains duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_unique_mapping
)


class KflowHTTPError(RuntimeError):
    """An HTTP response whose status is available to reconciliation code."""

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f"{method} {path} failed: HTTP {status}: {detail}")
        self.status = status


class Kflow:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise KflowHTTPError(method, path, error.code, detail) from error
        if not raw:
            return {}
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, (dict, list)):
            raise RuntimeError(f"{method} {path} returned an invalid JSON payload")
        return decoded


class DurableState:
    """A process lock plus an atomically replaced, fsync'd submission journal."""

    def __init__(self, path: Path, workflow_sha: str) -> None:
        self.path = path.expanduser().resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.workflow_sha = workflow_sha
        self.lock_handle: Any = None
        self.data: dict[str, Any] = {}

    def _close_lock(self) -> None:
        if self.lock_handle is not None:
            try:
                fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.lock_handle.close()
                self.lock_handle = None

    def __enter__(self) -> "DurableState":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_handle = self.lock_path.open("a+", encoding="utf-8")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock_handle.close()
            self.lock_handle = None
            raise RuntimeError(
                f"Another launcher holds the local state lock: {self.lock_path}"
            ) from error
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise RuntimeError(f"Durable state {self.path} is not a JSON object")
                self.data = loaded
            else:
                self.data = {
                    "schema_version": 1,
                    "workflow_id": WORKFLOW_ID,
                    "repo": REPO,
                    "branch": BRANCH,
                    "workflow_sha": self.workflow_sha,
                    "registrations": {},
                    "jobs": {},
                }
            expected = {
                "schema_version": 1,
                "workflow_id": WORKFLOW_ID,
                "repo": REPO,
                "branch": BRANCH,
                "workflow_sha": self.workflow_sha,
            }
            mismatches = {
                key: (self.data.get(key), value)
                for key, value in expected.items()
                if self.data.get(key) != value
            }
            if mismatches:
                raise RuntimeError(
                    f"Durable state belongs to another workflow snapshot: {mismatches}"
                )
        except (OSError, json.JSONDecodeError) as error:
            self._close_lock()
            raise RuntimeError(f"Cannot read durable state {self.path}: {error}") from error
        except Exception:
            self._close_lock()
            raise
        return self

    def save(self) -> None:
        self.data["updated_epoch"] = time.time()
        encoded = (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record_registration(self, name: str, status: str) -> None:
        self.data.setdefault("registrations", {})[name] = {
            "status": status,
            "recorded_epoch": time.time(),
        }
        self.save()

    def record_job(self, key: str, **values: Any) -> None:
        record = dict(self.data.setdefault("jobs", {}).get(key) or {})
        record.update(values)
        record["recorded_epoch"] = time.time()
        self.data["jobs"][key] = record
        self.save()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._close_lock()


def full_sha(value: str, name: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuntimeError(f"{name} must be a full 40-character commit SHA")
    return value


def validated_git_ref(value: str, name: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*", value):
        raise RuntimeError(
            f"{name} must be a full refs/heads/... or refs/tags/... ref; "
            "the exact commit belongs in --mfclrtmb-sha"
        )
    components = value.split("/")
    if (
        ".." in value
        or "@{" in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
        or any(component.startswith(".") for component in components)
    ):
        raise RuntimeError(f"{name} is not a safe canonical Git ref")
    return value


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def run_git(*arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def preflight_repository(workflow_sha: str, fix_ref: str, fix_sha: str) -> None:
    branch = run_git("branch", "--show-current")
    if branch != BRANCH:
        raise RuntimeError(f"Run from isolated branch {BRANCH}; found {branch or 'detached'}")
    dirty = run_git("status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise RuntimeError("The workflow worktree must be completely clean before submission")
    head = full_sha(run_git("rev-parse", "HEAD"), "local HEAD")
    if head != workflow_sha:
        raise RuntimeError(f"Local HEAD {head} does not match --workflow-sha {workflow_sha}")

    remote = run_git("ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}")
    remote_rows = [line.split() for line in remote.splitlines() if line.strip()]
    if len(remote_rows) != 1 or len(remote_rows[0]) != 2:
        raise RuntimeError(f"Remote branch refs/heads/{BRANCH} did not resolve uniquely")
    remote_sha, remote_ref = remote_rows[0]
    if remote_ref != f"refs/heads/{BRANCH}" or full_sha(remote_sha, "remote HEAD") != workflow_sha:
        raise RuntimeError("The remote workflow branch does not exactly match local HEAD")
    subprocess.run(
        ["git", "-C", str(ROOT), "merge-base", "--is-ancestor", SOURCE_SHA, workflow_sha],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    for relative, (_, expected) in INPUT_SHA256.items():
        path = ROOT / relative
        if not path.is_file() or sha256_path(path) != expected:
            raise RuntimeError(f"Frozen input hash mismatch: {relative}")
    subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--quiet", SOURCE_SHA, "--", *INPUT_SHA256],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    patterns = [fix_ref]
    if fix_ref.startswith("refs/tags/"):
        patterns.append(f"{fix_ref}^{{}}")
    remote_fix = run_git(
        "ls-remote",
        "--exit-code",
        "https://github.com/PacificCommunity/ofp-sam-mfclrtmb.git",
        *patterns,
    )
    rows = {
        row[1]: full_sha(row[0], "remote mfclrtmb SHA")
        for row in (line.split() for line in remote_fix.splitlines() if line.strip())
        if len(row) == 2
    }
    resolved = rows.get(f"{fix_ref}^{{}}", rows.get(fix_ref, ""))
    if fix_ref not in rows or resolved != fix_sha:
        raise RuntimeError(
            f"MFCLRTMB_FIX_REF {fix_ref} does not resolve exactly to {fix_sha}"
        )


def read_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = yaml.load(handle, Loader=UniqueKeyLoader) or {}
    if not isinstance(config, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    resources = config.get("resources") if isinstance(config.get("resources"), dict) else {}
    checks = {
        "triggers": config.get("triggers") == {},
        "input_jobs": config.get("input_jobs") == [],
        "runtime image": config.get("docker_image") == RUNTIME_IMAGE,
        "canonical Suva host": config.get("remote_host") == REMOTE_HOST,
        "Linux/X86_64/Docker slot": config.get("slot_requirements") == SLOT_REQUIREMENTS,
        "2 CPUs": resources.get("cpus") == 2,
        "16 GB memory": resources.get("memory") == "16GB",
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Unsafe Kflow config {path}: {', '.join(failed)}")
    return config


def task_code(config: Mapping[str, Any], mode: str) -> str:
    value = config.get("pilot_name") if mode == "pilot" else config.get("name")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"Kflow config has no task code for {mode}")
    return value.strip()


def task_payload(
    config: dict[str, Any], branch: str, registration_name: str
) -> dict[str, Any]:
    resources = config.get("resources") or {}
    return {
        "name": registration_name,
        "description": config.get("description", ""),
        "repo_full_name": REPO,
        "branch": branch,
        "make_target": config.get("make_target", "all"),
        "command": config.get("command"),
        "target_folder": config.get("target_folder", ""),
        "checkout": config.get("checkout", {"mode": "full", "paths": []}),
        "docker_image": config.get("docker_image"),
        "remote_user": config.get("remote_user"),
        "remote_host": config.get("remote_host"),
        "remote_base_dir": config.get("remote_base_dir"),
        "slot_requirements": config.get("slot_requirements", ""),
        "cpus": resources.get("cpus"),
        "memory": resources.get("memory"),
        "disk": resources.get("disk"),
        "stream_error": config.get("stream_error", True),
        "ghcr_login": config.get("ghcr_login", False),
        "exclude_machines": config.get("exclude_machines", []),
        "exclude_slots": config.get("exclude_slots", []),
        "env": config.get("env", {}),
        "tags": config.get("tags", {}),
        "metadata": config.get("metadata", {}),
        "output_patterns": config.get("output_patterns", []),
        "artifacts": config.get("artifacts", []),
        "input_jobs": [],
        "triggers": {},
    }


def normalized_report(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": report.get("name"),
        "description": report.get("description", ""),
        "repo_full_name": report.get("repo_full_name") or report.get("repo"),
        "branch": report.get("branch"),
        "make_target": report.get("make_target") or report.get("make") or "all",
        "command": report.get("command"),
        "target_folder": report.get("target_folder", ""),
        "checkout": report.get("checkout") or {"mode": "full", "paths": []},
        "docker_image": report.get("docker_image"),
        "remote_user": report.get("remote_user"),
        "remote_host": report.get("remote_host"),
        "remote_base_dir": report.get("remote_base_dir"),
        "slot_requirements": report.get("slot_requirements", ""),
        "cpus": report.get("cpus"),
        "memory": report.get("memory"),
        "disk": report.get("disk"),
        "stream_error": bool(report.get("stream_error", True)),
        "ghcr_login": bool(report.get("ghcr_login", False)),
        "exclude_machines": report.get("exclude_machines") or [],
        "exclude_slots": report.get("exclude_slots") or [],
        "env": report.get("env") or {},
        "tags": report.get("tags") or {},
        "metadata": report.get("metadata") or {},
        "output_patterns": report.get("output_patterns") or [],
        "artifacts": report.get("artifacts") or [],
        "input_jobs": report.get("input_jobs") or [],
        "triggers": report.get("triggers") or {},
    }


def exact_contract_mapping(
    actual: Any,
    expected: Mapping[str, Any],
    label: str,
    allowed_server_keys: set[str] | None = None,
) -> None:
    actual_map = dict(nested_mapping(actual))
    expected_map = dict(expected)
    for key in allowed_server_keys or set():
        if key in expected_map:
            raise RuntimeError(f"{label} attempts to set server-owned key {key}")
        actual_map.pop(key, None)
    if actual_map != expected_map:
        missing = sorted(set(expected_map) - set(actual_map))
        extra = sorted(set(actual_map) - set(expected_map))
        changed = {
            key: (actual_map[key], expected_map[key])
            for key in sorted(set(actual_map) & set(expected_map))
            if actual_map[key] != expected_map[key]
        }
        raise RuntimeError(
            f"{label} is not exactly equal to the immutable contract: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )


def compare_registration(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    wanted = normalized_report(expected)
    found = normalized_report(actual)
    mismatches: dict[str, tuple[Any, Any]] = {}
    for key, value in wanted.items():
        if key != "metadata" and found[key] != value:
            mismatches[key] = (found[key], value)
    if mismatches:
        raise RuntimeError(
            "Existing Kflow task differs from the immutable registration; "
            f"refusing to overwrite: {mismatches}"
        )
    try:
        exact_contract_mapping(
            found["metadata"],
            wanted["metadata"],
            "task metadata",
            allowed_server_keys=SERVER_REPO_METADATA_KEYS,
        )
    except RuntimeError as error:
        raise RuntimeError(
            "Existing Kflow task differs from the immutable registration; "
            f"refusing to overwrite: {error}"
        ) from error


def get_report(api: Kflow, name: str) -> dict[str, Any] | None:
    try:
        response = api.request("GET", f"/api/report/{urllib.parse.quote(name, safe='')}")
    except KflowHTTPError as error:
        if error.status == 404:
            return None
        raise
    report = response.get("report", response) if isinstance(response, dict) else None
    if not isinstance(report, dict):
        raise RuntimeError(f"Kflow returned no task record for {name}")
    return report


def ensure_registration(
    api: Kflow, registration: dict[str, Any], state: DurableState
) -> str:
    name = str(registration["name"])
    existing = get_report(api, name)
    if existing is not None:
        compare_registration(registration, existing)
        state.record_registration(name, "reused-exact")
        return "reused-exact"

    prior = nested_mapping(state.data.get("registrations")).get(name)
    prior_status = str(nested_mapping(prior).get("status") or "")
    if prior_status in {
        "post-intent",
        "ambiguous-not-found",
        "reconciled-after-ambiguous-post",
        "registered-once",
        "reused-exact",
    }:
        raise RuntimeError(
            f"Durable state records a prior task-registration POST for {name} "
            f"(status={prior_status}), but Kflow GET returns 404. Refusing a second POST."
        )

    state.record_registration(name, "post-intent")
    try:
        response = api.request(
            "POST", f"/api/report/{urllib.parse.quote(name, safe='')}", registration
        )
    except Exception as error:
        # A timed-out POST may have committed. Never repeat it: reconcile by GET.
        reconciled = get_report(api, name)
        if reconciled is None:
            state.record_registration(name, "ambiguous-not-found")
            raise RuntimeError(
                f"Task registration outcome is ambiguous after {error}; POST was not retried"
            ) from error
        compare_registration(registration, reconciled)
        state.record_registration(name, "reconciled-after-ambiguous-post")
        return "reconciled-after-ambiguous-post"

    report = response.get("report", response) if isinstance(response, dict) else None
    if not isinstance(report, dict):
        raise RuntimeError(f"Kflow returned no task after registering {name}")
    compare_registration(registration, report)
    reread = get_report(api, name)
    if reread is None:
        raise RuntimeError(f"Registered Kflow task {name} disappeared during verification")
    compare_registration(registration, reread)
    state.record_registration(name, "registered-once")
    return "registered-once"


def response_job(response: Any) -> dict[str, Any]:
    job = response.get("job", response) if isinstance(response, dict) else None
    if not isinstance(job, dict):
        raise RuntimeError(f"Kflow response has no job: {response}")
    return job


def response_job_number(response: Any) -> str:
    job = response_job(response)
    value = job.get("job_number") or job.get("number") or job.get("id")
    if value in (None, ""):
        raise RuntimeError(f"Kflow response has no job number: {response}")
    return str(value)


def get_job(api: Kflow, reference: str) -> dict[str, Any]:
    clean = reference.strip().lstrip("#")
    response = api.request("GET", f"/api/job/{urllib.parse.quote(clean, safe='')}")
    return response_job(response)


def nested_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def job_key_value(job: Mapping[str, Any]) -> str:
    env = nested_mapping(job.get("env") or job.get("env_json"))
    metadata = nested_mapping(job.get("metadata") or job.get("metadata_json"))
    tags = nested_mapping(job.get("tags") or job.get("tags_json"))
    return str(
        env.get("JOB_KEY")
        or metadata.get("job_key")
        or tags.get("JOB_KEY")
        or job.get("job_key")
        or job.get("JOB_KEY")
        or ""
    )


def submission_key_value(job: Mapping[str, Any]) -> str:
    env = nested_mapping(job.get("env") or job.get("env_json"))
    metadata = nested_mapping(job.get("metadata") or job.get("metadata_json"))
    tags = nested_mapping(job.get("tags") or job.get("tags_json"))
    return str(
        metadata.get("submission_key")
        or env.get("KFLOW_SUBMISSION_KEY")
        or tags.get("submission_key")
        or job.get("submission_key")
        or ""
    )


def job_identity(job: Mapping[str, Any]) -> str:
    return str(job.get("id") or job.get("job_number") or canonical_sha256(job))


def paginated_jobs(
    api: Kflow,
    method: str,
    path_for_page: Any,
    payload: dict[str, Any] | None,
    label: str,
    max_pages: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_pages: set[tuple[str, ...]] = set()
    for page in range(1, max_pages + 1):
        response = api.request(method, path_for_page(page), payload)
        jobs = response.get("jobs", []) if isinstance(response, dict) else []
        if not isinstance(jobs, list):
            raise RuntimeError(f"Kflow {label} page {page} is not a list")
        if not jobs:
            return results
        if any(not isinstance(job, dict) for job in jobs):
            raise RuntimeError(f"Kflow returned a non-object job on {label} page {page}")
        identifiers = tuple(job_identity(job) for job in jobs)
        if identifiers in seen_pages:
            raise RuntimeError(
                f"Kflow pagination repeated {label} page {page}; reconciliation aborted"
            )
        seen_pages.add(identifiers)
        for job, identifier in zip(jobs, identifiers):
            if identifier in seen_ids:
                raise RuntimeError(
                    f"Kflow pagination repeated job {identifier} on {label}; "
                    "reconciliation aborted"
                )
            seen_ids.add(identifier)
            results.append(job)
    raise RuntimeError(f"Kflow pagination exceeded {max_pages} pages for {label}")


def list_jobs_by_tag(
    api: Kflow, task: str, tag: str, key: str, max_pages: int = 1000
) -> list[dict[str, Any]]:
    quoted = urllib.parse.quote(task, safe="")
    return paginated_jobs(
        api,
        "POST",
        lambda page: f"/api/jobs/{quoted}/?page={page}",
        {tag: key},
        f"{task} tag {tag}",
        max_pages,
    )


def list_jobs_unfiltered(
    api: Kflow, task: str, max_pages: int = 1000
) -> list[dict[str, Any]]:
    quoted = urllib.parse.quote(task, safe="")
    return paginated_jobs(
        api,
        "GET",
        lambda page: f"/api/jobs/{quoted}?page={page}",
        None,
        f"{task} unfiltered job list",
        max_pages,
    )


def list_jobs_by_key(
    api: Kflow, task: str, key: str, max_pages: int = 1000
) -> list[dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}
    for job in list_jobs_unfiltered(api, task, max_pages=max_pages):
        if job_key_value(job) != key and submission_key_value(job) != key:
            continue
        by_identity.setdefault(job_identity(job), job)
    for tag in ("JOB_KEY", "submission_key"):
        for job in list_jobs_by_tag(api, task, tag, key, max_pages=max_pages):
            if job_key_value(job) != key and submission_key_value(job) != key:
                raise RuntimeError(
                    f"Kflow tag query {tag}={key} returned a job with incompatible identity"
                )
            by_identity.setdefault(job_identity(job), job)
    return list(by_identity.values())


def metadata_subset(actual: Any, expected: Mapping[str, Any], label: str) -> None:
    actual_map = nested_mapping(actual)
    failed = {
        key: (actual_map.get(key), value)
        for key, value in expected.items()
        if actual_map.get(key) != value
    }
    if failed:
        raise RuntimeError(f"{label} mismatch: {failed}")


def report_spec_for_job(job: Mapping[str, Any]) -> Mapping[str, Any]:
    details = nested_mapping(job.get("details"))
    return nested_mapping(details.get("report_spec"))


def validate_reconciled_job(task: str, payload: Mapping[str, Any], job: Mapping[str, Any]) -> None:
    key = str(nested_mapping(payload.get("metadata")).get("submission_key") or "")
    checks = {
        "report_code": str(job.get("report_code") or "") == task,
        "repo": str(job.get("repo_full_name") or job.get("repo") or "") == REPO,
        "branch": job.get("branch") == payload.get("branch"),
        "make_target": job.get("make_target") == payload.get("make_target"),
        "command": job.get("command") == payload.get("command"),
        "target_folder": str(job.get("target_folder") or "")
        == str(payload.get("target_folder") or ""),
        "docker_image": job.get("docker_image") == payload.get("docker_image"),
        "remote_user": job.get("remote_user") == payload.get("remote_user"),
        "remote_host": job.get("remote_host") == payload.get("remote_host"),
        "remote_base_dir": str(job.get("remote_dir") or "").startswith(
            str(payload.get("remote_base_dir") or "").rstrip("/") + "/"
        ),
        "batch_name": job.get("batch_name") == payload.get("batch_name"),
        "cpus": int(job.get("cpus") or 0) == int(payload.get("cpus") or 0),
        "memory": str(job.get("memory") or "") == str(payload.get("memory") or ""),
        "disk": str(job.get("disk") or "") == str(payload.get("disk") or ""),
        "JOB_KEY": job_key_value(job) == key,
        "submission_key": submission_key_value(job) == key,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"Reconciled Kflow job does not match payload: {', '.join(failed)}")
    exact_contract_mapping(
        job.get("metadata"),
        nested_mapping(payload.get("metadata")),
        "job metadata",
        allowed_server_keys=SERVER_REPO_METADATA_KEYS,
    )
    exact_contract_mapping(
        job.get("env"),
        nested_mapping(payload.get("env")),
        "job environment",
        allowed_server_keys=SERVER_JOB_ENV_KEYS,
    )
    exact_contract_mapping(
        job.get("tags"), nested_mapping(payload.get("tags")), "job tags"
    )
    input_jobs = metadata_input_jobs(job)
    if input_jobs and input_jobs != list(payload.get("input_jobs") or []):
        raise RuntimeError(
            f"Reconciled Kflow job input dependencies mismatch: {input_jobs} "
            f"!= {payload.get('input_jobs')}"
        )
    spec = report_spec_for_job(job)
    if not spec:
        raise RuntimeError("Reconciled Kflow job has no immutable report specification")
    expected_spec = {
        "output_patterns": payload.get("output_patterns"),
        "input_jobs": payload.get("input_jobs"),
        "slot_requirements": payload.get("slot_requirements"),
        "checkout": payload.get("checkout"),
        "exclude_machines": payload.get("exclude_machines"),
        "exclude_slots": payload.get("exclude_slots"),
        "ghcr_login": payload.get("ghcr_login"),
        "triggers": payload.get("triggers"),
    }
    metadata_subset(spec, expected_spec, "job report specification")


def metadata_input_jobs(job: Mapping[str, Any]) -> list[str]:
    metadata = nested_mapping(job.get("metadata") or job.get("metadata_json"))
    value = metadata.get("expected_input_jobs") or []
    if not isinstance(value, list):
        raise RuntimeError("Kflow job expected_input_jobs metadata is not a list")
    return [str(item) for item in value]


def unique_match(matches: list[dict[str, Any]], task: str, key: str) -> dict[str, Any] | None:
    if len(matches) > 1:
        numbers = [str(job.get("job_number") or job.get("id") or "?") for job in matches]
        raise RuntimeError(
            f"Duplicate Kflow jobs exist for {task} JOB_KEY={key}: {', '.join(numbers)}"
        )
    return matches[0] if matches else None


def poll_for_submission(api: Kflow, task: str, key: str) -> dict[str, Any] | None:
    for attempt in range(6):
        match = unique_match(list_jobs_by_key(api, task, key), task, key)
        if match is not None:
            return match
        if attempt < 5:
            time.sleep(2)
    return None


def submit_or_reconcile(
    api: Kflow,
    task: str,
    payload: dict[str, Any],
    state: DurableState,
) -> tuple[str, str]:
    key = str(payload["metadata"]["submission_key"])
    existing = unique_match(list_jobs_by_key(api, task, key), task, key)
    if existing is not None:
        validate_reconciled_job(task, payload, existing)
        number = response_job_number(existing)
        state.record_job(key, status="reused-exact", task=task, job_number=number)
        return number, "reused-exact"

    prior = nested_mapping(state.data.get("jobs")).get(key)
    prior_status = str(nested_mapping(prior).get("status") or "")
    if prior_status in {
        "post-intent",
        "ambiguous-post",
        "post-returned-but-not-indexed",
        "reconciled-after-ambiguous-post",
        "submitted-once",
        "reused-exact",
    }:
        raise RuntimeError(
            f"Durable state records a prior POST for JOB_KEY={key} "
            f"(status={prior_status}), but Kflow does not return one unique match. "
            "Refusing a second POST; investigate Kflow/state consistency."
        )

    state.record_job(key, status="post-intent", task=task)
    try:
        response = api.request("POST", f"/api/job/{urllib.parse.quote(task, safe='')}", payload)
    except Exception as error:
        state.record_job(key, status="ambiguous-post", task=task, error=str(error))
        reconciled = poll_for_submission(api, task, key)
        if reconciled is None:
            raise RuntimeError(
                f"Job POST outcome is ambiguous for JOB_KEY={key}; it was not retried. "
                "Re-run only after Kflow is reachable so reconciliation can complete."
            ) from error
        validate_reconciled_job(task, payload, reconciled)
        number = response_job_number(reconciled)
        state.record_job(
            key, status="reconciled-after-ambiguous-post", task=task, job_number=number
        )
        return number, "reconciled-after-ambiguous-post"

    returned = response_job(response)
    validate_reconciled_job(task, payload, returned)
    returned_number = response_job_number(response)
    reconciled = poll_for_submission(api, task, key)
    if reconciled is None:
        state.record_job(key, status="post-returned-but-not-indexed", task=task)
        raise RuntimeError(
            f"Kflow returned job {returned_number}, but JOB_KEY={key} was not uniquely indexed; "
            "the POST was not retried"
        )
    validate_reconciled_job(task, payload, reconciled)
    number = response_job_number(reconciled)
    if number != returned_number:
        raise RuntimeError(
            f"Kflow POST returned {returned_number}, but reconciliation found {number}"
        )
    state.record_job(key, status="submitted-once", task=task, job_number=number)
    return number, "submitted-once"


def artifact_paths(job: Mapping[str, Any]) -> set[str]:
    details = nested_mapping(job.get("details"))
    artifacts = nested_mapping(details.get("artifacts"))
    files = artifacts.get("files") or []
    paths: set[str] = set()
    if isinstance(files, list):
        for item in files:
            raw = item.get("path") if isinstance(item, Mapping) else item
            if raw:
                paths.add(str(raw).replace("\\", "/").lstrip("./"))
    return paths


def require_artifacts(job: Mapping[str, Any], required: Iterable[str], label: str) -> None:
    paths = artifact_paths(job)
    missing = [
        expected
        for expected in required
        if not any(path == expected or path.endswith("/" + expected) for path in paths)
    ]
    if missing:
        raise RuntimeError(f"{label} is missing required Kflow artifacts: {', '.join(missing)}")


def common_provenance(workflow_sha: str, fix_ref: str, fix_sha: str) -> dict[str, Any]:
    return {
        "workflow_id": WORKFLOW_ID,
        "workflow_sha": workflow_sha,
        "single_area_model_source_sha": SOURCE_SHA,
        "mfclrtmb_fix_repo": "PacificCommunity/ofp-sam-mfclrtmb",
        "mfclrtmb_fix_ref": fix_ref,
        "mfclrtmb_fix_sha": fix_sha,
        "runtime_image": RUNTIME_IMAGE,
        "sparse_nuts_sha": SPARSENUTS_SHA,
        "sparse_nuts_version": SPARSENUTS_VERSION,
        "stan_estimators_sha": STANESTIMATORS_SHA,
        "stan_estimators_version": STANESTIMATORS_VERSION,
        "input_sha256": {path: digest for path, (_, digest) in INPUT_SHA256.items()},
    }


def verify_stage_job(
    api: Kflow,
    reference: str,
    task: str,
    metadata: Mapping[str, Any],
    artifacts: Iterable[str],
    label: str,
) -> dict[str, Any]:
    job = get_job(api, reference)
    status = str(job.get("status") or "").strip().lower()
    if status not in SUCCESS_STATUSES:
        raise RuntimeError(
            f"{label} job {reference} is not complete (status={status or 'unknown'})"
        )
    if str(job.get("report_code") or "") != task:
        raise RuntimeError(f"{label} job {reference} belongs to the wrong Kflow task")
    remote_host = str(job.get("remote_host") or "").strip()
    remote_slot = str(job.get("remote_host_slot") or "").strip()
    slot_machine = remote_slot.rsplit("@", 1)[-1] if "@" in remote_slot else ""
    if remote_host != REMOTE_HOST or not re.match(r"^suvofp", slot_machine):
        raise RuntimeError(
            f"{label} job {reference} did not complete in the canonical Suva slot "
            f"(remote_host={remote_host or 'missing'}, "
            f"remote_host_slot={remote_slot or 'missing'})"
        )
    metadata_subset(job.get("metadata"), metadata, f"{label} metadata")
    require_artifacts(job, artifacts, label)
    return job


def common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mfclrtmb-ref", required=True)
    parser.add_argument("--mfclrtmb-sha", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument(
        "--kflow-url", default=os.environ.get("KFLOW_URL", "http://127.0.0.1:8089")
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE)
    parser.add_argument(
        "--confirm-sole-launcher",
        action="store_true",
        help=(
            "Confirm this is the sole launcher and state file for this workflow; "
            "Kflow has no remote JOB_KEY uniqueness constraint"
        ),
    )
    parser.add_argument("--dry-run", action="store_true")


def gate_approval_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--approved-gate-job", required=True)
    parser.add_argument(
        "--confirm-gate-approved",
        action="store_true",
        help="Record explicit user approval after reviewing the completed gate",
    )
    parser.add_argument(
        "--geometry",
        required=True,
        choices=("stan-adaptive",),
        help="Explicitly select the only locally validated geometry",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    gate = subparsers.add_parser("gate", help="Submit only the evaluation gate")
    common_parser(gate)

    pilot = subparsers.add_parser(
        "pilot", help="Submit the mandatory short v2.6 pilot after gate approval"
    )
    common_parser(pilot)
    gate_approval_parser(pilot)

    chains = subparsers.add_parser(
        "chains", help="Submit ten chains after separate gate and pilot approvals"
    )
    common_parser(chains)
    gate_approval_parser(chains)
    chains.add_argument("--approved-pilot-job", required=True)
    chains.add_argument(
        "--confirm-pilot-approved",
        action="store_true",
        help="Record explicit user approval after reviewing the completed short pilot",
    )
    chains.add_argument("--seed", type=int, default=20260812)
    return parser.parse_args(argv)


def normalize_reference(value: str, flag: str) -> str:
    clean = str(value).strip().lstrip("#")
    if not clean or not re.fullmatch(r"[A-Za-z0-9_.-]+", clean):
        raise RuntimeError(f"{flag} must be a non-empty Kflow job reference")
    return clean


def explicit_job_payload(
    registration: Mapping[str, Any],
    env: Mapping[str, Any],
    tags: Mapping[str, Any],
    metadata: Mapping[str, Any],
    input_jobs: list[str],
    batch_name: str,
) -> dict[str, Any]:
    return {
        "repo": REPO,
        "branch": registration["branch"],
        "make_target": registration["make_target"],
        "command": registration["command"],
        "target_folder": registration["target_folder"],
        "checkout": registration["checkout"],
        "docker_image": registration["docker_image"],
        "remote_user": registration["remote_user"],
        "remote_host": registration["remote_host"],
        "remote_base_dir": registration["remote_base_dir"],
        "slot_requirements": registration["slot_requirements"],
        "cpus": registration["cpus"],
        "memory": registration["memory"],
        "disk": registration["disk"],
        "ghcr_login": registration["ghcr_login"],
        "exclude_machines": registration["exclude_machines"],
        "exclude_slots": registration["exclude_slots"],
        "output_patterns": registration["output_patterns"],
        "input_jobs": input_jobs,
        "triggers": {},
        "env": {**nested_mapping(registration.get("env")), **env},
        "batch_name": batch_name,
        "tags": {**nested_mapping(registration.get("tags")), **tags},
        "metadata": {
            **nested_mapping(registration.get("metadata")),
            **metadata,
            "input_jobs_override": True,
            "expected_input_jobs": list(input_jobs),
        },
    }


def seal_job(task: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
    digest = canonical_sha256(
        {"contract_version": 1, "workflow_id": WORKFLOW_ID, "task": task, "payload": payload}
    )
    key = f"mfclrtmb-mcmc-20260813-{label}-{digest}"
    payload["env"] = {
        **payload["env"],
        "JOB_KEY": key,
        "KFLOW_SUBMISSION_KEY": key,
    }
    payload["tags"] = {
        **payload["tags"],
        "JOB_KEY": key,
        "submission_key": key,
    }
    payload["metadata"] = {
        **payload["metadata"],
        "job_key": key,
        "submission_key": key,
        "submission_payload_sha256": digest,
        "submission_contract_version": 1,
    }
    return payload


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    if args.branch != BRANCH:
        raise RuntimeError(f"Kflow checkout branch must remain {BRANCH}")
    workflow_sha = full_sha(args.workflow_sha, "--workflow-sha")
    fix_ref = validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref")
    fix_sha = full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha")
    config = read_config(CONFIG[args.mode])
    task = task_code(config, args.mode)
    registration = task_payload(config, args.branch, task)
    if args.mode == "pilot":
        job_config = dict(registration["metadata"].get("job_config") or {})
        job_config["required"] = list(job_config.get("required") or [])
        registration["env"] = {**registration["env"], "MCMC_RUN_KIND": "pilot"}
        registration["tags"] = {**registration["tags"], "stage": "mfclrtmb-mcmc-pilot"}
        registration["metadata"] = {
            **registration["metadata"],
            "task_role": "mfclrtmb-mcmc-pilot",
            "job_config": job_config,
        }
        registration["output_patterns"] = ["outputs/mfclrtmb-mcmc-pilot/**"]
    elif args.mode == "chains":
        job_config = dict(registration["metadata"].get("job_config") or {})
        required = list(job_config.get("required") or [])
        for name in ("MCMC_APPROVED_PILOT_JOB", "MCMC_PILOT_APPROVAL"):
            if name not in required:
                required.append(name)
        job_config["required"] = required
        registration["env"] = {**registration["env"], "MCMC_RUN_KIND": "production"}
        registration["tags"] = {**registration["tags"], "stage": "mfclrtmb-mcmc"}
        registration["metadata"] = {
            **registration["metadata"],
            "task_role": "mfclrtmb-mcmc-chain",
            "job_config": job_config,
        }
        registration["output_patterns"] = ["outputs/mfclrtmb-mcmc/**"]
    provenance = common_provenance(workflow_sha, fix_ref, fix_sha)
    base_env = {
        "WORKFLOW_SHA": workflow_sha,
        "SINGLE_AREA_MODEL_SOURCE_SHA": SOURCE_SHA,
        "MFCLRTMB_FIX_REF": fix_ref,
        "MFCLRTMB_FIX_SHA": fix_sha,
        "KFLOW_RUNTIME_IMAGE": RUNTIME_IMAGE,
        "KFLOW_FORWARD_GITHUB_TOKEN_TO_RUNTIME": "1",
        "SPARSENUTS_REF": SPARSENUTS_SHA,
        "SPARSENUTS_VERSION": SPARSENUTS_VERSION,
        "STANESTIMATORS_REF": STANESTIMATORS_SHA,
        "STANESTIMATORS_VERSION": STANESTIMATORS_VERSION,
        **{env_name: digest for _, (env_name, digest) in INPUT_SHA256.items()},
    }
    base_metadata = {
        "internal_task": False,
        "task_visibility": "primary",
        **provenance,
    }

    if args.mode == "gate":
        payload = explicit_job_payload(
            registration,
            base_env,
            {
                "stage": "mfclrtmb-eval-gate",
                "species": "BET",
                "configuration": "single-area",
            },
            {
                **base_metadata,
                "workflow_role": "mfclrtmb-eval-gate",
                "requires_user_review_before_pilot": True,
                "job_title": "Single-area BET mfclrtmb parity gate",
            },
            [],
            "single-area-bet-mfclrtmb-eval-gate",
        )
        seal_job(task, payload, "gate")
        return {
            "mode": "gate",
            "workflow_sha": workflow_sha,
            "registration": registration,
            "jobs": [(task, payload)],
        }

    if not args.confirm_gate_approved:
        raise RuntimeError(
            "--confirm-gate-approved is required after reviewing the completed gate"
        )
    gate_job = normalize_reference(args.approved_gate_job, "--approved-gate-job")

    if args.mode == "pilot":
        inputs = [gate_job]
        env = {
            **base_env,
            "MCMC_RUN_KIND": "pilot",
            "MCMC_APPROVED_GATE_JOB": gate_job,
            "MCMC_APPROVED_PILOT_JOB": "",
            "MCMC_GATE_APPROVAL": "approved-after-review",
            "MCMC_PILOT_APPROVAL": "",
            "MCMC_GEOMETRY": args.geometry,
            "MCMC_CHAIN_ID": "1",
            "MCMC_TOTAL_CHAINS": "1",
            "MCMC_WARMUP": "20",
            "MCMC_SAMPLES": "5",
            "MCMC_ADAPT_DELTA": "0.8",
            "MCMC_MAX_TREEDEPTH": "3",
            "MCMC_REFRESH": "1",
            "MCMC_SEED": "20260813",
            "MCMC_SKIP_MONITOR": "true",
        }
        metadata = {
            **base_metadata,
            "workflow_role": "mfclrtmb-mcmc-pilot",
            "run_kind": "pilot",
            "approved_gate_job": gate_job,
            "gate_explicitly_approved": True,
            "geometry": args.geometry,
            "chain_id": 1,
            "total_chains": 1,
            "warmup": 20,
            "samples": 5,
            "refresh": 1,
            "adapt_delta": 0.8,
            "max_treedepth": 3,
            "seed": 20260813,
            "requires_user_review_before_production": True,
            "job_title": "Single-area BET mfclrtmb short v2.6 pilot",
        }
        payload = explicit_job_payload(
            registration,
            env,
            {
                "stage": "mfclrtmb-mcmc-pilot",
                "species": "BET",
                "configuration": "single-area",
                "chain": "pilot",
            },
            metadata,
            inputs,
            "single-area-bet-mfclrtmb-pilot",
        )
        seal_job(task, payload, "pilot")
        return {
            "mode": "pilot",
            "workflow_sha": workflow_sha,
            "approved_gate_job": gate_job,
            "registration": registration,
            "jobs": [(task, payload)],
        }

    if not args.confirm_pilot_approved:
        raise RuntimeError(
            "--confirm-pilot-approved is required after reviewing the completed short pilot"
        )
    pilot_job = normalize_reference(args.approved_pilot_job, "--approved-pilot-job")
    if args.seed < 1:
        raise RuntimeError("--seed must be a positive integer")
    inputs = [gate_job, pilot_job]
    jobs: list[tuple[str, dict[str, Any]]] = []
    for chain_id in range(1, 11):
        env = {
            **base_env,
            "MCMC_RUN_KIND": "production",
            "MCMC_APPROVED_GATE_JOB": gate_job,
            "MCMC_APPROVED_PILOT_JOB": pilot_job,
            "MCMC_GATE_APPROVAL": "approved-after-review",
            "MCMC_PILOT_APPROVAL": "approved-after-review",
            "MCMC_GEOMETRY": args.geometry,
            "MCMC_CHAIN_ID": str(chain_id),
            "MCMC_TOTAL_CHAINS": "10",
            "MCMC_WARMUP": "150",
            "MCMC_SAMPLES": "100",
            "MCMC_ADAPT_DELTA": "0.8",
            "MCMC_MAX_TREEDEPTH": "10",
            "MCMC_REFRESH": "1",
            "MCMC_SEED": str(args.seed),
            "MCMC_SKIP_MONITOR": "false",
        }
        metadata = {
            **base_metadata,
            "workflow_role": "mfclrtmb-mcmc-chain",
            "run_kind": "production",
            "approved_gate_job": gate_job,
            "approved_pilot_job": pilot_job,
            "gate_explicitly_approved": True,
            "pilot_explicitly_approved": True,
            "geometry": args.geometry,
            "chain_id": chain_id,
            "total_chains": 10,
            "warmup": 150,
            "samples": 100,
            "refresh": 1,
            "adapt_delta": 0.8,
            "max_treedepth": 10,
            "base_seed": args.seed,
            "job_title": f"Single-area BET mfclrtmb chain {chain_id:02d}/10",
        }
        payload = explicit_job_payload(
            registration,
            env,
            {
                "stage": "mfclrtmb-mcmc",
                "species": "BET",
                "configuration": "single-area",
                "chain": str(chain_id),
            },
            metadata,
            inputs,
            f"single-area-bet-mfclrtmb-chain-{chain_id:02d}",
        )
        seal_job(task, payload, f"chain-{chain_id:02d}")
        jobs.append((task, payload))
    return {
        "mode": "chains",
        "workflow_sha": workflow_sha,
        "approved_gate_job": gate_job,
        "approved_pilot_job": pilot_job,
        "registration": registration,
        "jobs": jobs,
    }


def serializable_plan(plan: dict[str, Any]) -> dict[str, Any]:
    result = dict(plan)
    result["jobs"] = [
        {"task": task, "payload": payload} for task, payload in plan["jobs"]
    ]
    return result


def task_name_for(mode: str) -> str:
    config = read_config(CONFIG[mode])
    return task_code(config, mode)


def verify_gate_for_plan(api: Kflow, plan: Mapping[str, Any], args: argparse.Namespace) -> None:
    provenance = common_provenance(
        str(plan["workflow_sha"]),
        validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref"),
        full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha"),
    )
    job = verify_stage_job(
        api,
        str(plan["approved_gate_job"]),
        task_name_for("gate"),
        {**provenance, "workflow_role": "mfclrtmb-eval-gate"},
        GATE_ARTIFACTS,
        "Approved gate",
    )
    expected = build_plan(
        parse_args(
            [
                "gate",
                "--workflow-sha",
                str(plan["workflow_sha"]),
                "--mfclrtmb-ref",
                validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref"),
                "--mfclrtmb-sha",
                full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha"),
            ]
        )
    )
    expected_task, expected_payload = expected["jobs"][0]
    validate_reconciled_job(expected_task, expected_payload, job)


def verify_pilot_for_plan(api: Kflow, plan: Mapping[str, Any], args: argparse.Namespace) -> None:
    provenance = common_provenance(
        str(plan["workflow_sha"]),
        validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref"),
        full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha"),
    )
    job = verify_stage_job(
        api,
        str(plan["approved_pilot_job"]),
        task_name_for("pilot"),
        {
            **provenance,
            "workflow_role": "mfclrtmb-mcmc-pilot",
            "run_kind": "pilot",
            "approved_gate_job": str(plan["approved_gate_job"]),
            "gate_explicitly_approved": True,
            "geometry": args.geometry,
            "chain_id": 1,
            "total_chains": 1,
            "warmup": 20,
            "samples": 5,
            "refresh": 1,
            "adapt_delta": 0.8,
            "max_treedepth": 3,
            "seed": 20260813,
        },
        PILOT_ARTIFACTS,
        "Approved pilot",
    )
    pilot_arguments = [
        "pilot",
        "--workflow-sha",
        str(plan["workflow_sha"]),
        "--mfclrtmb-ref",
        validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref"),
        "--mfclrtmb-sha",
        full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha"),
        "--approved-gate-job",
        str(plan["approved_gate_job"]),
        "--confirm-gate-approved",
        "--geometry",
        args.geometry,
    ]
    expected = build_plan(parse_args(pilot_arguments))
    expected_task, expected_payload = expected["jobs"][0]
    validate_reconciled_job(expected_task, expected_payload, job)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan = build_plan(args)
    if args.dry_run:
        print(json.dumps(serializable_plan(plan), indent=2, sort_keys=True))
        return 0

    if not args.confirm_sole_launcher:
        raise RuntimeError(
            "--confirm-sole-launcher is required because Kflow has no distributed "
            "JOB_KEY uniqueness constraint; use exactly one launcher/state file"
        )

    fix_ref = validated_git_ref(args.mfclrtmb_ref, "--mfclrtmb-ref")
    fix_sha = full_sha(args.mfclrtmb_sha, "--mfclrtmb-sha")
    workflow_sha = full_sha(args.workflow_sha, "--workflow-sha")
    preflight_repository(workflow_sha, fix_ref, fix_sha)
    token = os.environ.get("KFLOW_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set KFLOW_API_TOKEN")
    # Deliberately omit the client GitHub credential header. The audited Kflow
    # service credential is used for the primary checkout and protected runtime
    # forwarding; a caller token must not silently override that known-good
    # server credential.
    api = Kflow(args.kflow_url, token)
    with DurableState(args.state_file, workflow_sha) as state:
        if args.mode in {"pilot", "chains"}:
            verify_gate_for_plan(api, plan, args)
        if args.mode == "chains":
            verify_pilot_for_plan(api, plan, args)
        registration_status = ensure_registration(api, plan["registration"], state)
        jobs = []
        for task, payload in plan["jobs"]:
            number, status = submit_or_reconcile(api, task, payload, state)
            jobs.append({"job_number": number, "status": status})
    print(
        json.dumps(
            {
                "mode": args.mode,
                "registration": registration_status,
                "jobs": jobs,
                "state_file": str(args.state_file.expanduser().resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RuntimeError,
        urllib.error.URLError,
        subprocess.CalledProcessError,
        OSError,
    ) as error:
        raise SystemExit(f"ERROR: {error}")
