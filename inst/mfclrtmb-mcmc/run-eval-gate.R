#!/usr/bin/env Rscript

options(warn = 1)
suppressPackageStartupMessages(library(mfclrtmb))

required_env <- function(name) {
  value <- trimws(Sys.getenv(name, ""))
  if (!nzchar(value)) stop(name, " is required", call. = FALSE)
  value
}

env_number <- function(name, default) {
  value <- suppressWarnings(as.numeric(Sys.getenv(name, as.character(default))))
  if (length(value) != 1L || !is.finite(value) || value <= 0) {
    stop(name, " must be a positive finite number", call. = FALSE)
  }
  value
}

sha256_file <- function(path) {
  output <- system2("sha256sum", path, stdout = TRUE, stderr = TRUE)
  status <- attr(output, "status")
  if (!is.null(status) && status != 0L) stop("sha256sum failed: ", path, call. = FALSE)
  strsplit(output[[1L]], "[[:space:]]+")[[1L]][[1L]]
}

sha_env <- function(name) {
  value <- tolower(required_env(name))
  if (!grepl("^[0-9a-f]{64}$", value)) {
    stop(name, " must be a SHA-256 digest", call. = FALSE)
  }
  value
}

commit_env <- function(name) {
  value <- tolower(required_env(name))
  if (!grepl("^[0-9a-f]{40}$", value)) {
    stop(name, " must be a full commit SHA", call. = FALSE)
  }
  value
}

par_scalar <- function(path, heading) {
  lines <- readLines(path, warn = FALSE)
  index <- match(heading, trimws(lines))
  if (is.na(index) || index == length(lines)) {
    stop("Missing PAR heading: ", heading, call. = FALSE)
  }
  value <- suppressWarnings(as.numeric(trimws(lines[[index + 1L]])))
  if (length(value) != 1L || !is.finite(value)) {
    stop("Invalid scalar after PAR heading: ", heading, call. = FALSE)
  }
  value
}

indexed_names <- function(value) {
  occurrence <- ave(seq_along(value), value, FUN = seq_along)
  counts <- table(value)
  ifelse(as.integer(counts[value]) > 1L,
         sprintf("%s[%d]", value, occurrence), value)
}

workflow_sha <- commit_env("WORKFLOW_SHA")
model_source_sha <- commit_env("SINGLE_AREA_MODEL_SOURCE_SHA")
fix_ref <- required_env("MFCLRTMB_FIX_REF")
fix_sha <- commit_env("MFCLRTMB_FIX_SHA")
runtime_image <- required_env("KFLOW_RUNTIME_IMAGE")
expected_runtime_image <- paste0(
  "ghcr.io/pacificcommunity/tuna-flow@sha256:",
  "7b9dc95f535025a42109ac958c4faa3af96592cd19510ac0be15af4478eccf27"
)
if (!identical(runtime_image, expected_runtime_image)) {
  stop("KFLOW_RUNTIME_IMAGE is not the approved tuna-flow v2.6 digest", call. = FALSE)
}
if (!identical(required_env("KFLOW_DOCKER_IMAGE"), runtime_image)) {
  stop("The actual Kflow Docker image does not match the approved digest", call. = FALSE)
}
work_root <- required_env("KFLOW_WORK_ROOT")
job_library <- normalizePath(file.path(work_root, "R-library"), mustWork = TRUE)
mfclrtmb_path <- normalizePath(find.package("mfclrtmb"), mustWork = TRUE)
if (!identical(
  mfclrtmb_path,
  normalizePath(file.path(job_library, "mfclrtmb"), mustWork = TRUE)
)) {
  stop("mfclrtmb was not loaded from the isolated verified job library", call. = FALSE)
}

case_dir <- normalizePath(
  Sys.getenv("MFCLRTMB_CASE_DIR", "steps/BET/model"), mustWork = TRUE
)
root_name <- "bet"
final_par <- file.path(case_dir, "final.par")
expected_sha <- c(
  "final.par" = sha_env("MCMC_FINAL_PAR_SHA256"),
  "bet.frq" = sha_env("MCMC_BET_FRQ_SHA256"),
  "bet.ini" = sha_env("MCMC_BET_INI_SHA256"),
  "bet.tag.txt" = sha_env("MCMC_BET_TAG_SHA256"),
  "bet.age_length" = sha_env("MCMC_BET_AGE_LENGTH_SHA256")
)
expected_par_sha <- unname(expected_sha[["final.par"]])
objective_tolerance <- env_number("MFCLRTMB_OBJECTIVE_TOLERANCE", 1e-6)
gradient_tolerance <- env_number("MFCLRTMB_GROWTH_GRADIENT_TOLERANCE", 5e-8)
mgc_tolerance <- env_number("MFCLRTMB_MGC_TOLERANCE", 5e-8)
output_root <- Sys.getenv("KFLOW_OUTPUT_ROOT", file.path(getwd(), "outputs"))
result_dir <- file.path(output_root, "mfclrtmb-eval-gate")
dir.create(result_dir, recursive = TRUE, showWarnings = FALSE)

