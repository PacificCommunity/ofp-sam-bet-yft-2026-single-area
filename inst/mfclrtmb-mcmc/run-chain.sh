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

sparse_nuts_ref="${SPARSENUTS_REF:-}"
stan_estimators_ref="${STANESTIMATORS_REF:-}"
sparse_nuts_version="${SPARSENUTS_VERSION:-}"
stan_estimators_version="${STANESTIMATORS_VERSION:-}"
if [[ ! "$sparse_nuts_ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "[mfclrtmb-mcmc] SPARSENUTS_REF must be a full 40-character commit SHA." >&2
  exit 2
fi
if [[ ! "$stan_estimators_ref" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "[mfclrtmb-mcmc] STANESTIMATORS_REF must be a full 40-character commit SHA." >&2
  exit 2
fi
if [[ -z "$sparse_nuts_version" || -z "$stan_estimators_version" ]]; then
  echo "[mfclrtmb-mcmc] Exact sampler package versions are required." >&2
  exit 2
fi
export SPARSENUTS_REF="${sparse_nuts_ref,,}"
export STANESTIMATORS_REF="${stan_estimators_ref,,}"

Rscript --vanilla - <<'RS'
repos <- c(
  andrjohns = "https://andrjohns.r-universe.dev",
  stan = "https://stan-dev.r-universe.dev",
  CRAN = "https://cloud.r-project.org"
)
options(repos = repos)
if (!requireNamespace("remotes", quietly = TRUE)) {
  install.packages("remotes", repos = repos[["CRAN"]])
}
library_root <- normalizePath(Sys.getenv("R_LIBS_USER"), mustWork = TRUE)
.libPaths(unique(c(library_root, .libPaths())))

install_exact <- function(package, repository, ref, version) {
  installed_ref <- if (requireNamespace(package, quietly = TRUE)) {
    tolower(as.character(utils::packageDescription(package, fields = "RemoteSha")))
  } else {
    ""
  }
  installed_version <- if (requireNamespace(package, quietly = TRUE)) {
    as.character(utils::packageVersion(package))
  } else {
    ""
  }
  installed_path <- if (requireNamespace(package, quietly = TRUE)) {
    normalizePath(find.package(package), mustWork = TRUE)
  } else {
    ""
  }
  expected_path <- file.path(library_root, package)
  if (!identical(installed_ref, ref) || !identical(installed_version, version) ||
      !identical(installed_path, normalizePath(expected_path, mustWork = FALSE))) {
    remotes::install_github(
      paste0(repository, "@", ref),
      dependencies = NA,
      upgrade = "never",
      lib = library_root
    )
  }
  description <- utils::packageDescription(package, lib.loc = library_root)
  actual_ref <- tolower(as.character(description$RemoteSha))
  actual_version <- as.character(description$Version)
  actual_path <- normalizePath(find.package(package, lib.loc = library_root), mustWork = TRUE)
  stopifnot(
    identical(actual_ref, ref),
    identical(actual_version, version),
    identical(actual_path, normalizePath(expected_path, mustWork = TRUE))
  )
  c(package = package, version = actual_version, commit = actual_ref, path = actual_path)
}

stan <- install_exact(
  "StanEstimators",
  "andrjohns/StanEstimators",
  tolower(Sys.getenv("STANESTIMATORS_REF")),
  Sys.getenv("STANESTIMATORS_VERSION")
)
sparse <- install_exact(
  "SparseNUTS",
  "noaa-afsc/SparseNUTS",
  tolower(Sys.getenv("SPARSENUTS_REF")),
  Sys.getenv("SPARSENUTS_VERSION")
)

verify_exact <- function(package, ref, version) {
  description <- utils::packageDescription(package, lib.loc = library_root)
  actual <- c(
    package = package,
    version = as.character(description$Version),
    commit = tolower(as.character(description$RemoteSha)),
    path = normalizePath(find.package(package, lib.loc = library_root), mustWork = TRUE)
  )
  stopifnot(
    identical(actual[["commit"]], ref),
    identical(actual[["version"]], version),
    identical(
      actual[["path"]],
      normalizePath(file.path(library_root, package), mustWork = TRUE)
    )
  )
  actual
}
stan <- verify_exact(
  "StanEstimators",
  tolower(Sys.getenv("STANESTIMATORS_REF")),
  Sys.getenv("STANESTIMATORS_VERSION")
)
sparse <- verify_exact(
  "SparseNUTS",
  tolower(Sys.getenv("SPARSENUTS_REF")),
  Sys.getenv("SPARSENUTS_VERSION")
)
provenance <- rbind(stan, sparse)
utils::write.csv(
  as.data.frame(provenance, stringsAsFactors = FALSE),
  file.path(Sys.getenv("KFLOW_WORK_ROOT"), "sampler-package-provenance.csv"),
  row.names = FALSE
)
RS

Rscript --vanilla inst/mfclrtmb-mcmc/run-chain.R
