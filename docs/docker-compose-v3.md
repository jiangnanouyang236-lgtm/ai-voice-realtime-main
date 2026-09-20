# Go + Python 兼容 Compose Deployment

> 文件名和 Makefile 目标保留 `v3` 以兼容现有部署脚本。当前 `main` 已承载 V4 测试候选；
> 新的拆分 Python 服务优先参考 [`docker-compose-v4.md`](./docker-compose-v4.md)。

The v3 deployment is split into small Docker Compose layers:

- Model layer: `deploy/vllm/docker-compose.models.yml` starts vLLM ASR, LLM,
  Router, and TTS services on GPU.
- Business layer: `deploy/compose/business.yml` starts STT/LLM/TTS gRPC
  adapters, Python Gateway, and Admin on `wzk-ai-net`.
- WebRTC entry layer: `deploy/compose/go-gateway.host.yml` starts Go Gateway
  with host networking.
- Optional MCP layer: `deploy/compose/mcp.yml` starts local MCP helper
  services behind the `mcp` profile.

The root `docker-compose.v3.yml` is intentionally thin. It includes the split
files so `docker compose -f docker-compose.v3.yml ...` still works, while
Makefile targets use explicit `-f` combinations for older operational habits
and clearer per-layer starts.

The business-layer path is:

```text
Rust client
  -> Go Voice Gateway :8282 on host network
  -> Python Gateway :7860 on host loopback
  -> STT :50054, LLM :50053, TTS :50052
```

The Rust client is not included in compose. It normally runs on the robot,
board, or test machine.

The model layer and Python/business services join the same external Docker
network `AI_VOICE_DOCKER_NETWORK=wzk-ai-net`. The Go Gateway intentionally uses
host network so WebRTC UDP candidates bind directly on the host instead of
going through Docker port NAT.

## Why Compose Before Full Go Orchestration

The current Go Gateway already reuses one Python Gateway WebSocket per Rust
session. Removing that local bridge is unlikely to fix user-facing latency by
itself: current slow paths usually sit in LLM first token, tool/MCP routing, or
TTS provider output. A full Go orchestration rewrite would also need to preserve
Python Gateway behavior for round state, interrupt/cancel semantics, playback
ids, trace events, Bot runtime config, Router/MCP/Agent handling, and the
incremental char-by-char TTS stream.

Compose is therefore the lower-risk next step: it makes the current validated
pipeline easier to deploy, observe, restart, and later split.

## Start

Create the shared Docker network:

```bash
make compose-network
```

Start the model layer first when vLLM runs on the same host:

```bash
cp deploy/vllm/.env.models.example deploy/vllm/.env.models
# Edit deploy/vllm/.env.models with model paths, GPU ids, ports, and API keys.
make models-config
make models-up
make models-logs
```

Prepare environment values:

```bash
cp deploy/env.v3.compose.example .env
# Edit .env with the real test or production values.
```

Build the Go Gateway runtime binary and package it into a runtime image before
starting the compose stack. The runtime Dockerfile only copies the packaged
binary and does not run `go mod download` or compile Go source:

```bash
cd go_voice_gateway
make package-ubuntu22-amd64
cd dist/go_voice_gateway-ubuntu22-amd64
docker build -f Dockerfile.ubuntu22 -t ai-voice-go-gateway:ubuntu22-amd64 .
cd ../../..
```

`deploy/compose/go-gateway.host.yml` references that image through
`GO_VOICE_GATEWAY_IMAGE` and intentionally has no `build:` block. This keeps
production `docker compose up -d` away from Compose's buildx path.

Render the final compose config:

```bash
make compose-v3-validate
make compose-v3-config
# or:
docker compose -f docker-compose.v3.yml config
```

`make compose-v3-validate` only renders compose with
`deploy/env.v3.compose.example` and checks the split stack shape. It does not
start containers or read production secrets.

`docker compose config` expands environment variables from the shell or root
`.env`. Treat its output as sensitive when real keys are present.

If vLLM runs in `deploy/vllm/docker-compose.models.yml`, the default business
endpoints can use container DNS:

```bash
QWEN_ASR_BASE_URL=http://vllm-stt:8000/v1
LLM_BASE_URL=http://vllm-llm:8000/v1
LLM_ROUTER_BASE_URL=http://vllm-router:8000/v1
QWEN3_TTS_CUSTOM_VOICE_WS_URL=ws://vllm-tts:8000/v1/audio/speech/stream
```

If vLLM runs on another host, keep these as host/IP endpoints instead.

By default, non-WebRTC business service ports are published on host loopback
only through `AI_VOICE_HOST_BIND_IP=127.0.0.1`. This lets the host-network Go
Gateway call Python Gateway at `ws://127.0.0.1:7860/ws` while keeping STT,
LLM, TTS, Admin, and optional MCP ports off the LAN. Set
`AI_VOICE_HOST_BIND_IP=0.0.0.0` only when you intentionally want those ports
reachable from outside the host.

Start the core stack:

```bash
make compose-v3-up
# or:
docker compose -f docker-compose.v3.yml up -d
```

Follow logs:

```bash
make compose-v3-logs
```

Smoke-check the published host ports:

```bash
make compose-v3-smoke
```

Check the Go Gateway process and active WebRTC sessions:

```bash
curl -s http://127.0.0.1:8282/healthz
curl -s http://127.0.0.1:8282/internal/status
```

