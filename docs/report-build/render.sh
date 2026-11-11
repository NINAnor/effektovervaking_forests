#!/usr/bin/env bash
set -euo pipefail

docs_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if ! command -v quarto >/dev/null 2>&1; then
    printf '%s\n' 'Quarto is not installed or not on PATH. Install Quarto >= 1.4.' >&2
    exit 1
fi

input=.
if [[ ${1:-} == *.qmd ]]; then
    case "$1" in
        forest_monitoring_report.qmd|forest_monitoring_methods.qmd) input=$1 ;;
        *) printf 'Unknown report source: %s\n' "$1" >&2; exit 1 ;;
    esac
    shift
fi

build_dir=$(mktemp -d "${TMPDIR:-/tmp}/forest-monitoring-quarto.XXXXXX")
trap 'rm -rf -- "$build_dir"' EXIT

for resource in _quarto.yml forest_monitoring_report.qmd forest_monitoring_methods.qmd \
    references.bib ambio.csl reference_a4.docx typst-template.typ typst_wrap_code.lua \
    figures map media; do
    if [[ -e "$docs_dir/$resource" ]]; then
        cp -R -- "$docs_dir/$resource" "$build_dir/$resource"
    fi
done

quarto render "$build_dir/$input" --to all "$@"

for output in index.html forest_monitoring_report.pdf forest_monitoring_report.docx \
    forest_monitoring_methods.html forest_monitoring_methods.pdf forest_monitoring_methods.docx; do
    if [[ -f "$build_dir/$output" ]]; then
        cp -- "$build_dir/$output" "$docs_dir/$output"
        printf 'Published %s\n' "$docs_dir/$output"
    fi
done
