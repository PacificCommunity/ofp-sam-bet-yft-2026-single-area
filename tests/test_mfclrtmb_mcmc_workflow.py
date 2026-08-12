from __future__ import annotations

import ast
import importlib.util
import json
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch-mfclrtmb-mcmc.py"
FIX_REF = "refs/heads/reviewed-growth-bh-fix"
FIX_SHA = "a" * 40
WORKFLOW_SHA = "b" * 40
BRANCH = "mfclrtmb-mcmc-2026-08-12"

SPEC = importlib.util.spec_from_file_location("mfclrtmb_launcher", LAUNCHER)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


def run_launcher(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(LAUNCHER), *arguments],
        cwd=ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def common_arguments(mode: str) -> list[str]:
    return [
        mode,
        "--mfclrtmb-ref",
        FIX_REF,
        "--mfclrtmb-sha",
        FIX_SHA,
        "--workflow-sha",
        WORKFLOW_SHA,
    ]


def dry_plan(*arguments: str) -> dict[str, Any]:
    completed = run_launcher(*arguments, "--dry-run")
    return json.loads(completed.stdout)


def build_plan(arguments: list[str]) -> dict[str, Any]:
    return launcher.build_plan(launcher.parse_args(arguments + ["--dry-run"]))


def make_job(task: str, payload: dict[str, Any], number: int = 41001) -> dict[str, Any]:
    return {
        "id": f"j{number}",
        "job_number": number,
        "report_code": task,
        "repo_full_name": payload["repo"],
        "branch": payload["branch"],
        "make_target": payload["make_target"],
        "command": payload["command"],
        "target_folder": payload["target_folder"],
        "docker_image": payload["docker_image"],
        "remote_user": payload["remote_user"],
        "remote_host": payload["remote_host"],
        "remote_dir": f"{payload['remote_base_dir']}/{task}/job-{number}",
        "batch_name": payload["batch_name"],
        "cpus": payload["cpus"],
        "memory": payload["memory"],
        "disk": payload["disk"],
        "status": "submitted",
        "env": dict(payload["env"]),
        "tags": dict(payload["tags"]),
        "metadata": dict(payload["metadata"]),
        "details": {
            "report_spec": {
                "output_patterns": list(payload["output_patterns"]),
                "input_jobs": list(payload["input_jobs"]),
                "slot_requirements": payload["slot_requirements"],
                "checkout": dict(payload["checkout"]),
                "exclude_machines": list(payload["exclude_machines"]),
                "exclude_slots": list(payload["exclude_slots"]),
                "ghcr_login": payload["ghcr_login"],
                "triggers": dict(payload["triggers"]),
            }
        },
    }


class MemoryState:
    def __init__(self, jobs: dict[str, Any] | None = None) -> None:
        self.data = {"jobs": dict(jobs or {}), "registrations": {}}
        self.registrations: list[tuple[str, str]] = []

    def record_registration(self, name: str, status: str) -> None:
        self.registrations.append((name, status))
        self.data["registrations"][name] = {"status": status}

    def record_job(self, key: str, **values: Any) -> None:
        record = dict(self.data["jobs"].get(key) or {})
        record.update(values)
        self.data["jobs"][key] = record


class RegistrationAPI:
    def __init__(
        self,
        existing: dict[str, Any] | None = None,
        ambiguous: bool = False,
        commit_ambiguous: bool = True,
    ) -> None:
        self.existing = existing
        self.ambiguous = ambiguous
        self.commit_ambiguous = commit_ambiguous
        self.posts = 0
        self.gets = 0

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "GET":
            self.gets += 1
            if self.existing is None:
                raise launcher.KflowHTTPError(method, path, 404, "missing")
            return {"report": self.existing}
        if method == "POST" and path.startswith("/api/report/"):
            self.posts += 1
            assert payload is not None
            if self.ambiguous:
                if self.commit_ambiguous:
                    self.existing = dict(payload)
                raise TimeoutError("response lost after commit")
            self.existing = dict(payload)
            return {"report": self.existing}
        raise AssertionError((method, path, payload))


