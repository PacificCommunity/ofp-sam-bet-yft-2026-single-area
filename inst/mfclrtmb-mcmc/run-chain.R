#!/usr/bin/env Rscript

options(warn = 1)
suppressPackageStartupMessages(library(mfclrtmb))

required_env <- function(name) {
  value <- trimws(Sys.getenv(name, ""))
  if (!nzchar(value)) stop(name, " is required", call. = FALSE)
  value
}

env_integer <- function(name, default) {
  value <- suppressWarnings(as.integer(Sys.getenv(name, as.character(default))))
  if (length(value) != 1L || !is.finite(value) || value < 1L) {
    stop(name, " must be a positive integer", call. = FALSE)
  }
  value
}

env_nonnegative_integer <- function(name, default) {
  value <- suppressWarnings(as.integer(Sys.getenv(name, as.character(default))))
  if (length(value) != 1L || !is.finite(value) || value < 0L) {
    stop(name, " must be a non-negative integer", call. = FALSE)
  }
  value
}

env_number <- function(name, default) {
  value <- suppressWarnings(as.numeric(Sys.getenv(name, as.character(default))))
  if (length(value) != 1L || !is.finite(value)) {
    stop(name, " must be a finite number", call. = FALSE)
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

env_boolean <- function(name) {
  value <- tolower(required_env(name))
  if (!value %in% c("true", "false")) {
    stop(name, " must be exactly true or false", call. = FALSE)
  }
  identical(value, "true")
}

reference_matches <- function(reference, record) {
  reference <- sub("^#", "", trimws(reference))
  candidates <- sub(
    "^#", "",
    trimws(c(as.character(record$kflow_job_number), as.character(record$kflow_job_id)))
  )
  nzchar(reference) && reference %in% candidates[nzchar(candidates)]
}

same_reference <- function(left, right) {
  identical(sub("^#", "", trimws(left)), sub("^#", "", trimws(right)))
}

indexed_names <- function(value) {
  occurrence <- ave(seq_along(value), value, FUN = seq_along)
  counts <- table(value)
  ifelse(as.integer(counts[value]) > 1L,
         sprintf("%s[%d]", value, occurrence), value)
}

approval <- required_env("MCMC_GATE_APPROVAL")
if (!identical(approval, "approved-after-review")) {
  stop("MCMC_GATE_APPROVAL must record explicit post-gate approval", call. = FALSE)
}
approved_gate_job <- required_env("MCMC_APPROVED_GATE_JOB")
run_kind <- match.arg(tolower(required_env("MCMC_RUN_KIND")), c("pilot", "production"))
pilot_approval <- Sys.getenv("MCMC_PILOT_APPROVAL", "")
approved_pilot_job <- Sys.getenv("MCMC_APPROVED_PILOT_JOB", "")
if (identical(run_kind, "production")) {
  if (!identical(pilot_approval, "approved-after-review")) {
    stop("Production requires explicit post-pilot approval", call. = FALSE)
  }
  approved_pilot_job <- required_env("MCMC_APPROVED_PILOT_JOB")
} else if (nzchar(pilot_approval) || nzchar(approved_pilot_job)) {
  stop("The short pilot cannot approve or depend on itself", call. = FALSE)
}
workflow_sha <- commit_env("WORKFLOW_SHA")
model_source_sha <- commit_env("SINGLE_AREA_MODEL_SOURCE_SHA")
fix_ref <- required_env("MFCLRTMB_FIX_REF")
fix_sha <- commit_env("MFCLRTMB_FIX_SHA")
expected_sha <- c(
  "final.par" = sha_env("MCMC_FINAL_PAR_SHA256"),
  "bet.frq" = sha_env("MCMC_BET_FRQ_SHA256"),
  "bet.ini" = sha_env("MCMC_BET_INI_SHA256"),
  "bet.tag.txt" = sha_env("MCMC_BET_TAG_SHA256"),
  "bet.age_length" = sha_env("MCMC_BET_AGE_LENGTH_SHA256")
)
expected_par_sha <- unname(expected_sha[["final.par"]])
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
sparse_nuts_sha <- commit_env("SPARSENUTS_REF")
sparse_nuts_version <- required_env("SPARSENUTS_VERSION")
stan_estimators_sha <- commit_env("STANESTIMATORS_REF")
stan_estimators_version <- required_env("STANESTIMATORS_VERSION")
if (!identical(sparse_nuts_sha, "2f3f1626219afce68fa2da0d884d4f2dca138117") ||
    !identical(sparse_nuts_version, "1.0.2") ||
    !identical(stan_estimators_sha, "d19186c7079c6a08160bb77db5b577a203254bf1") ||
    !identical(stan_estimators_version, "0.3.1")) {
  stop("Sampler package pins do not match the validated workflow", call. = FALSE)
}
work_root <- required_env("KFLOW_WORK_ROOT")
job_library <- normalizePath(file.path(work_root, "R-library"), mustWork = TRUE)
verify_installed_package <- function(package, version, commit) {
  description <- utils::packageDescription(package, lib.loc = job_library)
  path <- normalizePath(find.package(package, lib.loc = job_library), mustWork = TRUE)
  checks <- c(
    identical(as.character(description$Version), version),
    identical(tolower(as.character(description$RemoteSha)), commit),
    identical(path, normalizePath(file.path(job_library, package), mustWork = TRUE))
  )
  if (!all(checks)) stop("Installed ", package, " does not match its exact pin", call. = FALSE)
  invisible(path)
}
verify_installed_package("SparseNUTS", sparse_nuts_version, sparse_nuts_sha)
verify_installed_package("StanEstimators", stan_estimators_version, stan_estimators_sha)
mfclrtmb_path <- normalizePath(find.package("mfclrtmb"), mustWork = TRUE)
if (!identical(
  mfclrtmb_path,
  normalizePath(file.path(job_library, "mfclrtmb"), mustWork = TRUE)
)) {
  stop("mfclrtmb was not loaded from the isolated verified job library", call. = FALSE)
}
geometry <- tolower(required_env("MCMC_GEOMETRY"))
if (!identical(geometry, "stan-adaptive")) {
  stop("This workflow only permits the locally validated stan-adaptive geometry", call. = FALSE)
}
sampler_metric <- "stan"
adapt_stan_metric <- TRUE

chain_id <- env_integer("MCMC_CHAIN_ID", 1L)
total_chains <- env_integer("MCMC_TOTAL_CHAINS", 10L)
warmup <- env_integer("MCMC_WARMUP", 150L)
samples <- env_integer("MCMC_SAMPLES", 100L)
refresh <- env_nonnegative_integer("MCMC_REFRESH", 1L)
adapt_delta <- env_number("MCMC_ADAPT_DELTA", 0.8)
max_treedepth <- env_integer("MCMC_MAX_TREEDEPTH", 10L)
base_seed <- env_integer("MCMC_SEED", 20260812L)
seed <- base_seed + chain_id - 1L
skip_monitor <- env_boolean("MCMC_SKIP_MONITOR")
if (chain_id > total_chains) stop("MCMC_CHAIN_ID exceeds MCMC_TOTAL_CHAINS", call. = FALSE)
if (identical(run_kind, "pilot")) {
  pilot_settings_ok <- chain_id == 1L && total_chains == 1L && warmup == 20L &&
    samples == 5L && refresh == 1L && max_treedepth == 3L &&
    base_seed == 20260813L && skip_monitor
  if (!pilot_settings_ok) {
    stop(
      "The v2.6 pilot requires one chain, 20 warmup, 5 samples, depth 3, ",
      "seed 20260813, refresh 1, and skip_monitor=true",
      call. = FALSE
    )
  }
} else {
  production_settings_ok <- total_chains == 10L && warmup == 150L &&
    samples == 100L && refresh == 1L && max_treedepth == 10L && !skip_monitor
  if (!production_settings_ok) {
    stop(
      "Production fanout requires 10 chains, 150 warmup, 100 samples, ",
      "depth 10, refresh 1, and skip_monitor=false",
      call. = FALSE
    )
  }
}
if (adapt_delta != 0.8) stop("The pilot and production fanout require adapt_delta=0.8", call. = FALSE)

input_root <- Sys.getenv(
  "KFLOW_INPUT_ROOT",
  Sys.getenv("KFLOW_INPUT_DIR", file.path(getwd(), "inputs"))
)
case_dir <- normalizePath(
  Sys.getenv("MFCLRTMB_CASE_DIR", "steps/BET/model"), mustWork = TRUE
)
output_root <- Sys.getenv("KFLOW_OUTPUT_ROOT", file.path(getwd(), "outputs"))
chain_key <- if (identical(run_kind, "pilot")) "pilot" else sprintf("chain-%02d", chain_id)
result_dir <- file.path(
  output_root,
  if (identical(run_kind, "pilot")) "mfclrtmb-mcmc-pilot" else "mfclrtmb-mcmc",
  chain_key
)
mcmc_dir <- file.path(result_dir, "mcmc")
dir.create(mcmc_dir, recursive = TRUE, showWarnings = FALSE)

source_provenance_path <- file.path(work_root, "mfclrtmb-source-provenance.txt")
sampler_provenance_path <- file.path(work_root, "sampler-package-provenance.csv")
if (!file.exists(source_provenance_path) || !file.exists(sampler_provenance_path)) {
  stop("Verified source or sampler package provenance is missing", call. = FALSE)
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
  stop("Verified mfclrtmb source provenance does not match this job", call. = FALSE)
}
sampler_provenance <- utils::read.csv(
  sampler_provenance_path, stringsAsFactors = FALSE
)
if (nrow(sampler_provenance) != 2L ||
    !setequal(sampler_provenance$package, c("StanEstimators", "SparseNUTS"))) {
  stop("Sampler package provenance is incomplete", call. = FALSE)
}
sampler_by_package <- split(sampler_provenance, sampler_provenance$package)
sampler_checks <- c(
  identical(tolower(sampler_by_package$StanEstimators$commit[[1L]]), stan_estimators_sha),
  identical(sampler_by_package$StanEstimators$version[[1L]], stan_estimators_version),
  identical(tolower(sampler_by_package$SparseNUTS$commit[[1L]]), sparse_nuts_sha),
  identical(sampler_by_package$SparseNUTS$version[[1L]], sparse_nuts_version)
)
if (!all(sampler_checks)) {
  stop("Installed sampler packages do not match the exact pins", call. = FALSE)
}
copied <- c(
  file.copy(
    source_provenance_path,
    file.path(result_dir, "mfclrtmb-source-provenance.txt"),
    overwrite = FALSE
  ),
  file.copy(
    sampler_provenance_path,
    file.path(result_dir, "sampler-package-provenance.csv"),
    overwrite = FALSE
  )
)
if (!all(copied)) stop("Could not copy immutable provenance into outputs", call. = FALSE)

gate_files <- list.files(
  input_root,
  pattern = "^gate-result[.]csv$",
  recursive = TRUE,
  full.names = TRUE
)
if (length(gate_files) != 1L) {
  stop(
    "Exactly one approved mfclrtmb gate artifact must be attached; found ",
    length(gate_files),
    call. = FALSE
  )
}
gate <- utils::read.csv(gate_files[[1L]], stringsAsFactors = FALSE)
if (nrow(gate) != 1L || !identical(gate$status[[1L]], "passed") ||
    !identical(gate$workflow_role[[1L]], "mfclrtmb-eval-gate")) {
  stop("The attached evaluation gate did not pass", call. = FALSE)
}
gate_checks <- c(
  identical(tolower(gate$workflow_sha[[1L]]), workflow_sha),
  identical(gate$single_area_model_source_sha[[1L]], model_source_sha),
  identical(tolower(gate$final_par_sha256[[1L]]), expected_par_sha),
  identical(tolower(gate$bet_frq_sha256[[1L]]), unname(expected_sha[["bet.frq"]])),
  identical(tolower(gate$bet_ini_sha256[[1L]]), unname(expected_sha[["bet.ini"]])),
  identical(tolower(gate$bet_tag_sha256[[1L]]), unname(expected_sha[["bet.tag.txt"]])),
  identical(
    tolower(gate$bet_age_length_sha256[[1L]]),
    unname(expected_sha[["bet.age_length"]])
  ),
  identical(gate$runtime_image[[1L]], runtime_image),
  gate$frq_n_tag_groups[[1L]] == 0L,
  isTRUE(gate$tag_input_is_null[[1L]]),
  identical(gate$mfclrtmb_fix_ref[[1L]], fix_ref),
  identical(tolower(gate$mfclrtmb_fix_sha[[1L]]), fix_sha),
  identical(tolower(gate$mfclrtmb_source_tree[[1L]]), tolower(source_tree)),
  reference_matches(approved_gate_job, gate),
  isTRUE(gate$production_route[[1L]])
)
if (!all(gate_checks)) {
  stop("The attached gate provenance does not match this chain", call. = FALSE)
}

pilot_files <- list.files(
  input_root,
  pattern = "^pilot-result[.]csv$",
  recursive = TRUE,
  full.names = TRUE
)
if (identical(run_kind, "pilot") && length(pilot_files) != 0L) {
  stop("The short pilot must not consume a prior pilot artifact", call. = FALSE)
}
if (identical(run_kind, "production")) {
  if (length(pilot_files) != 1L) {
    stop(
      "Exactly one explicitly approved short-pilot artifact must be attached; found ",
      length(pilot_files),
      call. = FALSE
    )
  }
  pilot <- utils::read.csv(pilot_files[[1L]], stringsAsFactors = FALSE)
  if (nrow(pilot) != 1L || !identical(pilot$status[[1L]], "passed") ||
      !identical(pilot$workflow_role[[1L]], "mfclrtmb-mcmc-pilot")) {
    stop("The attached short-pilot artifact did not pass", call. = FALSE)
  }
  pilot_checks <- c(
    reference_matches(approved_pilot_job, pilot),
    same_reference(pilot$approved_gate_job[[1L]], approved_gate_job),
    identical(tolower(pilot$workflow_sha[[1L]]), workflow_sha),
    identical(pilot$single_area_model_source_sha[[1L]], model_source_sha),
    identical(tolower(pilot$final_par_sha256[[1L]]), expected_par_sha),
    identical(pilot$runtime_image[[1L]], runtime_image),
    pilot$frq_n_tag_groups[[1L]] == 0L,
    isTRUE(pilot$tag_input_is_null[[1L]]),
    identical(pilot$mfclrtmb_fix_ref[[1L]], fix_ref),
    identical(tolower(pilot$mfclrtmb_fix_sha[[1L]]), fix_sha),
    identical(tolower(pilot$mfclrtmb_source_tree[[1L]]), tolower(source_tree)),
    identical(pilot$sparse_nuts_version[[1L]], sparse_nuts_version),
    identical(tolower(pilot$sparse_nuts_sha[[1L]]), sparse_nuts_sha),
    identical(pilot$stan_estimators_version[[1L]], stan_estimators_version),
    identical(tolower(pilot$stan_estimators_sha[[1L]]), stan_estimators_sha),
    identical(pilot$geometry[[1L]], geometry),
    pilot$chain_id[[1L]] == 1L,
    pilot$total_chains[[1L]] == 1L,
    pilot$warmup[[1L]] == 20L,
    pilot$samples[[1L]] == 5L,
    pilot$refresh[[1L]] == 1L,
    pilot$adapt_delta[[1L]] == 0.8,
    pilot$max_treedepth[[1L]] == 3L,
    pilot$base_seed[[1L]] == 20260813L,
    isTRUE(pilot$skip_monitor[[1L]]),
    pilot$retained_draws[[1L]] == 5L,
    pilot$mle_start_max_abs_difference[[1L]] == 0
  )
  if (!all(pilot_checks)) {
    stop("The attached short-pilot provenance does not authorize production", call. = FALSE)
  }
}

input_files <- file.path(case_dir, names(expected_sha))
if (any(!file.exists(input_files))) {
  stop("The fixed BET single-area input set is incomplete", call. = FALSE)
}
observed_sha <- vapply(input_files, sha256_file, character(1L))
names(observed_sha) <- basename(input_files)
if (!identical(unname(observed_sha[names(expected_sha)]), unname(expected_sha))) {
  stop("The chain inputs do not match the five frozen input hashes", call. = FALSE)
}
final_par <- file.path(case_dir, "final.par")
inputs <- read_mfcl_inputs(case_dir, root = "bet", par = final_par)
if (!identical(as.integer(inputs$frq$dimensions$n_tag_groups), 0L) ||
    !is.null(inputs$tag)) {
  stop(
    "Single-area BET must parse with n_tag_groups=0 and inputs$tag=NULL; ",
    "bet.tag.txt is provenance-only and must not be aliased or activated",
    call. = FALSE
  )
}
started <- Sys.time()
fit_timing <- system.time({
  fit <- mfclrtmb_fit(
    inputs = inputs,
    root = "bet",
    par = final_par,
    run_optimization = FALSE,
    write_outputs = FALSE,
    build_report = FALSE,
    run_sdreport = FALSE,
    openmp_threads = 1L,
    verbose = TRUE
  )
})
parameter_names <- names(fit$par)
if (length(fit$par) != 232L || !all(is.finite(fit$gradient))) {
  stop("Unexpected standard production evaluation before sampling", call. = FALSE)
}
gate_objective <- as.numeric(gate$mfclrtmb_objective[[1L]])
objective_tolerance <- as.numeric(gate$objective_tolerance[[1L]])
if (!is.finite(gate_objective) || !is.finite(objective_tolerance) ||
    abs(as.numeric(fit$objective) - gate_objective) > objective_tolerance) {
  stop("Chain objective does not reproduce the approved gate", call. = FALSE)
}

geometry_source <- "StanEstimators warmup adaptation"
message("[mfclrtmb-mcmc] Using the approved adaptive StanEstimators SparseNUTS geometry")

settings <- data.frame(
  run_kind = run_kind,
  workflow_sha = workflow_sha,
  runtime_image = runtime_image,
  approved_gate_job = approved_gate_job,
  approved_gate_artifact = gate_files[[1L]],
  approved_pilot_job = approved_pilot_job,
  approved_pilot_artifact = if (identical(run_kind, "production")) pilot_files[[1L]] else "",
  chain_id = chain_id,
  total_chains = total_chains,
  warmup = warmup,
  samples = samples,
  refresh = refresh,
  adapt_delta = adapt_delta,
  max_treedepth = max_treedepth,
  seed = seed,
  base_seed = base_seed,
  skip_monitor = skip_monitor,
  init = "last.par.best",
  geometry = geometry,
  geometry_source = geometry_source,
  sampler_metric = sampler_metric,
  adapt_stan_metric = adapt_stan_metric,
  mfclrtmb_fix_ref = fix_ref,
  mfclrtmb_fix_sha = fix_sha,
  mfclrtmb_source_tree = source_tree,
  sparse_nuts_sha = sparse_nuts_sha,
  sparse_nuts_version = sparse_nuts_version,
  stan_estimators_sha = stan_estimators_sha,
  stan_estimators_version = stan_estimators_version,
  final_par_sha256 = expected_par_sha,
  bet_frq_sha256 = unname(expected_sha[["bet.frq"]]),
  bet_ini_sha256 = unname(expected_sha[["bet.ini"]]),
  bet_tag_sha256 = unname(expected_sha[["bet.tag.txt"]]),
  bet_age_length_sha256 = unname(expected_sha[["bet.age_length"]]),
  frq_n_tag_groups = as.integer(inputs$frq$dimensions$n_tag_groups),
  tag_input_is_null = is.null(inputs$tag),
  run_optimization = FALSE,
  backend_profile_override = FALSE,
  openmp_threads = 1L,
  stringsAsFactors = FALSE
)
utils::write.csv(settings, file.path(result_dir, "mcmc-run-settings.csv"), row.names = FALSE)

sampling_timing <- system.time({
  snuts <- mfclrtmb_snuts(
    fit,
    num_samples = samples,
    num_warmup = warmup,
    chains = 1L,
    cores = 1L,
    parallel_chains = FALSE,
    thin = 1L,
    seed = seed,
    metric = sampler_metric,
    adapt_stan_metric = adapt_stan_metric,
    adapt_delta = adapt_delta,
    max_treedepth = max_treedepth,
    init = "last.par.best",
    laplace = FALSE,
    skip_optimization = TRUE,
    skip_cor = TRUE,
    skip_monitor = skip_monitor,
    globals = NULL,
    refresh = refresh,
    print = TRUE,
    max_report_draws = samples,
    postprocess = FALSE,
    snuts_args = list(),
    verbose = TRUE,
    output_dir = mcmc_dir,
    save = TRUE
  )
})

initial_values <- snuts$snuts_fit$inits
if (!is.list(initial_values) || length(initial_values) != 1L) {
  stop("SparseNUTS did not retain one chain initial vector", call. = FALSE)
}
initial <- as.numeric(initial_values[[1L]])
mle <- as.numeric(fit$par)
if (length(initial) != length(mle) || max(abs(initial - mle)) != 0) {
  stop("SparseNUTS did not start exactly at the final.par MLE", call. = FALSE)
}
draws <- snuts$draws
if (!is.matrix(draws) || !identical(dim(draws), c(samples, length(mle))) ||
    !all(is.finite(draws))) {
  stop("SparseNUTS returned an invalid retained-draw matrix", call. = FALSE)
}

draw_index <- data.frame(
  chain = chain_id,
  draw = seq_len(samples),
  post_warmup_iteration = seq_len(samples),
  stringsAsFactors = FALSE
)
dir.create(file.path(mcmc_dir, "draws"), recursive = TRUE, showWarnings = FALSE)
utils::write.csv(draw_index, file.path(mcmc_dir, "draws", "draw-index.csv"), row.names = FALSE)

status_lines <- readLines("/proc/self/status", warn = FALSE)
peak_line <- grep("^VmHWM:", status_lines, value = TRUE)
peak_rss_kb <- if (length(peak_line)) {
  as.numeric(sub("^VmHWM:[[:space:]]*([0-9]+).*$", "\\1", peak_line[[1L]]))
} else {
  NA_real_
}
status <- data.frame(
  status = "completed",
  run_kind = run_kind,
  chain_id = chain_id,
  retained_draws = nrow(draws),
  parameter_count = ncol(draws),
  mle_start_max_abs_difference = max(abs(initial - mle)),
  mfclrtmb_objective = as.numeric(fit$objective),
  mfclrtmb_mgc = max(abs(fit$gradient)),
  fit_elapsed_seconds = unname(fit_timing[["elapsed"]]),
  sampling_elapsed_seconds = unname(sampling_timing[["elapsed"]]),
  total_elapsed_seconds = as.numeric(difftime(Sys.time(), started, units = "secs")),
  peak_rss_kb = peak_rss_kb,
  stringsAsFactors = FALSE
)
utils::write.csv(status, file.path(result_dir, "chain-status.csv"), row.names = FALSE)
if (identical(run_kind, "pilot")) {
  pilot_passed <- nrow(draws) == 5L && ncol(draws) == length(mle) &&
    max(abs(initial - mle)) == 0 && all(is.finite(draws))
  pilot <- data.frame(
    status = if (pilot_passed) "passed" else "failed",
    workflow_role = "mfclrtmb-mcmc-pilot",
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
    mfclrtmb_fix_ref = fix_ref,
    mfclrtmb_fix_sha = fix_sha,
    mfclrtmb_source_tree = source_tree,
    sparse_nuts_sha = sparse_nuts_sha,
    sparse_nuts_version = sparse_nuts_version,
    stan_estimators_sha = stan_estimators_sha,
    stan_estimators_version = stan_estimators_version,
    approved_gate_job = approved_gate_job,
    geometry = geometry,
    chain_id = chain_id,
    total_chains = total_chains,
    warmup = warmup,
    samples = samples,
    refresh = refresh,
    adapt_delta = adapt_delta,
    max_treedepth = max_treedepth,
    base_seed = base_seed,
    skip_monitor = skip_monitor,
    retained_draws = nrow(draws),
    parameter_count = ncol(draws),
    mle_start_max_abs_difference = max(abs(initial - mle)),
    kflow_job_number = Sys.getenv("KFLOW_JOB_NUMBER", ""),
    kflow_job_id = Sys.getenv("KFLOW_JOB_ID", ""),
    stringsAsFactors = FALSE
  )
  utils::write.csv(pilot, file.path(result_dir, "pilot-result.csv"), row.names = FALSE)
  if (!pilot_passed) stop("The mandatory short v2.6 pilot failed", call. = FALSE)
}
saveRDS(
  list(settings = settings, status = status, parameter_names = indexed_names(parameter_names)),
  file.path(result_dir, "chain-provenance.rds"),
  version = 3L
)
print(status)
