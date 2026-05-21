#!/usr/bin/env bash
# scripts/render_plantuml.sh
#
# Render every PlantUML source under docs/architecture/ to both PNG and SVG
# using the official `plantuml/plantuml` Docker image. Used by the
# Build_Pipeline (Requirements R10.3, R10.4, R10.5, R10.6).
#
# Usage:
#   bash scripts/render_plantuml.sh
#   PLANTUML_IMAGE=plantuml/plantuml:1.2024.7 bash scripts/render_plantuml.sh
#
# Behavior:
#   - Renders every *.puml file under docs/architecture/ to PNG and SVG,
#     writing outputs alongside each source file (same basename, .png/.svg).
#   - Enforces a 120-second timeout per render via GNU `timeout`.
#   - Reports a per-diagram PASS/FAIL line plus a per-diagram ERROR line on
#     timeout, syntax error, or any non-zero exit from PlantUML.
#   - Fails non-zero if any required diagram from R10.2 is missing:
#       component.puml, deployment.puml, sequence.puml
#   - Does not keep partial artifacts: any .png/.svg produced this run for
#     a diagram that subsequently failed is removed before the script exits.
#   - Idempotent: re-running overwrites any prior artifacts for the diagrams
#     that succeed.
#
# Environment:
#   PLANTUML_IMAGE   Docker image reference. Default: plantuml/plantuml:latest
#
# Exit codes:
#   0   All diagrams rendered successfully (PNG and SVG for every *.puml).
#   1   Generic / unexpected failure.
#   2   Required dependency missing (docker, timeout) or Docker daemon
#       unreachable.
#   3   Required diagram missing from docs/architecture/, or no .puml files
#       found.
#   4   One or more diagrams failed to render (timeout, syntax error, or
#       non-zero exit from PlantUML).

set -euo pipefail

# --- Locate repo root ---------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ARCH_DIR="$REPO_ROOT/docs/architecture"

# --- Configuration ------------------------------------------------------------
PLANTUML_IMAGE="${PLANTUML_IMAGE:-plantuml/plantuml:latest}"
PER_FILE_TIMEOUT="120s"
REQUIRED_DIAGRAMS=("component.puml" "deployment.puml" "sequence.puml")

# --- Bookkeeping --------------------------------------------------------------
# FAILED_DIAGRAMS[diagram_name]=1 when that diagram failed to render this run.
# CREATED_ARTIFACTS holds "diagram::abs_path" tuples for files we produced.
declare -A FAILED_DIAGRAMS=()
declare -a CREATED_ARTIFACTS=()

log() { printf '[render_plantuml] %s\n' "$*"; }
err() { printf '[render_plantuml] ERROR: %s\n' "$*" >&2; }

