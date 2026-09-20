# Unified Logging

This repo uses one logging convention across the Python services, Go Voice Gateway, and Rust client.

## Common Environment Variables

| Variable | Default | Applies to | Notes |
| --- | --- | --- | --- |
| `VOICE_LOG_LEVEL` | `info` | Python, Go, Rust | `debug`, `info`, `warn`/`warning`, `error`. Rust also accepts `RUST_LOG`. |
| `VOICE_LOG_FORMAT` | `text` / Rust `compact` | Python, Go, Rust | Python/Go support `text` and `json`. Rust supports `compact`, `pretty`, and `full`. |
| `VOICE_LOG_COLOR` | `auto` | Python, Rust | `auto`, `always`, `never`. Keep this off for JSON logs. |
| `VOICE_LOG_STREAM` | `stdout` | Python | `stdout` or `stderr`. |
| `VOICE_LOG_DIR` | empty | Python, Go | Writes `<service>.log` in this directory with rotation. |
| `VOICE_LOG_FILE` | empty | Python, Go | Exact log file path. Prefer `VOICE_LOG_DIR` when running multiple services. |
| `VOICE_LOG_MAX_BYTES` | `10485760` | Python, Go | Per-file rotation size. |
| `VOICE_LOG_BACKUP_COUNT` | `5` | Python, Go | Number of rotated files to keep. |

Go Voice Gateway also keeps service-specific aliases such as `GO_VOICE_GATEWAY_LOG_FORMAT`, `GO_VOICE_GATEWAY_LOG_LEVEL`, `GO_VOICE_GATEWAY_LOG_DIR`, and `GO_VOICE_GATEWAY_LOG_FILE`. Those aliases take priority over the shared variables.

## Recommended Profiles

Local terminal debugging:

```bash
export VOICE_LOG_LEVEL=info
export VOICE_LOG_FORMAT=text
export VOICE_LOG_COLOR=always
```

Docker Compose or Kubernetes stdout collection:

```bash
export VOICE_LOG_LEVEL=info
export VOICE_LOG_FORMAT=json
export VOICE_LOG_COLOR=never
```

Bare-metal deployment with local rotated files:

```bash
export VOICE_LOG_LEVEL=info
export VOICE_LOG_FORMAT=json
export VOICE_LOG_DIR=/var/log/wzk-ai-voice
export VOICE_LOG_MAX_BYTES=10485760
export VOICE_LOG_BACKUP_COUNT=5
```

## Python Services

Python entrypoints now share `voice_logging.configure_logging()`:

- `gateway` for `gateway/gateway_server.py`
- `stt` for `stt/stt_grpc_server.py`
- `llm` for `llm/llm_grpc_server.py`
- `tts` for `tts/tts_grpc_server.py`
- `mcp-utils`, `mcp-robot`, and `mcp-singing` for the SSE MCP servers

Text logs include timestamp, level, service, logger, and message. JSON logs include stable fields such as `ts`, `level`, `service`, `logger`, `message`, `module`, `line`, `process`, and `thread`.

The Python formatter redacts common sensitive keys and inline key-value patterns such as `robot_secret=...`, `api_key=...`, `password=...`, and `token=...`.

## Go Voice Gateway

Go Voice Gateway uses the standard library `slog` logger. It supports text and JSON output, source IP fields, and optional local file rotation.

When WebRTC ICE connects, Go Voice Gateway logs `ice_selected_candidate_pair` once per connected/completed transition. The important fields are:

- `ice_route=turn` when the selected local or remote candidate is `relay`, meaning media/data is going through TURN relay.
- `ice_route=stun_or_direct` when the selected pair is not relay. Check `local_candidate_type` and `remote_candidate_type`; `srflx`/`prflx` means STUN-derived NAT traversal, while `host` usually means direct LAN/local candidate.
- `local_candidate_protocol`, `local_candidate_relay_protocol`, and `local_candidate_url` help distinguish TURN UDP/TCP/TLS when relay is used.

Useful examples:

```bash
GO_VOICE_GATEWAY_LOG_LEVEL=debug GO_VOICE_GATEWAY_LOG_FORMAT=text ./go_voice_gateway
GO_VOICE_GATEWAY_LOG_FORMAT=json GO_VOICE_GATEWAY_LOG_DIR=/var/log/wzk-ai-voice ./go_voice_gateway
```

For production containers, prefer stdout JSON and let Docker/Kubernetes handle retention. Use local rotation mainly for direct binary deployment or short diagnostic runs.

## Rust Client

The Rust binaries share `rust_client/src/logging.rs` and use `tracing_subscriber`.

When native WebRTC selects an ICE candidate pair, the Rust client logs `[RTC] native selected ICE candidate pair`. The important fields match Go Voice Gateway:

- `ice_route=turn` means the selected local or remote candidate is `relay`, so the connection is going through TURN relay.
- `ice_route=stun_or_direct` means no relay candidate was selected. Check `local_candidate_type` and `remote_candidate_type`: `srflx`/`prflx` means STUN-derived NAT traversal; `host` usually means LAN/direct.
- `ice_route=unknown` is only for an unspecified selected pair and should be treated as a diagnostic anomaly.

Useful examples:

```bash
VOICE_LOG_LEVEL=debug VOICE_LOG_FORMAT=pretty cargo run --bin rust_client --features native-webrtc
RUST_LOG=rust_client=debug,webrtc=info cargo run --bin live_speech_smoke --features native-webrtc
```

The Rust client intentionally logs to stderr/stdout instead of owning file rotation. On robots or servers, use systemd journald, Docker logging, or logrotate around the process.

## Notes

- Do not log raw robot secrets, API keys, access tokens, or database URLs.
- Keep high-volume packet/audio logs behind `debug`.
- Prefer structured fields for identifiers: `session`, `round_id`, `utterance_id`, `robot_id`, and `source_ip`.
- In WebRTC mode, Rust `playback_complete` / `playback_interrupted` reports are
  forwarded by Go Gateway back to Python Gateway so
  `/internal/runtime/traces` includes client playback completion, underruns,
  and first-audio-to-playback timing for the same `round_id`.
