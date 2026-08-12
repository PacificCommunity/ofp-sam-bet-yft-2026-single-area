#!/usr/bin/env bash
set -euo pipefail

work_root="${KFLOW_WORK_ROOT:-${PWD}/work}"
output_root="${KFLOW_OUTPUT_ROOT:-${OUTPUT_DIR:-${PWD}/outputs}}"
library_root="${work_root}/R-library"

mkdir -p "$work_root" "$output_root" "$library_root"
export KFLOW_WORK_ROOT="$work_root"
export KFLOW_OUTPUT_ROOT="$output_root"
export R_LIBS_USER="$library_root"
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1
export NUMEXPR_NUM_THREADS=1

bash inst/mfclrtmb-mcmc/install-pinned-mfclrtmb.sh
Rscript --vanilla inst/mfclrtmb-mcmc/run-eval-gate.R
