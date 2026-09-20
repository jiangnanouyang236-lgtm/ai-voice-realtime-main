# Go-Python Internal Voice Protocol v1

> Status: frozen production contract
> Frozen: 2026-07-30
> Scope: Go Voice Gateway <-> Python Gateway
> Transport: one WebSocket per realtime client session

This document is the frozen production contract for M1. M1 is now the V3
default; M0 remains an explicit session-level rollback.

## 1. Completion Definition

M1 is complete only when one `/internal/voice/ws` session carries:

- session open, heartbeat, and close;
- completed user audio and its utterance lifecycle;
- committed Turn Gate / natural-barge-in candidate text without a second ASR pass;
- client events such as wake, startup, interrupt wake, and sleep exit;
- interrupt and input-audio cancellation;
- playback reports;
- orchestrator status;
- TTS audio;
- completed, cancelled, and failed response terminals.

The compatible `/ws` bridge remains an explicit rollback path. A production
session must not split active semantics between M0 and M1.

## 2. Current Production State

| Area | V3 behavior | Constraint |
|---|---|---|
| Session | One M1 WebSocket per Rust realtime session | Connection generation changes cannot resume accepted work |
| User audio | Completed Opus batch enters Python ASR through M1 | No automatic replay after acceptance |
| Candidate text | Accepted candidate text enters Python LLM/TTS through the same M1 session | No M0 session fork or second ASR |
| Client event | M1 active and idempotent | Duplicate event ID cannot duplicate TTS |
| Interrupt / playback report | M1 owns active semantics | Must correlate round and playback generation |
| Downlink | M1 status, VAF1 audio and typed terminal | Incremental TTS cannot be sentence-buffered |
| Errors | Structured source error is forwarded once | Do not rewrap as a second bridge error |
| Fallback | `m0` is an explicit session mode | No active per-turn M0/M1 mixing |

## 3. Ownership

Go owns:

- WebRTC, RTP ordering/loss accounting, packet grace windows, and ICE route;
- bounded Opus packet retention until utterance commit;
- the M1 socket, heartbeat, reconnect decision, and message dispatch;
- active downlink generation and stale audio rejection;
- forwarding Rust playback reports.

Python owns:

- device/session registration validation;
- utterance acceptance, ASR, LLM, MCP, memory, and TTS orchestration;
- response round allocation and lifecycle;
- cancellation of ASR/LLM/TTS work;
- structured business and upstream errors;
- final `exit` semantics.

Only Python creates `round_id` and `playback_id`. Go creates `utterance_id`.
Both sides preserve `session_id` and `trace_id`.

## 4. Transport and Framing

### 4.1 Connection

- Endpoint: `/internal/voice/ws`.
- One WebSocket per Go realtime session.
- JSON text frames carry control envelopes.
- VAF1 binary frames carry audio without base64.
- Maximum JSON and binary sizes are negotiated in `session.opened`.
- A reconnect creates a new protocol connection generation. It does not
  silently resume an accepted utterance or response.

### 4.2 JSON Envelope

Every control frame uses:

```json
{
  "version": 1,
  "type": "input_audio.end",
  "session_id": "rtc_...",
  "trace_id": "rtc_...:utt_...",
  "utterance_id": "utt_...",
  "round_id": "rtc_...:4",
  "playback_id": "rtc_...:4:playback",
  "timestamp_ms": 1780000000000,
  "payload": {}
}
```

Rules:

- `version`, `type`, `session_id`, `timestamp_ms`, and `payload` are required.
- Unknown fields are ignored within protocol version 1.
- An unknown `type` returns `protocol.error` with
  `code=UNSUPPORTED_MESSAGE_TYPE`.
- A message with missing required identifiers returns `protocol.error` and has
  no orchestration side effect.
- `trace_id` is stable for one user turn or client-event turn.

### 4.3 VAF1 Audio

The existing VAF1 prefix is retained:

```text
"VAF1" | uint32_be(header_json_bytes) | header_json | payload
```

The JSON header must contain:

