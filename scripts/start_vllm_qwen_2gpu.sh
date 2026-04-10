set -euo pipefail
export CUDA_VISIBLE_DEVICES=0,1,2,3
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export NCCL_CUMEM_ENABLE=0
export VLLM_PORT=${VLLM_PORT:-8001}
export VLLM_HOST=${VLLM_HOST:-0.0.0.0}
export VLLM_MODEL=${VLLM_MODEL:-/home/liumq/models/Qwen/Qwen2___5-72B-Instruct-AWQ}
export VLLM_SERVED_MODEL_NAME=${VLLM_SERVED_MODEL_NAME:-qwen2.5-72b}
export VLLM_TP=${VLLM_TP:-4}
export VLLM_MAX_LEN=${VLLM_MAX_LEN:-8192}

python3 -m vllm.entrypoints.openai.api_server \
  --host "${VLLM_HOST}" \
  --port "${VLLM_PORT}" \
  --model "${VLLM_MODEL}" \
  --served-model-name "${VLLM_SERVED_MODEL_NAME}" \
  --tensor-parallel-size "${VLLM_TP}" \
  --max-model-len "${VLLM_MAX_LEN}" \
  --quantization awq_marlin \
  --disable-custom-all-reduce \
  --gpu-memory-utilization 0.88
