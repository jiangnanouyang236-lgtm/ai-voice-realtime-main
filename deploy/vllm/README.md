# vLLM Model Layer

This folder contains the GPU model layer for the v3 voice stack. Keep it
separate from the business service layer so model restarts and Gateway releases
do not have to happen together.

## Layout

```text
deploy/vllm/docker-compose.models.yml   # preferred multi-model compose
deploy/vllm/.env.models.example         # copy to .env.models
deploy/vllm/stt/                        # ASR image and legacy single-service compose
deploy/vllm/llm/                        # main LLM legacy single-service compose
deploy/vllm/llm-router/                 # Router LLM legacy single-service compose
deploy/vllm/llm-mini/                   # optional mini LLM legacy single-service compose
deploy/vllm/tts/                        # TTS legacy single-service compose
```

The checked-in `.env*.example` files are templates only. Real `.env*` files are
ignored by Git.

## Network

The model layer and business layer share one external Docker network:

```bash
docker network create wzk-ai-net
```

Both `deploy/vllm/docker-compose.models.yml` and root `docker-compose.v3.yml`
use `AI_VOICE_DOCKER_NETWORK=wzk-ai-net` by default.

## Start Model Layer

```bash
cp deploy/vllm/.env.models.example deploy/vllm/.env.models
# Edit deploy/vllm/.env.models with model paths, GPU ids, ports, and API keys.

make compose-network
make models-config
make models-up
make models-logs
```

Optional mini LLM profile:

```bash
docker compose --env-file deploy/vllm/.env.models \
  -f deploy/vllm/docker-compose.models.yml \
  --profile mini up -d --build
```

## Business Layer Endpoints

When both layers share the same Docker network, root `deploy/env.v3.compose.example`
can point business services at model containers:

```bash
QWEN_ASR_BASE_URL=http://vllm-stt:8000/v1
LLM_BASE_URL=http://vllm-llm:8000/v1
LLM_ROUTER_BASE_URL=http://vllm-router:8000/v1
QWEN3_TTS_CUSTOM_VOICE_WS_URL=ws://vllm-tts:8000/v1/audio/speech/stream
```

If the model layer runs on another host, keep using host/IP endpoints instead.

## Stop

```bash
make models-down
```