class JobAPI:
    def __init__(
        self,
        task: str,
        payload: dict[str, Any],
        jobs: list[dict[str, Any]] | None = None,
        ambiguous: bool = False,
        commit_ambiguous: bool = True,
    ) -> None:
        self.task = task
        self.payload = payload
        self.jobs = list(jobs or [])
        self.ambiguous = ambiguous
        self.commit_ambiguous = commit_ambiguous
        self.job_posts = 0
        self.page_calls: list[tuple[str, int]] = []
        self.unfiltered_page_calls: list[int] = []

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if method == "GET" and path.startswith("/api/jobs/"):
            assert payload is None
            page = int(re.search(r"[?&]page=([0-9]+)", path).group(1))
            self.unfiltered_page_calls.append(page)
            return {"jobs": list(self.jobs) if page == 1 else []}
        if method == "POST" and path.startswith("/api/jobs/"):
            assert payload is not None and len(payload) == 1
            tag, value = next(iter(payload.items()))
            page = int(re.search(r"[?&]page=([0-9]+)", path).group(1))
            self.page_calls.append((tag, page))
            if page != 1:
                return {"jobs": []}
            matching = [
                job for job in self.jobs if str((job.get("tags") or {}).get(tag) or "") == value
            ]
            return {"jobs": matching}
        if method == "POST" and path.startswith("/api/job/"):
            self.job_posts += 1
            job = make_job(self.task, self.payload)
            if self.ambiguous:
                if self.commit_ambiguous:
                    self.jobs.append(job)
                raise TimeoutError("response lost after commit")
            self.jobs.append(job)
            return {"job": job}
        raise AssertionError((method, path, payload))


class WorkflowConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = yaml.safe_load(
            (ROOT / "kflow-mfclrtmb-eval-gate.yaml").read_text(encoding="utf-8")
        )
        self.chain = yaml.safe_load(
            (ROOT / "kflow-mfclrtmb-mcmc-chain.yaml").read_text(encoding="utf-8")
        )

    def test_gate_pilot_chain_task_codes_are_distinct(self) -> None:
        codes = {self.gate["name"], self.chain["pilot_name"], self.chain["name"]}
        self.assertEqual(len(codes), 3)

    def test_tasks_use_exact_v26_suva_linux_docker_contract(self) -> None:
        for config in (self.gate, self.chain):
            self.assertEqual(config["triggers"], {})
            self.assertEqual(config["input_jobs"], [])
            self.assertEqual(config["docker_image"], launcher.RUNTIME_IMAGE)
            self.assertEqual(config["remote_host"], "suvofpsubmit.corp.spc.int")
            self.assertEqual(config["slot_requirements"], launcher.SLOT_REQUIREMENTS)
            self.assertEqual(config["resources"]["cpus"], 2)
            self.assertEqual(config["resources"]["memory"], "16GB")

    def test_all_five_input_hashes_are_explicit_in_both_tasks(self) -> None:
        for config in (self.gate, self.chain):
            for _, (env_name, digest) in launcher.INPUT_SHA256.items():
                self.assertEqual(config["env"][env_name], digest)
            self.assertEqual(config["env"]["WORKFLOW_SHA"], "")

    def test_sampler_pins_are_exact(self) -> None:
        env = self.chain["env"]
        self.assertEqual(env["SPARSENUTS_REF"], launcher.SPARSENUTS_SHA)
        self.assertEqual(env["SPARSENUTS_VERSION"], "1.0.2")
        self.assertEqual(env["STANESTIMATORS_REF"], launcher.STANESTIMATORS_SHA)
        self.assertEqual(env["STANESTIMATORS_VERSION"], "0.3.1")

    def test_job_side_scripts_verify_checkout_inputs_and_package_commits(self) -> None:
        install = (ROOT / "inst/mfclrtmb-mcmc/install-pinned-mfclrtmb.sh").read_text()
        chain_shell = (ROOT / "inst/mfclrtmb-mcmc/run-chain.sh").read_text()
        gate_r = (ROOT / "inst/mfclrtmb-mcmc/run-eval-gate.R").read_text()
        chain_r = (ROOT / "inst/mfclrtmb-mcmc/run-chain.R").read_text()
        self.assertIn("git rev-parse 'HEAD^{commit}'", install)
        self.assertIn("WORKFLOW_SHA", install)
        self.assertIn("andrjohns/StanEstimators", chain_shell)
        self.assertIn("RemoteSha", chain_shell)
        for env_name in [value[0] for value in launcher.INPUT_SHA256.values()]:
            self.assertIn(env_name, gate_r)
            self.assertIn(env_name, chain_r)
        self.assertIn("pilot-result.csv", chain_r)
        self.assertIn("MCMC_RUN_KIND", chain_r)
        for text in (gate_r, chain_r):
            self.assertIn("inputs$frq$dimensions$n_tag_groups", text)
            self.assertIn("!is.null(inputs$tag)", text)
            self.assertIn("provenance-only", text)

    def test_launcher_has_no_duplicate_literal_dictionary_keys(self) -> None:
        tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"), filename=str(LAUNCHER))
        duplicates: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            for key in set(keys):
                if keys.count(key) > 1:
                    duplicates.append((node.lineno, key))
        self.assertEqual(duplicates, [])

    def test_runtime_yaml_loader_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate key 'branch'"):
            yaml.load("branch: first\nbranch: second\n", Loader=launcher.UniqueKeyLoader)

    def test_all_standalone_and_embedded_r_is_parseable(self) -> None:
        files = [
            ROOT / "inst/mfclrtmb-mcmc/run-eval-gate.R",
            ROOT / "inst/mfclrtmb-mcmc/run-chain.R",
        ]
        for path in files:
            completed = subprocess.run(
                ["Rscript", "--vanilla", "-e", "invisible(parse(file('stdin')))"],
                input=path.read_text(encoding="utf-8"),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
        shell = (ROOT / "inst/mfclrtmb-mcmc/run-chain.sh").read_text(encoding="utf-8")
        marker = "Rscript --vanilla - <<'RS'\n"
        embedded = shell.split(marker, 1)[1].split("\nRS\n", 1)[0]
        completed = subprocess.run(
            ["Rscript", "--vanilla", "-e", "invisible(parse(file('stdin')))"],
            input=embedded,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_gate_uses_standard_nonoptimizing_production_path(self) -> None:
        text = (ROOT / "inst/mfclrtmb-mcmc/run-eval-gate.R").read_text()
        self.assertIn("fit <- mfclrtmb_fit(", text)
        self.assertIn("run_optimization = FALSE", text)
        self.assertIn("write_outputs = FALSE", text)
        self.assertIn("build_report = FALSE", text)
        self.assertIn("run_sdreport = FALSE", text)
        self.assertNotIn("backend_profile =", text)


class LauncherPlanTests(unittest.TestCase):
    def test_gate_dry_run_is_one_explicit_job_and_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            plan = dry_plan(*common_arguments("gate"), "--state-file", str(state))
            self.assertFalse(state.exists())
        self.assertEqual(plan["mode"], "gate")
        self.assertEqual(len(plan["jobs"]), 1)
        job = plan["jobs"][0]["payload"]
        self.assertEqual(job["triggers"], {})
        self.assertEqual(job["input_jobs"], [])
        self.assertTrue(job["metadata"]["input_jobs_override"])
        self.assertEqual(job["env"]["WORKFLOW_SHA"], WORKFLOW_SHA)

    def test_real_launch_requires_sole_launcher_attestation(self) -> None:
        completed = run_launcher(*common_arguments("gate"), check=False)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--confirm-sole-launcher is required", completed.stderr)

    def test_pilot_requires_gate_approval(self) -> None:
        completed = run_launcher(
            *common_arguments("pilot"),
            "--approved-gate-job",
            "12345",
            "--geometry",
            "stan-adaptive",
            "--dry-run",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--confirm-gate-approved is required", completed.stderr)

    def test_short_pilot_settings_and_task_are_mandatory_and_exact(self) -> None:
        plan = dry_plan(
            *common_arguments("pilot"),
            "--approved-gate-job",
            "12345",
            "--confirm-gate-approved",
            "--geometry",
            "stan-adaptive",
        )
        self.assertIn("-pilot-", plan["registration"]["name"])
        self.assertNotIn(
            "MCMC_APPROVED_PILOT_JOB",
            plan["registration"]["metadata"]["job_config"]["required"],
        )
        self.assertEqual(len(plan["jobs"]), 1)
        job = plan["jobs"][0]["payload"]
        expected = {
            "MCMC_RUN_KIND": "pilot",
            "MCMC_TOTAL_CHAINS": "1",
            "MCMC_WARMUP": "20",
            "MCMC_SAMPLES": "5",
            "MCMC_MAX_TREEDEPTH": "3",
            "MCMC_REFRESH": "1",
            "MCMC_SEED": "20260813",
            "MCMC_SKIP_MONITOR": "true",
        }
        for name, value in expected.items():
            self.assertEqual(job["env"][name], value)
        self.assertEqual(job["input_jobs"], ["12345"])
        self.assertTrue(job["metadata"]["requires_user_review_before_production"])

    def test_production_requires_separate_pilot_approval(self) -> None:
        completed = run_launcher(
            *common_arguments("chains"),
            "--approved-gate-job",
            "12345",
            "--confirm-gate-approved",
            "--approved-pilot-job",
            "12346",
            "--geometry",
            "stan-adaptive",
            "--dry-run",
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--confirm-pilot-approved is required", completed.stderr)

    def test_production_plan_preserves_user_settings_and_has_ten_jobs(self) -> None:
        plan = dry_plan(
            *common_arguments("chains"),
            "--approved-gate-job",
            "12345",
            "--confirm-gate-approved",
            "--approved-pilot-job",
            "12346",
            "--confirm-pilot-approved",
            "--geometry",
            "stan-adaptive",
            "--seed",
            "91234",
        )
        self.assertIn("-chain-", plan["registration"]["name"])
        required = plan["registration"]["metadata"]["job_config"]["required"]
        self.assertIn("MCMC_APPROVED_PILOT_JOB", required)
        self.assertIn("MCMC_PILOT_APPROVAL", required)
        self.assertEqual(len(plan["jobs"]), 10)
        keys: set[str] = set()
        for chain_id, item in enumerate(plan["jobs"], 1):
            job = item["payload"]
            self.assertEqual(job["input_jobs"], ["12345", "12346"])
            self.assertEqual(job["triggers"], {})
            self.assertTrue(job["metadata"]["input_jobs_override"])
            self.assertEqual(job["env"]["MCMC_RUN_KIND"], "production")
            self.assertEqual(job["env"]["MCMC_CHAIN_ID"], str(chain_id))
            self.assertEqual(job["env"]["MCMC_TOTAL_CHAINS"], "10")
            self.assertEqual(job["env"]["MCMC_WARMUP"], "150")
            self.assertEqual(job["env"]["MCMC_SAMPLES"], "100")
            self.assertEqual(job["env"]["MCMC_REFRESH"], "1")
            self.assertEqual(job["env"]["MCMC_ADAPT_DELTA"], "0.8")
            self.assertEqual(job["env"]["MCMC_MAX_TREEDEPTH"], "10")
            self.assertEqual(job["env"]["MCMC_SEED"], "91234")
            self.assertEqual(job["env"]["MCMC_SKIP_MONITOR"], "false")
            self.assertEqual(job["cpus"], 2)
            self.assertEqual(job["memory"], "16GB")
            self.assertEqual(job["remote_host"], launcher.REMOTE_HOST)
            key = job["env"]["JOB_KEY"]
            self.assertEqual(job["metadata"]["submission_key"], key)
            self.assertEqual(job["tags"]["JOB_KEY"], key)
            keys.add(key)
        self.assertEqual(len(keys), 10)

    def test_deterministic_job_keys_are_stable(self) -> None:
        arguments = [
            *common_arguments("chains"),
            "--approved-gate-job",
            "12345",
            "--confirm-gate-approved",
            "--approved-pilot-job",
            "12346",
            "--confirm-pilot-approved",
            "--geometry",
            "stan-adaptive",
        ]
        first = build_plan(arguments)
        second = build_plan(arguments)
        first_keys = [payload["env"]["JOB_KEY"] for _, payload in first["jobs"]]
        second_keys = [payload["env"]["JOB_KEY"] for _, payload in second["jobs"]]
        self.assertEqual(first_keys, second_keys)

    def test_only_stan_adaptive_geometry_is_accepted_and_has_no_geometry_job(self) -> None:
        for unsupported in ("adaptive-diagonal", "native-hessian"):
            completed = run_launcher(
                *common_arguments("pilot"),
                "--approved-gate-job",
                "12345",
                "--confirm-gate-approved",
                "--geometry",
                unsupported,
                "--dry-run",
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid choice", completed.stderr)
        plan = build_plan(
            [
                *common_arguments("chains"),
                "--approved-gate-job",
                "12345",
                "--confirm-gate-approved",
                "--approved-pilot-job",
                "12346",
                "--confirm-pilot-approved",
                "--geometry",
                "stan-adaptive",
            ]
        )
        for _, payload in plan["jobs"]:
            self.assertEqual(payload["env"]["MCMC_GEOMETRY"], "stan-adaptive")
            self.assertEqual(payload["input_jobs"], ["12345", "12346"])
            self.assertNotIn("MCMC_GEOMETRY_JOB", payload["env"])

    def test_noncanonical_or_option_like_fix_ref_is_rejected(self) -> None:
        for value in ("--upload-pack=unsafe", "main", "refs/heads/../unsafe", FIX_SHA):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                launcher.validated_git_ref(value, "fix ref")


class RegistrationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = build_plan(common_arguments("gate"))
        self.registration = self.plan["registration"]

    def test_existing_exact_registration_is_reused_without_post(self) -> None:
        api = RegistrationAPI(existing=dict(self.registration))
        state = MemoryState()
        status = launcher.ensure_registration(api, self.registration, state)
        self.assertEqual(status, "reused-exact")
        self.assertEqual(api.posts, 0)

    def test_existing_mismatch_aborts_without_overwrite(self) -> None:
        existing = dict(self.registration)
        existing["docker_image"] = "wrong"
        api = RegistrationAPI(existing=existing)
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            launcher.ensure_registration(api, self.registration, MemoryState())
        self.assertEqual(api.posts, 0)

    def test_existing_task_metadata_extra_is_not_immutable_equality(self) -> None:
        existing = json.loads(json.dumps(self.registration))
        existing["metadata"]["stale_extra"] = "unsafe"
        api = RegistrationAPI(existing=existing)
        with self.assertRaisesRegex(RuntimeError, r"extra=\['stale_extra'\]"):
            launcher.ensure_registration(api, self.registration, MemoryState())
        self.assertEqual(api.posts, 0)

    def test_documented_server_repo_metadata_is_allowed(self) -> None:
        existing = json.loads(json.dumps(self.registration))
        existing["metadata"].update(
            {
                "repo_private": False,
                "repo_owner_login": "PacificCommunity",
                "repo_owner_type": "Organization",
                "repo_owner_avatar_url": "https://example.invalid/avatar.png",
            }
        )
        api = RegistrationAPI(existing=existing)
        status = launcher.ensure_registration(api, self.registration, MemoryState())
        self.assertEqual(status, "reused-exact")
        self.assertEqual(api.posts, 0)

    def test_missing_registration_posts_once_then_rereads(self) -> None:
        api = RegistrationAPI()
        status = launcher.ensure_registration(api, self.registration, MemoryState())
        self.assertEqual(status, "registered-once")
        self.assertEqual(api.posts, 1)
        self.assertGreaterEqual(api.gets, 2)

    def test_ambiguous_registration_post_is_get_reconciled_not_retried(self) -> None:
        api = RegistrationAPI(ambiguous=True)
        status = launcher.ensure_registration(api, self.registration, MemoryState())
        self.assertEqual(status, "reconciled-after-ambiguous-post")
        self.assertEqual(api.posts, 1)

    def test_prior_registration_post_with_missing_server_task_is_not_retried(self) -> None:
        api = RegistrationAPI()
        state = MemoryState()
        state.data["registrations"][self.registration["name"]] = {
            "status": "ambiguous-not-found"
        }
        with self.assertRaisesRegex(RuntimeError, "Refusing a second POST"):
            launcher.ensure_registration(api, self.registration, state)
        self.assertEqual(api.posts, 0)

    def test_ambiguous_uncommitted_registration_stops_after_one_post(self) -> None:
        api = RegistrationAPI(ambiguous=True, commit_ambiguous=False)
        state = MemoryState()
        with self.assertRaisesRegex(RuntimeError, "outcome is ambiguous"):
            launcher.ensure_registration(api, self.registration, state)
        self.assertEqual(api.posts, 1)
        self.assertEqual(
            state.data["registrations"][self.registration["name"]]["status"],
            "ambiguous-not-found",
        )


class SubmissionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        plan = build_plan(common_arguments("gate"))
        self.task, self.payload = plan["jobs"][0]
        self.key = self.payload["metadata"]["submission_key"]

    def test_paginated_both_key_names_are_queried_and_deduplicated(self) -> None:
        job = make_job(self.task, self.payload)
        api = JobAPI(self.task, self.payload, [job])
        matches = launcher.list_jobs_by_key(api, self.task, self.key)
        self.assertEqual(len(matches), 1)
        self.assertEqual(api.unfiltered_page_calls, [1, 2])
        self.assertIn(("JOB_KEY", 1), api.page_calls)
        self.assertIn(("JOB_KEY", 2), api.page_calls)
        self.assertIn(("submission_key", 1), api.page_calls)
        self.assertIn(("submission_key", 2), api.page_calls)

    def test_unfiltered_scan_detects_metadata_env_and_top_level_only_keys(self) -> None:
        variants = []
        for location in ("metadata", "env", "top"):
            job = make_job(self.task, self.payload, 42000 + len(variants))
            job["tags"].pop("JOB_KEY")
            job["tags"].pop("submission_key")
            job["metadata"].pop("job_key")
            job["metadata"].pop("submission_key")
            job["env"].pop("JOB_KEY")
            job["env"].pop("KFLOW_SUBMISSION_KEY")
            if location == "metadata":
                job["metadata"]["job_key"] = self.key
            elif location == "env":
                job["env"]["KFLOW_SUBMISSION_KEY"] = self.key
            else:
                job["job_key"] = self.key
            variants.append(job)
        api = JobAPI(self.task, self.payload, variants)
        matches = launcher.list_jobs_by_key(api, self.task, self.key)
        self.assertEqual(len(matches), 3)

    def test_unfiltered_untagged_duplicate_blocks_submission(self) -> None:
        exact = make_job(self.task, self.payload, 41001)
        untagged = make_job(self.task, self.payload, 41002)
        untagged["tags"] = {}
        api = JobAPI(self.task, self.payload, [exact, untagged])
        with self.assertRaisesRegex(RuntimeError, "Duplicate Kflow jobs"):
            launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())
        self.assertEqual(api.job_posts, 0)

    def test_existing_exact_job_is_reused_without_post(self) -> None:
        api = JobAPI(self.task, self.payload, [make_job(self.task, self.payload)])
        number, status = launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())
        self.assertEqual(number, "41001")
        self.assertEqual(status, "reused-exact")
        self.assertEqual(api.job_posts, 0)

    def test_duplicate_key_is_a_hard_failure(self) -> None:
        jobs = [make_job(self.task, self.payload, 41001), make_job(self.task, self.payload, 41002)]
        api = JobAPI(self.task, self.payload, jobs)
        with self.assertRaisesRegex(RuntimeError, "Duplicate Kflow jobs"):
            launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())
        self.assertEqual(api.job_posts, 0)

    def test_new_job_posts_once_and_is_reconciled(self) -> None:
        api = JobAPI(self.task, self.payload)
        number, status = launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())
        self.assertEqual(number, "41001")
        self.assertEqual(status, "submitted-once")
        self.assertEqual(api.job_posts, 1)

    def test_ambiguous_job_post_is_reconciled_without_second_post(self) -> None:
        api = JobAPI(self.task, self.payload, ambiguous=True)
        with mock.patch.object(launcher.time, "sleep"):
            number, status = launcher.submit_or_reconcile(
                api, self.task, self.payload, MemoryState()
            )
        self.assertEqual(number, "41001")
        self.assertEqual(status, "reconciled-after-ambiguous-post")
        self.assertEqual(api.job_posts, 1)

    def test_durable_prior_post_without_server_match_forbids_second_post(self) -> None:
        state = MemoryState({self.key: {"status": "ambiguous-post"}})
        api = JobAPI(self.task, self.payload)
        with self.assertRaisesRegex(RuntimeError, "Refusing a second POST"):
            launcher.submit_or_reconcile(api, self.task, self.payload, state)
        self.assertEqual(api.job_posts, 0)

    def test_ambiguous_uncommitted_job_stops_after_one_post(self) -> None:
        api = JobAPI(self.task, self.payload, ambiguous=True, commit_ambiguous=False)
        state = MemoryState()
        with mock.patch.object(launcher.time, "sleep"), self.assertRaisesRegex(
            RuntimeError, "it was not retried"
        ):
            launcher.submit_or_reconcile(api, self.task, self.payload, state)
        self.assertEqual(api.job_posts, 1)
        self.assertEqual(state.data["jobs"][self.key]["status"], "ambiguous-post")

    def test_reconciled_payload_mismatch_is_rejected(self) -> None:
        job = make_job(self.task, self.payload)
        job["docker_image"] = "wrong"
        api = JobAPI(self.task, self.payload, [job])
        with self.assertRaisesRegex(RuntimeError, "docker_image"):
            launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())

    def test_extra_job_env_tag_or_metadata_is_rejected(self) -> None:
        for field in ("env", "tags", "metadata"):
            with self.subTest(field=field):
                job = make_job(self.task, self.payload)
                job[field]["stale_extra"] = "unsafe"
                api = JobAPI(self.task, self.payload, [job])
                with self.assertRaisesRegex(RuntimeError, "not exactly equal"):
                    launcher.submit_or_reconcile(
                        api, self.task, self.payload, MemoryState()
                    )

    def test_documented_server_repo_metadata_is_allowed_on_job(self) -> None:
        job = make_job(self.task, self.payload)
        job["metadata"]["repo_private"] = False
        api = JobAPI(self.task, self.payload, [job])
        number, status = launcher.submit_or_reconcile(
            api, self.task, self.payload, MemoryState()
        )
        self.assertEqual((number, status), ("41001", "reused-exact"))

    def test_only_documented_server_callback_environment_is_allowed(self) -> None:
        job = make_job(self.task, self.payload)
        job["env"].update(
            {
                "KFLOW_CALLBACK_URL": "http://kflow.invalid/callback/41001",
                "KFLOW_CALLBACK_TOKEN": "[REDACTED]",
            }
        )
        api = JobAPI(self.task, self.payload, [job])
        number, status = launcher.submit_or_reconcile(
            api, self.task, self.payload, MemoryState()
        )
        self.assertEqual((number, status), ("41001", "reused-exact"))

    def test_undocumented_server_environment_key_is_rejected(self) -> None:
        job = make_job(self.task, self.payload)
        job["env"]["KFLOW_CALLBACK_UNDOCUMENTED"] = "unsafe"
        api = JobAPI(self.task, self.payload, [job])
        with self.assertRaisesRegex(
            RuntimeError, r"extra=\['KFLOW_CALLBACK_UNDOCUMENTED'\]"
        ):
            launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())

    def test_missing_report_specification_is_rejected(self) -> None:
        job = make_job(self.task, self.payload)
        job["details"].pop("report_spec")
        api = JobAPI(self.task, self.payload, [job])
        with self.assertRaisesRegex(RuntimeError, "no immutable report specification"):
            launcher.submit_or_reconcile(api, self.task, self.payload, MemoryState())


