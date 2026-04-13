#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/home/liumq/manifold-cfd-opt"
RUN_DIR="/home/liumq/opt_runs/opt2000"

cd "$REPO_DIR"

git stash push -u -m "auto-stash before opt2000 $(date +%F_%T)" >/dev/null 2>&1 || true
git pull --ff-only origin main

mkdir -p "$RUN_DIR"

cat > "$RUN_DIR/config_opt_remote_2000.yaml" <<EOF
evaluator: remote_openfoam

# 2000 designs: 200 + 450*4 = 2000
n_initial: 200
n_iterations: 450
batch_size: 4

template_dir: ${REPO_DIR}/templates/manifold_2d
cases_base: of_cases

ssh_host: 127.0.0.1
ssh_user: liumq
ssh_port: 22
remote_base: /home/liumq/manifold_cases
foam_source: /opt/openfoam13/etc/bashrc

n_cores: 16
max_parallel_cases: 8
timeout_s: 1800

db_path: ${RUN_DIR}/results_opt_2000.sqlite
csv_output: ${RUN_DIR}/results_opt_2000.csv
report_path: ${RUN_DIR}/optimization_report_opt_2000.md
EOF

cd "$RUN_DIR"

if [[ -f opt2000.pid ]]; then
  PID="$(cat opt2000.pid || true)"
  if [[ -n "${PID}" ]] && kill -0 "${PID}" >/dev/null 2>&1; then
    echo "ALREADY_RUNNING pid=${PID}"
    echo "run_dir=${RUN_DIR}"
    echo "log=${RUN_DIR}/opt2000.log"
    exit 0
  fi
fi

nohup python3 -u "${REPO_DIR}/scripts/run_openfoam_opt.py" config_opt_remote_2000.yaml > opt2000.log 2>&1 < /dev/null &
echo $! > opt2000.pid

echo "STARTED pid=$(cat opt2000.pid)"
echo "run_dir=${RUN_DIR}"
echo "log=${RUN_DIR}/opt2000.log"
tail -n 20 opt2000.log || true