input_files <- file.path(
  case_dir,
  names(expected_sha)
)
if (any(!file.exists(input_files))) {
  stop("The fixed BET single-area input set is incomplete", call. = FALSE)
}
input_manifest <- data.frame(
  file = basename(input_files),
  sha256 = vapply(input_files, sha256_file, character(1L)),
  expected_sha256 = unname(expected_sha[basename(input_files)]),
  bytes = as.numeric(file.info(input_files)$size),
  stringsAsFactors = FALSE
)
input_manifest$matches_expected <-
  input_manifest$sha256 == input_manifest$expected_sha256
utils::write.csv(input_manifest, file.path(result_dir, "input-manifest.csv"), row.names = FALSE)
if (!all(input_manifest$matches_expected)) {
  bad <- input_manifest$file[!input_manifest$matches_expected]
  stop("Frozen BET input hash mismatch: ", paste(bad, collapse = ", "), call. = FALSE)
}

source_provenance_path <- file.path(
  work_root, "mfclrtmb-source-provenance.txt"
)
if (!file.exists(source_provenance_path)) {
  stop("Verified mfclrtmb source provenance is missing", call. = FALSE)
}
source_provenance_lines <- readLines(source_provenance_path, warn = FALSE)
source_provenance <- setNames(
  sub("^[^=]+=", "", source_provenance_lines),
  sub("=.*$", "", source_provenance_lines)
)
source_tree <- unname(source_provenance[["source_tree"]])
source_checks <- c(
  identical(tolower(unname(source_provenance[["workflow_commit"]])), workflow_sha),
  identical(tolower(unname(source_provenance[["model_source_commit"]])), model_source_sha),
  identical(unname(source_provenance[["runtime_image"]]), runtime_image),
  identical(unname(source_provenance[["actual_runtime_image"]]), runtime_image),
  identical(unname(source_provenance[["requested_ref"]]), fix_ref),
  identical(tolower(unname(source_provenance[["resolved_commit"]])), fix_sha),
  !is.null(source_tree) && grepl("^[0-9a-f]{40}$", source_tree)
)
if (!all(source_checks)) {
  stop("Verified mfclrtmb source provenance does not match the gate", call. = FALSE)
}
provenance_copied <- file.copy(
  source_provenance_path,
  file.path(result_dir, "mfclrtmb-source-provenance.txt"),
  overwrite = FALSE
)
if (!isTRUE(provenance_copied)) {
  stop("Could not copy immutable mfclrtmb provenance into gate outputs", call. = FALSE)
}

native_objective <- par_scalar(final_par, "# Objective function value")
native_growth <- c(
  seasonal_growth_sv21 = -2.0070851e-7,
  `vb_coff[1]` = 5.2261549e-6,
  `vb_coff[2]` = -2.1324786e-5,
  `vb_coff[3]` = 2.6738385e-5,
  `var_coff[1]` = -2.2643749e-6,
  `var_coff[2]` = 1.5046607e-5
)
native_mgc <- 8.8984899e-5

inputs <- read_mfcl_inputs(case_dir, root = root_name, par = final_par)
if (!identical(as.integer(inputs$frq$dimensions$n_tag_groups), 0L) ||
    !is.null(inputs$tag)) {
  stop(
    "Single-area BET must parse with n_tag_groups=0 and inputs$tag=NULL; ",
    "bet.tag.txt is provenance-only and must not be aliased or activated",
    call. = FALSE
  )
}
started <- Sys.time()
timing <- system.time({
  fit <- mfclrtmb_fit(
    inputs = inputs,
    root = root_name,
    par = final_par,
    run_optimization = FALSE,
    write_outputs = FALSE,
    build_report = FALSE,
    run_sdreport = FALSE,
    openmp_threads = 1L,
    verbose = TRUE
  )
})
finished <- Sys.time()

parameter <- as.numeric(fit$par)
parameter_names <- names(fit$par)
gradient <- as.numeric(fit$gradient)
names(gradient) <- indexed_names(parameter_names)
expected_runs <- list(
  lengths = c(1L, 3L, 2L, 151L, 75L),
  values = c(
    "seasonal_growth_sv21", "vb_coff", "var_coff",
    "selectivity_coff", "orth_recr_all"
  )
)
runs <- unclass(rle(parameter_names))
if (length(parameter) != 232L || !identical(runs, expected_runs)) {
  stop("Unexpected active-parameter layout in the standard production fit", call. = FALSE)
}
if (!all(is.finite(parameter)) || !all(is.finite(gradient))) {
  stop("The standard production evaluation returned non-finite values", call. = FALSE)
}

