# BET and YFT 2026 single-area models

Fitted single-area MFCL snapshots for the 2026 bigeye tuna (BET) and yellowfin tuna (YFT) assessments.

## Models

| Species | Kflow selector | Frequency data | Fitted input |
|---|---|---|---|
| BET | BET | bet.frq | final.par |
| YFT | YFT | yft.frq | final.par |

Each base job starts from the supplied fitted parameter file and performs one MFCL run to 08.par. It does not repeat the full doitall sequence.

BET and YFT jitter and retrospective diagnostics use their model-specific doitall sequences. Each sequence generates a fresh `00.par` from the species frequency and initial-model files, then runs the complete phase chain. Jitter changes parameters only as they become active; if a fitted `indepvar.rpt` is not retained, mfclkit reconstructs the active mask from the fitted PAR with a no-minimisation MFCL report probe.

Only model inputs are versioned here. Fit reports, Hessian files, profiles, and other generated outputs are produced by Kflow.

## Experimental mfclrtmb work

The isolated `mfclrtmb-mcmc-2026-08-12` branch contains separate manual
native-parity gate, mandatory short tuna-flow v2.6 pilot, and explicitly
approved ten-chain SparseNUTS stages for the BET single-area model. This is
Bayesian-feasibility work, not a stock assessment. See
[`docs/mfclrtmb-mcmc.md`](docs/mfclrtmb-mcmc.md) for the safeguards and dry-run
commands. None of the three tasks has automatic Kflow triggers.

## Fishery labels

Each model carries its own labels.tmp and a generated fishery_map.R. All fisheries have MFCL region 1; historical area text in fishery display names is retained only as a label.

Regenerate a map after changing labels:

    Rscript scripts/build_fishery_map.R steps/BET/model/labels.tmp steps/BET/model/fishery_map.R BET
    Rscript scripts/build_fishery_map.R steps/YFT/model/labels.tmp steps/YFT/model/fishery_map.R YFT

## Kflow

Task: ofp-sam-bet-yft-2026-single-area

The launcher submits the BET and YFT base jobs independently. Each base job receives:

- Hessian: 5 partitions
- Profile: 2 chains
- Jitter: 30 seeds, CV 0.1
- Retrospective: 6 peels
- ASPM: 1 run

BET jitter and retrospective refits start from Phase 1 rather than perturbing or reusing `final.par` as `00.par`.

Self-test is not submitted.

    python3 scripts/register_kflow_task.py \
      --task-name ofp-sam-bet-yft-2026-single-area \
      --repo-full-name PacificCommunity/ofp-sam-bet-yft-2026-single-area \
      --branch BET-YFT-2026

    python3 scripts/launch_bet_yft_single_area.py