cleanup() {
  local rc=$?
  # Roll back artifacts for any diagram that failed during this run, so we
  # never publish or keep partial outputs (R10.6).
  if (( ${#FAILED_DIAGRAMS[@]} > 0 )) && (( ${#CREATED_ARTIFACTS[@]} > 0 )); then
    local entry diagram path
    for entry in "${CREATED_ARTIFACTS[@]}"; do
      diagram="${entry%%::*}"
      path="${entry#*::}"
      if [[ -n "${FAILED_DIAGRAMS[$diagram]:-}" ]] && [[ -e "$path" ]]; then
        rm -f -- "$path" 2>/dev/null || true
      fi
    done
  fi
  exit "$rc"
}
trap cleanup EXIT

# --- Dependency checks --------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  err "'docker' is not on PATH. Install Docker (or Docker Desktop) so the official plantuml/plantuml image can be used."
  exit 2
fi

if ! command -v timeout >/dev/null 2>&1; then
  err "'timeout' (GNU coreutils) is not on PATH. Install coreutils, or run from a Linux/WSL/Git-Bash environment that provides it."
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  err "Docker daemon is not reachable. Start Docker (e.g. Docker Desktop) and retry."
  exit 2
fi

# --- Validate architecture dir & required diagrams ---------------------------
if [[ ! -d "$ARCH_DIR" ]]; then
  err "architecture directory not found: $ARCH_DIR"
  exit 3
fi

missing_required=()
for d in "${REQUIRED_DIAGRAMS[@]}"; do
  if [[ ! -f "$ARCH_DIR/$d" ]]; then
    missing_required+=("$d")
  fi
done
if (( ${#missing_required[@]} > 0 )); then
  for d in "${missing_required[@]}"; do
    err "required diagram missing: docs/architecture/$d"
  done
  exit 3
fi

# --- Discover all .puml files -------------------------------------------------
shopt -s nullglob
PUML_FILES=("$ARCH_DIR"/*.puml)
shopt -u nullglob
if (( ${#PUML_FILES[@]} == 0 )); then
  err "no .puml files found under docs/architecture/"
  exit 3
fi

# --- Render -------------------------------------------------------------------
log "image:    $PLANTUML_IMAGE"
log "source:   $ARCH_DIR"
log "diagrams: ${#PUML_FILES[@]} (timeout ${PER_FILE_TIMEOUT} per format)"

# Git Bash / MSYS on Windows rewrites POSIX-looking paths and arguments
# containing ':' before invoking native Windows binaries, which mangles
# `-v /host/path:/work`. Disable the rewrite for our docker invocations.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

# `-failfast2` makes PlantUML exit non-zero on the first syntax error so the
# `timeout` exit code reflects the real outcome.
DOCKER_RUN_BASE=(docker run --rm -v "$ARCH_DIR:/work" -w /work "$PLANTUML_IMAGE" -failfast2)

run_render() {
  # $1: format token for PlantUML (png|svg)
  # $2: source file name relative to /work (e.g. component.puml)
  local fmt="$1" file="$2"
  timeout "$PER_FILE_TIMEOUT" "${DOCKER_RUN_BASE[@]}" "-t${fmt}" "$file"
}

overall_failed=0

for puml_path in "${PUML_FILES[@]}"; do
  diagram="$(basename -- "$puml_path")"
  base="${diagram%.puml}"
  log "--- $diagram ---"

  diagram_failed=0

  for fmt in png svg; do
    out_path="$ARCH_DIR/${base}.${fmt}"
    # Remove any prior artifact so a render failure cannot leave a stale
    # file masquerading as the new output (idempotency + atomicity).
    rm -f -- "$out_path"

    set +e
    render_output="$(run_render "$fmt" "$diagram" 2>&1)"
    rc=$?
    set -e

    # Track produced artifact (if any) so cleanup can remove it on failure.
    if [[ -f "$out_path" ]]; then
      CREATED_ARTIFACTS+=("${diagram}::${out_path}")
    fi

    case "$rc" in
      0)
        if [[ ! -f "$out_path" ]]; then
          err "$diagram: $fmt render reported success but produced no output file"
          if [[ -n "$render_output" ]]; then
            printf '%s\n' "$render_output" | sed 's/^/    | /' >&2
          fi
          FAILED_DIAGRAMS["$diagram"]=1
          diagram_failed=1
          break
        fi
        log "  PASS  ${base}.${fmt}"
        ;;
      124)
        err "$diagram: $fmt render exceeded ${PER_FILE_TIMEOUT} timeout"
        FAILED_DIAGRAMS["$diagram"]=1
        diagram_failed=1
        break
        ;;
      *)
        err "$diagram: $fmt render failed (exit $rc, likely PlantUML syntax error or runtime error)"
        if [[ -n "$render_output" ]]; then
          printf '%s\n' "$render_output" | sed 's/^/    | /' >&2
        fi
        FAILED_DIAGRAMS["$diagram"]=1
        diagram_failed=1
        break
        ;;
    esac
  done

  if (( diagram_failed != 0 )); then
    log "  FAIL  $diagram"
    overall_failed=1
  fi
done

if (( overall_failed != 0 )); then
  err "${#FAILED_DIAGRAMS[@]} diagram(s) failed to render: ${!FAILED_DIAGRAMS[*]}"
  exit 4
fi

log "all ${#PUML_FILES[@]} diagram(s) rendered successfully (PNG + SVG)"
exit 0