- `type: "audio_frame"` for decoder compatibility;
- `event_type: "input_audio.batch"` or `"response.audio"`;
- `version: 1`;
- `direction: "uplink"` or `"downlink"`;
- all applicable protocol identifiers;
- codec, sample rate, channels, sequence, and payload size.

Uplink audio remains batch-at-utterance-boundary. The payload is the existing
bounded OPUSRAW1 packet stream. M1 does not introduce streaming ASR.

Downlink audio remains incremental. Python emits each available TTS audio
chunk immediately; M1 must not buffer LLM text into phrases or sentences.

## 5. Message Contract

### 5.1 Go to Python

| Type | Required IDs | Result |
|---|---|---|
| `session.open` | session | `session.opened` |
| `session.close` | session | `session.closed` |
| `heartbeat.ping` | session | matching `heartbeat.pong` |
| `input_audio.start` | session, trace, utterance | `orchestrator.status=receiving_audio` |
| VAF1 `input_audio.batch` | session, trace, utterance | retained until matching `input_audio.end` |
| `input_audio.end` | session, trace, utterance | acceptance status, then response stream |
| `input_audio.cancel` | session, trace, utterance | cancellation status |
| `input_text.commit` | session, trace, utterance | acceptance status, then response stream |
| `client_event` | session, trace, request ID | acceptance status, then response stream |
| `interrupt` | session, trace, target round/playback when known | cancellation status or terminal |
| `playback.report` | session, trace, round, playback | acceptance status |

`input_audio.end` is idempotent by `(session_id, utterance_id)`.
`input_text.commit` is idempotent by `(session_id, utterance_id)` and carries
the accepted candidate text plus candidate sequence, speech epoch, audio
watermark, source and ASR timing metadata.
`client_event` is idempotent by `(session_id, event_id)`.
Duplicate accepted requests return their current or terminal status and never
start a second ASR/LLM/TTS execution.

### 5.2 Python to Go

| Type | Required IDs | Meaning |
|---|---|---|
| `session.opened` | session | Registration and limits accepted |
| `session.closed` | session | Python session resources released |
| `heartbeat.pong` | session | Echoes ping nonce |
| `orchestrator.status` | session plus active IDs | Non-terminal state update or request acceptance |
| `response.asr` | session, trace, utterance, round, playback | Final validated ASR text; optional non-blocking client context |
| VAF1 `response.audio` | session, trace, round, playback | Incremental TTS audio chunk |
| `response.done` | session, trace, round, playback | Successful terminal |
| `response.cancelled` | session, trace, round/playback when allocated | Expected cancellation terminal |
| `response.error` | session, trace plus active IDs | Orchestration terminal failure |
| `protocol.error` | session when known | Invalid protocol input; never a business error |
| `trace.event` | applicable IDs | Optional non-terminal diagnostic event |

Exactly one of `response.done`, `response.cancelled`, or `response.error`
terminates an accepted response turn.

`response.asr` is non-terminal. Its payload contains `text`, `valid`, `final`,
and optional `asr_time_ms`. Go forwards it without making ASR delivery a
prerequisite for LLM/TTS or successful turn completion.

## 6. Terminal Payloads and Error Model

### 6.1 Success

```json
{
  "exit": false,
  "reason": "completed",
  "audio_chunks": 42,
  "audio_bytes": 86016
}
```

### 6.2 Cancellation

```json
{
  "reason": "wake_interrupt",
  "cancelled_stage": "tts",
  "expected": true
}
```

Cancellation is not an error and must not be converted into
`ASR_BRIDGE_FAILED` or `LLM_TTS_FAILED`.

### 6.3 Error

```json
{
  "origin": "python_gateway",
  "stage": "tts",
  "code": "TTS_UPSTREAM_UNAVAILABLE",
  "message": "TTS upstream unavailable",
  "cause_code": "UNAVAILABLE",
  "retryable": true,
  "fatal": false
}
```

Error rules:

- Go forwards a valid `response.error` unchanged to Rust.
- Go additionally emits a trace/round/playback-bound `playback_cancel` to the
  Rust-facing client protocol for `response.error`; `response.cancelled` is
  likewise represented as `playback_cancel`. These compatibility terminals do
  not replay the turn or change M1 ownership.
- Go emits `M1_BRIDGE_FAILED` only for transport, framing, dispatch, timeout,
  or protocol failures created in Go.
- Go never wraps a Python business/upstream error into `ASR_BRIDGE_FAILED`.
- `protocol.error` closes the connection only when `fatal=true`.
- Logs may include identifiers and codes, but not robot secrets or raw audio.

## 7. Session and Turn State

```text
connection:
  disconnected -> opening -> idle -> closing -> closed

utterance:
  none -> receiving_audio -> audio_committed -> orchestrating -> none
                    \-> cancelled ---------------------------> none

response:
  none -> accepted -> speaking -> done
                 \-> cancelled
                 \-> error
```

Rules:

- At most one active response round exists per session.
- A new accepted user turn or interrupt first cancels the previous response.
- Audio with a stale `(round_id, playback_id)` is dropped by Go.
- A terminal for a stale response is recorded but cannot change the active
  response state.
- `session.close` cancels active utterance and response work before
  `session.closed`.

## 8. Concurrency and Backpressure

Each side must have:

- exactly one WebSocket reader loop;
- exactly one serialized writer queue;
- no lock held while waiting for a response;
- ID-based dispatch from the reader to pending request/turn state;
- bounded queues with visible drop/backpressure metrics;
- priority ordering: interrupt/session close, control terminal, status, audio.

If downlink audio backpressure exceeds the configured bound, Python cancels the
turn and sends `response.error` with `stage=downlink` when the socket remains
writable. Go must never block interrupt delivery behind queued audio.

## 9. Heartbeat and Reconnect

- Go sends `heartbeat.ping` only while no application message has been received
  during the heartbeat interval.
- Python immediately returns `heartbeat.pong` with the same nonce.
- Missing two consecutive heartbeat deadlines marks the connection dead.
- A dead connection fails all pending dispatch waits exactly once.
- Initial connect and idle reconnect may retry with bounded backoff.
- An accepted `input_audio.end` or `client_event` is never automatically replayed
  after connection loss; Go reports an unknown-outcome bridge error.
- Idle recycling remains resource reclamation, not liveness detection.

## 10. Fallback and Rollback

Automatic per-turn M1 -> M0 fallback is allowed only when all are true:

- M1 has not returned an acceptance status;
- no response status or audio has been received;
- the request is safe to replay;
- the same idempotency key is supplied to M0 during the migration window.

After M1 accepts a request, failure is terminal for that turn. It must not start
a duplicate M0 turn.

Production configuration has one session-level mode:

- `m1`: all active voice semantics use M1;
- `m0`: all active voice semantics use the compatible bridge;
- `shadow`: M0 remains active and M1 receives metadata only.

Per-feature active/ACK-only combinations are migration flags and must be
removed from the production-default configuration after M1 acceptance.

## 11. Implemented Migration Outcome

The migration slices are complete: typed terminal/error/heartbeat events,
serialized readers/writers, real user audio, client events, interrupt, audio
cancel and playback reports all use M1 by default. Historical rollout steps are
kept in Git history; future changes must preserve this contract and M0 rollback.

## 12. Release Regression Gates

V3 release candidates must keep the following gates passing:

- Go and Python contract fixtures encode/decode identical envelopes.
- First wake, normal speech, multi-turn chat, Robot MCP tool use, exit, and
  client-event wake playback.
- Interrupt during ASR, LLM, first TTS chunk, and active playback.
- No stale audio from an old round reaches Rust after interrupt.
- Python restart, Go restart, long idle, missed heartbeat, and reconnect.
- Duplicate `input_audio.end` and `client_event` do not duplicate execution.
- Structured Python errors reach Rust once with the original code.
- Incremental TTS first-audio latency does not regress beyond the agreed test
  tolerance.
- Ten concurrent robot sessions plus soak duration agreed for release.
- Deployment and rollback are exercised from the documented commands.
