# Single-area BET mfclrtmb gate, pilot, and MCMC plan

This isolated branch adds an experimental workflow for evaluating and sampling
the fitted single-area BET model. It does not modify the fit or run
`doitall.sh`. The tracked `steps/BET/model/final.par` is the MLE starting point.
This is Bayesian-feasibility work with `mfclrtmb`, not the BET 2026 stock
assessment. The official assessment is
[WCPFC-SC22-2026-SA-WP06_Rev01](https://meetings.wcpfc.int/node/32657).
All workflow files are confined to `mfclrtmb-mcmc-2026-08-12`. The launcher
refuses another branch; it does not update `main`, releases, GitHub Pages, or
GitHub Actions.

## Mandatory manual sequence

The workflow has three separate Kflow task codes and no automatic triggers:

1. `gate` installs the requested `mfclrtmb` source only after its ref resolves
   to the supplied full commit SHA. It evaluates the exact `final.par` through
   the standard production `mfclrtmb_fit()` path without optimisation, output
   writing, reporting, or a backend-profile override. It checks the native
   objective, all six native growth gradients, MGC, and the production route.
2. After a person reviews and approves the completed gate, `pilot` runs exactly
   one short SparseNUTS chain in the approved tuna-flow v2.6 image: 20 warmup,
   5 retained draws, tree depth 3, seed 20260813, `adapt_delta = 0.8`, and
   refresh 1. This is a runtime/provenance gate, not a convergence claim.
3. Only after a person separately reviews and approves that completed pilot can
   `chains` submit ten independent production chains: 150 warmup, 100 retained
   draws per chain, tree depth 10, `adapt_delta = 0.8`, refresh 1, 2 CPUs, and
   16 GB per job. Every production chain consumes and verifies both approved
   artifacts before sampling.

The locally proven `stan-adaptive` geometry must be selected explicitly. It is
hard-mapped to `metric = "stan"` and `adapt_stan_metric = TRUE`. No alternative
geometry or geometry job is accepted by this workflow.

## Immutable execution contract

Before any real API call, the launcher requires all of the following:

- a completely clean local worktree on `mfclrtmb-mcmc-2026-08-12`;
- local `HEAD`, `--workflow-sha`, and the exact remote branch head are equal;
- the original model source commit is an ancestor and the five tracked model
  inputs are byte-identical to it;
- the supplied `mfclrtmb` ref resolves to the supplied full commit SHA;
- tuna-flow v2.6 is selected by digest
  `sha256:7b9dc95f535025a42109ac958c4faa3af96592cd19510ac0be15af4478eccf27`;
- the canonical Suva submitter and Linux/X86_64/Docker slot are explicit;
- SparseNUTS 1.0.2 commit
  `2f3f1626219afce68fa2da0d884d4f2dca138117` and StanEstimators 0.3.1
  commit `d19186c7079c6a08160bb77db5b577a203254bf1` are installed from and
  verified against those exact commits.

The job rechecks the checkout SHA, all five input hashes, source tree, image
digest, and package commits. Provenance is copied into the artifacts. Explicit
`input_jobs_override` prevents stale task inputs from being inherited.
`bet.tag.txt` is hashed only as provenance: both gate and sampler require the
parsed FRQ to report `n_tag_groups == 0` and `inputs$tag` to be `NULL`, so that
file cannot be aliased to `bet.tag` or activate a tagging likelihood.

The launcher never blindly overwrites an existing task. It first GETs the task
and reuses it only when the immutable registration is equal; otherwise it
stops. Each job has a deterministic `JOB_KEY` and `submission_key`. The
launcher scans every unfiltered result page plus every result page for both key
tags, reuses exactly one matching job, and stops on duplicates. This also finds
jobs whose key survives only in metadata, environment, or a top-level field. A
local locked, atomically written journal prevents a second POST if the first
response was ambiguous. A timed-out POST is reconciled by GET/list only and is
never automatically repeated.

The journal lock is local, not a distributed Kflow transaction. Kflow does not
enforce a unique database constraint on `JOB_KEY`; therefore never run the same
stage concurrently from different machines or with different state files. Two
truly simultaneous remote launchers could both observe no job before either
POST is visible. A later reconciliation detects that duplicate and aborts, but
cannot retroactively prevent it. Use one launcher/state file and wait for each
command to return before invoking it again. Every real launch requires
`--confirm-sole-launcher` as an explicit attestation of that operating rule; it
is not a substitute for a server-side uniqueness constraint.

## 1. Dry-run and submit the gate

```bash
python3 scripts/launch-mfclrtmb-mcmc.py gate \
  --workflow-sha <full-clean-pushed-branch-head-sha> \
  --mfclrtmb-ref <full-remote-head-or-tag-ref> \
  --mfclrtmb-sha <full-fixed-commit-sha> \
  --dry-run
```

Dry-run only prints the exact registration and job payload and does not create
the state file. Remove `--dry-run` and add `--confirm-sole-launcher` only after
the workflow branch and fixed `mfclrtmb` source are available remotely. Then
inspect the completed gate's
`gate-result.csv`, `growth-gradient-parity.csv`, `input-manifest.csv`, and
source provenance before approving it.

## 2. Dry-run and submit the mandatory short pilot

```bash
python3 scripts/launch-mfclrtmb-mcmc.py pilot \
  --workflow-sha <same-full-workflow-sha> \
  --mfclrtmb-ref <same-full-remote-head-or-tag-ref> \
  --mfclrtmb-sha <same-full-fixed-commit-sha> \
  --approved-gate-job <completed-gate-job-number> \
  --confirm-gate-approved \
  --geometry stan-adaptive \
  --dry-run
```

The launcher verifies the exact gate task, completed status, metadata, and
artifact set before a real pilot submission. Review `pilot-result.csv`,
`chain-status.csv`, `mcmc-run-settings.csv`, draws, logs, and diagnostics.

## 3. Dry-run and submit the ten production chains

```bash
python3 scripts/launch-mfclrtmb-mcmc.py chains \
  --workflow-sha <same-full-workflow-sha> \
  --mfclrtmb-ref <same-full-remote-head-or-tag-ref> \
  --mfclrtmb-sha <same-full-fixed-commit-sha> \
  --approved-gate-job <same-completed-gate-job-number> \
  --confirm-gate-approved \
  --approved-pilot-job <completed-pilot-job-number> \
  --confirm-pilot-approved \
  --geometry stan-adaptive \
  --seed 20260812 \
  --dry-run
```

The geometry must match the approved pilot. Remove `--dry-run` only after both
human approvals. Re-running an unchanged command is idempotent: it reconciles
the deterministic keys rather than submitting duplicates.
