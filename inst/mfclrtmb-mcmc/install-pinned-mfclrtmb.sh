#!/usr/bin/env bash
set -euo pipefail

work_root="${KFLOW_WORK_ROOT:-${PWD}/work}"
library_root="${R_LIBS_USER:-${work_root}/R-library}"
source_root="${work_root}/mfclrtmb-fixed-source"
repo="${MFCLRTMB_FIX_REPO:-PacificCommunity/ofp-sam-mfclrtmb}"
ref="${MFCLRTMB_FIX_REF:-}"
expected_sha="${MFCLRTMB_FIX_SHA:-}"
workflow_sha="${WORKFLOW_SHA:-}"
model_source_sha="${SINGLE_AREA_MODEL_SOURCE_SHA:-}"
runtime_image="${KFLOW_RUNTIME_IMAGE:-}"
actual_runtime_image="${KFLOW_DOCKER_IMAGE:-}"

if [[ ! "$workflow_sha" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "[mfclrtmb-source] WORKFLOW_SHA must be a full 40-character commit SHA." >&2
  exit 2
fi
if [[ ! "$model_source_sha" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "[mfclrtmb-source] SINGLE_AREA_MODEL_SOURCE_SHA must be a full commit SHA." >&2
  exit 2
fi
checkout_sha="$(git rev-parse 'HEAD^{commit}')"
if [[ "${checkout_sha,,}" != "${workflow_sha,,}" ]]; then
  echo "[mfclrtmb-source] Kflow checkout does not match WORKFLOW_SHA." >&2
  echo "expected=${workflow_sha,,} checkout=${checkout_sha,,}" >&2
  exit 2
fi
if [[ -z "$runtime_image" ]]; then
  echo "[mfclrtmb-source] KFLOW_RUNTIME_IMAGE is required." >&2
  exit 2
fi
if [[ -z "$actual_runtime_image" || "$actual_runtime_image" != "$runtime_image" ]]; then
  echo "[mfclrtmb-source] Actual Kflow Docker image does not match KFLOW_RUNTIME_IMAGE." >&2
  echo "expected=$runtime_image actual=${actual_runtime_image:-missing}" >&2
  exit 2
fi

if [[ -z "$ref" ]]; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_REF is required." >&2
  exit 2
fi
if [[ ! "$ref" =~ ^refs/(heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_REF must be a full heads/tags ref." >&2
  exit 2
fi
if [[ "$ref" == *".."* || "$ref" == *"@{"* || "$ref" == *"//"* ||
      "$ref" == */ || "$ref" == *. || "$ref" == *.lock ]]; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_REF is not a safe canonical Git ref." >&2
  exit 2
fi
if ! git check-ref-format "$ref" >/dev/null 2>&1; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_REF fails git check-ref-format." >&2
  exit 2
fi
if [[ ! "$expected_sha" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_SHA must be a full 40-character commit SHA." >&2
  exit 2
fi
if [[ ! "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  echo "[mfclrtmb-source] MFCLRTMB_FIX_REPO must be an owner/repository name." >&2
  exit 2
fi
if [[ -e "$source_root" ]]; then
  echo "[mfclrtmb-source] Refusing to reuse an existing source directory: $source_root" >&2
  exit 2
fi

mkdir -p "$work_root" "$library_root"
if [[ -z "${GITHUB_PAT:-${GIT_PAT:-}}" ]]; then
  echo "[mfclrtmb-source] Kflow did not forward GitHub authentication for the private source repository." >&2
  exit 2
fi
askpass="$(mktemp "${work_root}/.mfclrtmb-git-askpass.XXXXXX")" || {
  echo "[mfclrtmb-source] Could not create the temporary GitHub credential helper." >&2
  exit 2
}
cleanup_askpass() {
  rm -f -- "$askpass"
}
trap cleanup_askpass EXIT
cat > "$askpass" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *Password*) printf '%s\n' "${GITHUB_PAT:-${GIT_PAT:-}}" ;;
  *) printf '\n' ;;
esac
EOF
chmod 700 "$askpass"
export GIT_ASKPASS="$askpass"
export GIT_TERMINAL_PROMPT=0

git init --quiet "$source_root"
git -C "$source_root" remote add origin "https://github.com/${repo}.git"
git -C "$source_root" fetch --quiet --no-tags --depth 1 origin "$ref"
resolved_sha="$(git -C "$source_root" rev-parse 'FETCH_HEAD^{commit}')"
if [[ "${resolved_sha,,}" != "${expected_sha,,}" ]]; then
  echo "[mfclrtmb-source] Immutable source verification failed." >&2
  echo "expected=${expected_sha,,} resolved=${resolved_sha,,} ref=$ref" >&2
  exit 2
fi
git -C "$source_root" checkout --quiet --detach "$resolved_sha"
checked_out_sha="$(git -C "$source_root" rev-parse HEAD)"
if [[ "${checked_out_sha,,}" != "${expected_sha,,}" ]]; then
  echo "[mfclrtmb-source] Detached checkout does not match MFCLRTMB_FIX_SHA." >&2
  exit 2
fi
if [[ -n "$(git -C "$source_root" status --porcelain=v1)" ]]; then
  echo "[mfclrtmb-source] Refusing to install a dirty source checkout." >&2
  exit 2
fi
cleanup_askpass
trap - EXIT
unset GIT_ASKPASS GIT_TERMINAL_PROMPT GITHUB_PAT GIT_PAT

source_tree="$(git -C "$source_root" rev-parse 'HEAD^{tree}')"
printf '%s\n' \
  "workflow_commit=${workflow_sha,,}" \
  "model_source_commit=${model_source_sha,,}" \
  "runtime_image=$runtime_image" \
  "actual_runtime_image=$actual_runtime_image" \
  "repository=$repo" \
  "requested_ref=$ref" \
  "resolved_commit=${resolved_sha,,}" \
  "source_tree=$source_tree" \
  > "${work_root}/mfclrtmb-source-provenance.txt"

echo "[mfclrtmb-source] Installing ${repo}@${resolved_sha,,} from verified source"
R CMD INSTALL \
  --no-multiarch \
  --with-keep.source \
  --library="$library_root" \
  "$source_root"

export MFCLRTMB_RESOLVED_SHA="${resolved_sha,,}"
export MFCLRTMB_SOURCE_TREE="$source_tree"
Rscript --vanilla - <<'RS'
library_root <- Sys.getenv("R_LIBS_USER")
expected <- tolower(Sys.getenv("MFCLRTMB_FIX_SHA"))
resolved <- tolower(Sys.getenv("MFCLRTMB_RESOLVED_SHA"))
stopifnot(nzchar(library_root), identical(expected, resolved))
.libPaths(unique(c(library_root, .libPaths())))
stopifnot(requireNamespace("mfclrtmb", quietly = TRUE))
path <- normalizePath(find.package("mfclrtmb"), mustWork = TRUE)
expected_path <- normalizePath(file.path(library_root, "mfclrtmb"), mustWork = TRUE)
stopifnot(identical(path, expected_path))
cat("[mfclrtmb-source] Installed mfclrtmb ",
    as.character(utils::packageVersion("mfclrtmb")), " at ", path, "\n", sep = "")
RS
