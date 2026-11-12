#!/usr/bin/env bash
# 手動觸發 GitHub Actions 工作流程並等它跑完（GitHub 排程常延遲數小時，排程任務改用這支主動觸發）。
# 用法：bash scripts/run_workflow.sh <workflow 檔名> [key=value ...]
# 例：bash scripts/run_workflow.sh update.yml
#     bash scripts/run_workflow.sh night.yml mode=update
set -u
WF=$1; shift
R=$(git -C "$(dirname "$0")/.." remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')
T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ARGS=(-f ref=main)
for kv in "$@"; do ARGS+=(-f "inputs[${kv%%=*}]=${kv#*=}"); done
gh api -X POST "repos/$R/actions/workflows/$WF/dispatches" "${ARGS[@]}" || { echo "觸發失敗"; exit 2; }
echo "已觸發 $R $WF（$T0）"
S=""
for i in $(seq 1 60); do
  sleep 10
  S=$(gh api "repos/$R/actions/workflows/$WF/runs?event=workflow_dispatch&per_page=5" \
      --jq "[.workflow_runs[] | select(.created_at >= \"$T0\")][-1] | \"\(.status) \(.conclusion) \(.html_url)\"" 2>/dev/null)
  case "$S" in completed*) break ;; esac
done
echo "結果：${S:-找不到執行紀錄}"
case "$S" in "completed success"*) exit 0 ;; *) exit 1 ;; esac
