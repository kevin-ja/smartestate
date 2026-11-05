#!/usr/bin/env bash
# Baja lo de una corrida de T1 a logs/t1_run_<run_id>/ e imprime dónde se fue el tiempo. Solo lee.
# Hay que correrlo antes de shut_down.sh: la corrida muere con el workspace.
#
# Uso:  bash scripts/t1_logs.sh <run_id>
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
run_id="${1:?Uso: bash scripts/t1_logs.sh <run_id>}"

set -a; . "$repo/.env"; set +a
export DATABRICKS_CONFIG_PROFILE=smartestate-dev
export DATABRICKS_HOST="$SMARTESTATE_WORKSPACE_HOST"

out="$repo/logs/t1_run_$run_id"
mkdir -p "$out"

databricks jobs get-run "$run_id" -o json > "$out/run.json"
cluster_id="$(jq -r '[.tasks[].cluster_instance.cluster_id // empty][0] // empty' "$out/run.json")"

# Salida de cada task. Una task sin arrancar todavía no tiene salida: no es un error.
for task in $(jq -r '.tasks[] | "\(.task_key):\(.run_id)"' "$out/run.json"); do
  key="${task%%:*}"
  databricks jobs get-run-output "${task##*:}" -o json > "$out/$key.output.json" 2>/dev/null || continue
  jq -r '(.logs // ""), (.error // ""), (.error_trace // "")' "$out/$key.output.json" > "$out/$key.log"
done

if [[ -n "$cluster_id" ]]; then
  databricks clusters events "$cluster_id" -o json > "$out/cluster_events.json" || true
fi

echo "== Corrida: $(jq -r '.state.life_cycle_state + " " + (.state.result_state // "")' "$out/run.json")"
echo "== Tasks"
jq -r '.tasks[] | "\(.task_key)\tsetup=\((.setup_duration // 0) / 1000 | floor)s\texec=\((.execution_duration // 0) / 1000 | floor)s\t\(.state.result_state // .state.life_cycle_state)"' "$out/run.json"
if [[ -f "$out/cluster_events.json" ]]; then
  echo "== Eventos del cluster"
  jq -r '(if type == "array" then . else .events end)[] | "\(.timestamp / 1000 | floor | todate)\t\(.type)"' "$out/cluster_events.json" | sort
fi

source_log="$out/data_pipeline.log"
if [[ -f "$source_log" ]] && grep -q 'stage start' "$source_log"; then
  echo "== Etapas ($source_log)"
  grep -E 'stage (start|end|failed)' "$source_log" | cut -d' ' -f2- || true
  echo "== Último avance de SBERT y recursos"
  grep 'sbert: ' "$source_log" | tail -n 1 || true
  grep 'resources stage=' "$source_log" | tail -n 1 || true
  echo "== Resumen"
  grep -A 30 'stage summary' "$source_log" || echo "Aún sin resumen: la corrida no ha terminado."
fi
echo "Guardado en $out"