`/healthz` is intentionally compact for container health checks. `/internal/status`
adds uptime, configured WebRTC/ICE/audio/ASR bridge settings, active session
counts, recent session source IPs, peer/ICE states, and RTP packet counters.
Secrets are not emitted. Keep `/internal/status` reachable only from localhost
or an internal operations network; do not expose it through public NGINX/FRP
routes.

Stop:

```bash
make compose-v3-down
```

## Split Files

Use the Makefile targets for normal operations:

```bash
# Core stack: business services plus Go Gateway
make compose-v3-validate
make compose-v3-config
make compose-v3-up
make compose-v3-logs
make compose-v3-down

# Business services only: STT, LLM, TTS, Python Gateway, Admin
make compose-business-up
make compose-business-logs
make compose-business-down

# Go Gateway only, useful when Python Gateway already runs on the host
cd go_voice_gateway
make package-ubuntu22-amd64
cd dist/go_voice_gateway-ubuntu22-amd64
docker build -f Dockerfile.ubuntu22 -t ai-voice-go-gateway:ubuntu22-amd64 .
cd ../../..
make compose-go-up
make compose-go-logs
make compose-go-down

# Optional local MCP helpers
make compose-mcp-up
make compose-mcp-logs
make compose-mcp-down
```

The equivalent raw Docker Compose commands are:

```bash
docker compose -f deploy/compose/business.yml -f deploy/compose/go-gateway.host.yml up -d
docker compose -f deploy/compose/business.yml up -d --build
docker compose -f deploy/compose/go-gateway.host.yml up -d
docker compose -f deploy/compose/mcp.yml --profile mcp up -d --build
```

## Ports

| Service | Container | Host default | Notes |
| --- | ---: | ---: | --- |
| Go Voice Gateway | host network | 8282 | Rust client WebRTC signaling entrypoint; controlled by `GO_VOICE_GATEWAY_ADDR`. |
| Go Voice Gateway WebRTC UDP | host network | 35500-35600/udp | Pion ICE/media candidate port range bound directly on the host. |
| Python Gateway | 7860 | 127.0.0.1:7860 | Current orchestration bridge for Go Gateway. |
| STT gRPC | 50054 | 127.0.0.1:50054 | Internal STT service. |
| LLM gRPC | 50053 | 127.0.0.1:50053 | Internal LLM service. |
| LLM admin | 18053 | 127.0.0.1:18053 | Admin runtime status/reload. |
| TTS gRPC | 50052 | 127.0.0.1:50052 | Internal TTS service. |
| TTS admin | 18052 | 127.0.0.1:18052 | Admin runtime status/reload. |
| Admin API | 18100 | 127.0.0.1:18100 | Admin backend and optional SPA host. |

The Go Gateway WebRTC UDP range defaults to
`GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT=35500` and
`GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT=35600`. Because the container uses host
network, Docker does not publish this range; open the same UDP range on the
host firewall and any upstream NAT if you expect STUN/direct connectivity.

To see which environment variable controls each Go Gateway config field:

```bash
cd go_voice_gateway
go run . --print-env-help
```

## Local MCP Profile

The local MCP helpers are optional because most deployments should use the
database-backed MCP runtime config. To start local MCP helpers too:

```bash
make compose-mcp-up
```

Then point Bot MCP server config at:

- `http://mcp-utils:5004/mcp`
- `http://mcp-robot:5003/mcp`
- `http://mcp-singing:5005/mcp`

专用天气 MCP 已移除；天气查询继续通过数据库配置的 Websearch MCP。

## Rust Client

When Go Gateway uses the default host-network listen address:

```bash
cd rust_client
GATEWAY_URL=ws://<server-ip>:8282/ws \
ROBOT_ID=test_01 \
ROBOT_SECRET="$ROBOT_SECRET" \
TRANSPORT_POLICY=webrtc_only \
WEBRTC_ENABLED=true \
WEBRTC_OFFER_FACTORY=native \
RTC_AUDIO_UPLINK=rtp_only \
cargo run --bin rust_client --features native-webrtc --release
```

If Go Gateway listens on a different port, set `GO_VOICE_GATEWAY_ADDR` for
compose and the matching Rust `GATEWAY_URL`.

To see which environment variable controls each Rust client config field:

```bash
cd rust_client
cargo run --bin rust_client --features native-webrtc -- --print-env-help
```

## Notes

- `LLM_GRPC_BIND_HOST`, `TTS_GRPC_BIND_HOST`, and `ADMIN_API_HOST` default to
  `127.0.0.1` for nohup/bare-metal runs, but compose sets them to `0.0.0.0`.
- The Go Gateway service uses `network_mode: host`; do not add a `ports:`
  block to that service. Other business services stay on `wzk-ai-net` and are
  published to host loopback for same-host access.
- The Go Gateway container uses its own binary for health checks:
  `/go_voice_gateway --healthcheck`.
- Go Gateway still bridges to Python Gateway. This keeps the current
  `ASR -> LLM -> TTS` orchestration behavior intact.
- Full Go direct gRPC orchestration is intentionally deferred. It is likely a
  larger behavior migration than a transport optimization because it must
  preserve Bot runtime config, Router/MCP/Agent behavior, round cancellation,
  playback ids, trace events, and the existing incremental TTS contract.
- TTS text submission must remain incremental; do not batch or sentence-split
  text before sending it to the Local Qwen3 TTS WebSocket.