growth_gradient <- gradient[names(native_growth)]
growth_difference <- growth_gradient - native_growth
rtmb_mgc <- max(abs(gradient))
objective_difference <- as.numeric(fit$objective) - native_objective
route <- fit$rtmb$data
production_route <- isTRUE(route$dynamic_state_catch_objective_cpp) &&
  isTRUE(route$dynamic_state_catch_comp_cpp) &&
  !isTRUE(route$fixed_effect_state_objective_atomic)
passed <- abs(objective_difference) <= objective_tolerance &&
  max(abs(growth_difference)) <= gradient_tolerance &&
  abs(rtmb_mgc - native_mgc) <= mgc_tolerance &&
  production_route

gradient_parity <- data.frame(
  name = names(native_growth),
  mfclrtmb = as.numeric(growth_gradient),
  native_mfcl = as.numeric(native_growth),
  difference = as.numeric(growth_difference),
  tolerance = gradient_tolerance,
  passed = abs(growth_difference) <= gradient_tolerance,
  stringsAsFactors = FALSE
)
utils::write.csv(
  gradient_parity,
  file.path(result_dir, "growth-gradient-parity.csv"),
  row.names = FALSE
)

status_lines <- readLines("/proc/self/status", warn = FALSE)
peak_line <- grep("^VmHWM:", status_lines, value = TRUE)
peak_rss_kb <- if (length(peak_line)) {
  as.numeric(sub("^VmHWM:[[:space:]]*([0-9]+).*$", "\\1", peak_line[[1L]]))
} else {
  NA_real_
}
package_path <- normalizePath(find.package("mfclrtmb"), mustWork = TRUE)
package_so <- file.path(package_path, "libs", "mfclrtmb.so")

gate <- data.frame(
  status = if (passed) "passed" else "failed",
  workflow_role = "mfclrtmb-eval-gate",
  workflow_sha = workflow_sha,
  single_area_model_source_sha = model_source_sha,
  final_par_sha256 = expected_par_sha,
  bet_frq_sha256 = unname(expected_sha[["bet.frq"]]),
  bet_ini_sha256 = unname(expected_sha[["bet.ini"]]),
  bet_tag_sha256 = unname(expected_sha[["bet.tag.txt"]]),
  bet_age_length_sha256 = unname(expected_sha[["bet.age_length"]]),
  runtime_image = runtime_image,
  frq_n_tag_groups = as.integer(inputs$frq$dimensions$n_tag_groups),
  tag_input_is_null = is.null(inputs$tag),
  mfclrtmb_fix_repo = Sys.getenv("MFCLRTMB_FIX_REPO", "PacificCommunity/ofp-sam-mfclrtmb"),
  mfclrtmb_fix_ref = fix_ref,
  mfclrtmb_fix_sha = fix_sha,
  mfclrtmb_source_tree = source_tree,
  mfclrtmb_version = as.character(utils::packageVersion("mfclrtmb")),
  mfclrtmb_shared_object_sha256 = sha256_file(package_so),
  native_objective = native_objective,
  mfclrtmb_objective = as.numeric(fit$objective),
  objective_difference = objective_difference,
  objective_tolerance = objective_tolerance,
  native_mgc = native_mgc,
  mfclrtmb_mgc = rtmb_mgc,
  mgc_difference = rtmb_mgc - native_mgc,
  mgc_tolerance = mgc_tolerance,
  maximum_growth_gradient_difference = max(abs(growth_difference)),
  growth_gradient_tolerance = gradient_tolerance,
  parameter_count = length(parameter),
  production_route = production_route,
  run_optimization = FALSE,
  write_outputs = FALSE,
  build_report = FALSE,
  run_sdreport = FALSE,
  backend_profile_override = FALSE,
  openmp_threads = 1L,
  kflow_job_number = Sys.getenv("KFLOW_JOB_NUMBER", ""),
  kflow_job_id = Sys.getenv("KFLOW_JOB_ID", ""),
  started_utc = format(started, tz = "UTC", usetz = TRUE),
  finished_utc = format(finished, tz = "UTC", usetz = TRUE),
  elapsed_seconds = unname(timing[["elapsed"]]),
  peak_rss_kb = peak_rss_kb,
  stringsAsFactors = FALSE
)
utils::write.csv(gate, file.path(result_dir, "gate-result.csv"), row.names = FALSE)
saveRDS(
  list(
    gate = gate,
    input_manifest = input_manifest,
    growth_gradient_parity = gradient_parity,
    parameter = setNames(parameter, indexed_names(parameter_names)),
    gradient = gradient
  ),
  file.path(result_dir, "gate-details.rds"),
  version = 3L
)

print(gate)
print(gradient_parity, digits = 17L, row.names = FALSE)
if (!passed) stop("Native MFCL/mfclrtmb evaluation gate failed", call. = FALSE)
