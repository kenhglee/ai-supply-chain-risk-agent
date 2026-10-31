cd "~/projects/supplychain-ai-agent"
export AWS_PROFILE=ken-ai-dev AWS_DEFAULT_REGION=us-west-2
set -a
source docker/worker.env
set +a
export AUTHORITY_TABLE=risk_agent_authority
export ACTIONS_TABLE=risk_agent_actions
export CODE_REVISION="e43c54c30afb2fcfb057668a48f98e070cde3792"
PY="~/projects/supplychain-ai-agent/venv/bin/python"
C="scripts/c2b_analysis_kill.py"
D="~/ownership-runs/OWN-contention-20261031T225042Z"
E="~/ownership-runs/OWN-contention-20261031T225042Z/events.jsonl"
export T="thread-69cc83390c314386a9e6a058c77109fe"