class StateAndPreflightTests(unittest.TestCase):
    def test_durable_state_is_mode_600_atomic_json_and_locked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nested" / "state.json"
            with launcher.DurableState(path, WORKFLOW_SHA) as first:
                first.record_job("key", status="post-intent")
                with self.assertRaisesRegex(RuntimeError, "holds the local state lock"):
                    with launcher.DurableState(path, WORKFLOW_SHA):
                        pass
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            loaded = json.loads(path.read_text())
            self.assertEqual(loaded["jobs"]["key"]["status"], "post-intent")
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_state_rejects_another_workflow_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            with launcher.DurableState(path, WORKFLOW_SHA) as state:
                state.save()
            with self.assertRaisesRegex(RuntimeError, "another workflow snapshot"):
                with launcher.DurableState(path, "c" * 40):
                    pass

    def test_repository_preflight_accepts_clean_exact_local_and_remote_sha(self) -> None:
        def fake_git(*arguments: str, **_: Any) -> str:
            if arguments == ("branch", "--show-current"):
                return BRANCH
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return ""
            if arguments == ("rev-parse", "HEAD"):
                return WORKFLOW_SHA
            if arguments[:4] == ("ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}"):
                return f"{WORKFLOW_SHA}\trefs/heads/{BRANCH}"
            if arguments[:3] == (
                "ls-remote",
                "--exit-code",
                "https://github.com/PacificCommunity/ofp-sam-mfclrtmb.git",
            ):
                return f"{FIX_SHA}\t{FIX_REF}"
            raise AssertionError(arguments)

        with mock.patch.object(launcher, "run_git", side_effect=fake_git), mock.patch.object(
            launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            launcher.preflight_repository(WORKFLOW_SHA, FIX_REF, FIX_SHA)
        self.assertEqual(run.call_count, 2)

    def test_repository_preflight_resolves_exact_remote_fix_branch(self) -> None:
        def fake_git(*arguments: str, **_: Any) -> str:
            if arguments == ("branch", "--show-current"):
                return BRANCH
            if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
                return ""
            if arguments == ("rev-parse", "HEAD"):
                return WORKFLOW_SHA
            if arguments[:4] == ("ls-remote", "--exit-code", "origin", f"refs/heads/{BRANCH}"):
                return f"{WORKFLOW_SHA}\trefs/heads/{BRANCH}"
            if arguments[:3] == (
                "ls-remote",
                "--exit-code",
                "https://github.com/PacificCommunity/ofp-sam-mfclrtmb.git",
            ):
                self.assertEqual(arguments[3:], (FIX_REF,))
                return f"{FIX_SHA}\t{FIX_REF}"
            raise AssertionError(arguments)

        with mock.patch.object(launcher, "run_git", side_effect=fake_git), mock.patch.object(
            launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ):
            launcher.preflight_repository(WORKFLOW_SHA, FIX_REF, FIX_SHA)

    def test_repository_preflight_rejects_dirty_tree_before_remote_query(self) -> None:
        with mock.patch.object(
            launcher,
            "run_git",
            side_effect=[BRANCH, "?? unsafe.txt", WORKFLOW_SHA],
        ):
            with self.assertRaisesRegex(RuntimeError, "completely clean"):
                launcher.preflight_repository(WORKFLOW_SHA, FIX_REF, FIX_SHA)

    def test_repository_preflight_rejects_remote_branch_mismatch(self) -> None:
        with mock.patch.object(
            launcher,
            "run_git",
            side_effect=[
                BRANCH,
                "",
                WORKFLOW_SHA,
                f"{'c' * 40}\trefs/heads/{BRANCH}",
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "remote workflow branch"):
                launcher.preflight_repository(WORKFLOW_SHA, FIX_REF, FIX_SHA)


class ArtifactContractTests(unittest.TestCase):
    class API:
        def __init__(self, job: dict[str, Any]) -> None:
            self.job = job

        def request(self, method: str, path: str, payload: Any = None) -> Any:
            self.assertions = (method, path, payload)
            return {"job": self.job}

    def stage_job(self, artifacts: tuple[str, ...]) -> dict[str, Any]:
        return {
            "job_number": 50001,
            "report_code": "task-code",
            "status": "completed",
            "remote_host": launcher.REMOTE_HOST,
            "remote_host_slot": "slot1_1@suvofpcand22.corp.spc.int",
            "metadata": {"workflow_role": "role", "workflow_sha": WORKFLOW_SHA},
            "details": {"artifacts": {"files": [{"path": path} for path in artifacts]}},
        }

    def test_exact_completed_task_metadata_and_artifacts_pass(self) -> None:
        job = self.stage_job(launcher.GATE_ARTIFACTS)
        returned = launcher.verify_stage_job(
            self.API(job),
            "50001",
            "task-code",
            {"workflow_role": "role", "workflow_sha": WORKFLOW_SHA},
            launcher.GATE_ARTIFACTS,
            "Gate",
        )
        self.assertEqual(returned["job_number"], 50001)

    def test_wrong_task_or_missing_artifact_fails(self) -> None:
        job = self.stage_job(launcher.GATE_ARTIFACTS[:-1])
        with self.assertRaisesRegex(RuntimeError, "missing required Kflow artifacts"):
            launcher.verify_stage_job(
                self.API(job),
                "50001",
                "task-code",
                {"workflow_role": "role", "workflow_sha": WORKFLOW_SHA},
                launcher.GATE_ARTIFACTS,
                "Gate",
            )

    def test_completed_stage_requires_canonical_suva_slot(self) -> None:
        for field, value in (
            ("remote_host", "wrong.example.invalid"),
            ("remote_host_slot", ""),
            ("remote_host_slot", "slot1_1@nouofpcand01.corp.spc.int"),
        ):
            with self.subTest(field=field, value=value):
                job = self.stage_job(launcher.GATE_ARTIFACTS)
                job[field] = value
                with self.assertRaisesRegex(RuntimeError, "canonical Suva"):
                    launcher.verify_stage_job(
                        self.API(job),
                        "50001",
                        "task-code",
                        {"workflow_role": "role", "workflow_sha": WORKFLOW_SHA},
                        launcher.GATE_ARTIFACTS,
                        "Gate",
                    )
        job["report_code"] = "wrong"
        with self.assertRaisesRegex(RuntimeError, "wrong Kflow task"):
            launcher.verify_stage_job(
                self.API(job),
                "50001",
                "task-code",
                {},
                (),
                "Gate",
            )

    def test_gate_approval_reconstructs_and_checks_the_full_immutable_job(self) -> None:
        args = launcher.parse_args(
            [
                *common_arguments("pilot"),
                "--approved-gate-job",
                "50001",
                "--confirm-gate-approved",
                "--geometry",
                "stan-adaptive",
                "--dry-run",
            ]
        )
        plan = launcher.build_plan(args)
        gate_plan = build_plan(common_arguments("gate"))
        task, payload = gate_plan["jobs"][0]
        job = make_job(task, payload, 50001)
        job["status"] = "completed"
        job["remote_host_slot"] = "slot1_1@suvofpcand22.corp.spc.int"
        job["details"]["artifacts"] = {
            "files": [{"path": path} for path in launcher.GATE_ARTIFACTS]
        }
        launcher.verify_gate_for_plan(self.API(job), plan, args)
        job["env"]["stale_extra"] = "unsafe"
        with self.assertRaisesRegex(RuntimeError, "job environment"):
            launcher.verify_gate_for_plan(self.API(job), plan, args)


if __name__ == "__main__":
    unittest.main()
