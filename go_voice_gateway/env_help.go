package main

import (
	"fmt"
	"io"
	"strings"
	"text/tabwriter"
)

type EnvHelpEntry struct {
	Field   string
	Env     string
	Default string
	Example string
	Notes   string
}

func goGatewayEnvHelpEntries() []EnvHelpEntry {
	return []EnvHelpEntry{
		{Field: "addr", Env: "GO_VOICE_GATEWAY_ADDR", Default: defaultAddr, Example: "export GO_VOICE_GATEWAY_ADDR=0.0.0.0:8282", Notes: "HTTP/WebSocket signaling listen address."},
		{Field: "websocket_path", Env: "GO_VOICE_GATEWAY_WS_PATH", Default: defaultWebSocketPath, Example: "export GO_VOICE_GATEWAY_WS_PATH=/ws", Notes: "WebSocket signaling path."},
		{Field: "bot_id", Env: "GO_VOICE_GATEWAY_BOT_ID, BOT_ID", Default: defaultBotID, Example: "export GO_VOICE_GATEWAY_BOT_ID=xiaowen", Notes: "Default bot id when the client does not provide one."},
		{Field: "rtc_enabled", Env: "GO_VOICE_GATEWAY_RTC_ENABLED", Default: "true", Example: "export GO_VOICE_GATEWAY_RTC_ENABLED=true", Notes: "Enable WebRTC signaling and media handling."},
		{Field: "ice_servers", Env: "GO_VOICE_GATEWAY_ICE_SERVERS, GATEWAY_RTC_ICE_SERVERS, WEBRTC_ICE_SERVERS", Default: "public STUN only; TURN must be injected", Example: `export GO_VOICE_GATEWAY_ICE_SERVERS='[{"urls":["stun:stun.example.com:3478"]},{"urls":["turn:turn.example.com:3478?transport=udp","turns:turn.example.com:443?transport=tcp"],"username":"<turn-user>","credential":"<turn-password>"}]'`, Notes: "JSON or comma-separated ICE server list advertised to clients; never commit TURN credentials."},
		{Field: "ice_transport_policy", Env: "GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY, WEBRTC_ICE_TRANSPORT_POLICY", Default: "all", Example: "export GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY=all", Notes: "Use relay to force TURN-only troubleshooting."},
		{Field: "ice_network_types", Env: "GO_VOICE_GATEWAY_ICE_NETWORK_TYPES, GATEWAY_RTC_ICE_NETWORK_TYPES, WEBRTC_ICE_NETWORK_TYPES", Default: defaultICENetworks, Example: "export GO_VOICE_GATEWAY_ICE_NETWORK_TYPES=udp4", Notes: "Use udp4,udp6 only after IPv6 is validated."},
		{Field: "rtc_udp_min_port", Env: "GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT", Default: fmt.Sprint(defaultRTCUDPMinPort), Example: "export GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT=35500", Notes: "First local UDP port Pion may bind for ICE/media."},
		{Field: "rtc_udp_max_port", Env: "GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT", Default: fmt.Sprint(defaultRTCUDPMaxPort), Example: "export GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT=35600", Notes: "Last local UDP port Pion may bind for ICE/media."},
		{Field: "audio.direction", Env: "GO_VOICE_GATEWAY_RTC_AUDIO_DIRECTION, GATEWAY_RTC_AUDIO_DIRECTION", Default: "sendrecv", Example: "export GO_VOICE_GATEWAY_RTC_AUDIO_DIRECTION=sendrecv", Notes: "SDP audio direction."},
		{Field: "audio.codec", Env: "GO_VOICE_GATEWAY_RTC_AUDIO_CODEC, GATEWAY_RTC_AUDIO_CODEC", Default: "opus", Example: "export GO_VOICE_GATEWAY_RTC_AUDIO_CODEC=opus", Notes: "Current Rust path expects Opus."},
		{Field: "audio.sample_rate", Env: "GO_VOICE_GATEWAY_RTC_AUDIO_SAMPLE_RATE, GATEWAY_RTC_AUDIO_SAMPLE_RATE", Default: "48000", Example: "export GO_VOICE_GATEWAY_RTC_AUDIO_SAMPLE_RATE=48000", Notes: "WebRTC Opus clock rate."},
		{Field: "audio.channels", Env: "GO_VOICE_GATEWAY_RTC_AUDIO_CHANNELS, GATEWAY_RTC_AUDIO_CHANNELS", Default: "1", Example: "export GO_VOICE_GATEWAY_RTC_AUDIO_CHANNELS=1", Notes: "Mono voice stream."},
		{Field: "audio.ptime_ms", Env: "GO_VOICE_GATEWAY_RTC_AUDIO_PTIME_MS, GATEWAY_RTC_AUDIO_PTIME_MS", Default: "20", Example: "export GO_VOICE_GATEWAY_RTC_AUDIO_PTIME_MS=20", Notes: "RTP pacing packetization time."},
		{Field: "audio.downlink_transport", Env: "GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT, GATEWAY_DOWNLINK_AUDIO_TRANSPORT", Default: defaultDownlinkAudioTransport, Example: "export GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT=webrtc_rtp", Notes: "Use websocket only as rollback."},
		{Field: "asr.processor", Env: "GO_VOICE_GATEWAY_ASR_PROCESSOR", Default: asrProcessorPythonGateway, Example: "export GO_VOICE_GATEWAY_ASR_PROCESSOR=python_gateway", Notes: "Bridge WebRTC audio into the existing Python Gateway."},
		{Field: "asr.handoff_dry_run_enabled", Env: "GO_VOICE_GATEWAY_ASR_HANDOFF_DRY_RUN", Default: "false", Example: "export GO_VOICE_GATEWAY_ASR_HANDOFF_DRY_RUN=false", Notes: "Dry-run queues ASR handoffs without calling Python Gateway."},
		{Field: "asr.handoff_queue_size", Env: "GO_VOICE_GATEWAY_ASR_HANDOFF_QUEUE_SIZE", Default: fmt.Sprint(defaultASREncodedAudioHandoffQueueSize), Example: "export GO_VOICE_GATEWAY_ASR_HANDOFF_QUEUE_SIZE=16", Notes: "Queue size for encoded ASR handoffs."},
		{Field: "asr.handoff_end_grace_ms", Env: "GO_VOICE_GATEWAY_ASR_HANDOFF_END_GRACE_MS", Default: fmt.Sprint(defaultASRHandoffEndGraceMS), Example: "export GO_VOICE_GATEWAY_ASR_HANDOFF_END_GRACE_MS=120", Notes: "Short tail wait after audio_end so late RTP packets can join the same utterance; set 0 to disable."},
		{Field: "asr.barge_in_enabled", Env: "GO_VOICE_GATEWAY_BARGE_IN_ENABLED", Default: "true for python_gateway", Example: "export GO_VOICE_GATEWAY_BARGE_IN_ENABLED=false", Notes: "ASR-only natural barge-in handling is ready by default on the Python Gateway processor; set false for an explicit server-side rollback."},
		{Field: "asr.python_gateway_ws_url", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL, GO_VOICE_GATEWAY_PYTHON_GATEWAY_WS_URL", Default: defaultPythonGatewayWSURL, Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL=ws://127.0.0.1:7860/ws", Notes: "Existing Python Gateway WebSocket endpoint."},
		{Field: "asr.python_gateway_connection_mode", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE, GO_VOICE_GATEWAY_PYTHON_GATEWAY_CONNECTION_MODE", Default: pythonGatewayConnectionModeSession, Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE=session", Notes: "Use per_turn only as rollback."},
		{Field: "asr.python_gateway_robot_id", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID, GO_VOICE_GATEWAY_ROBOT_ID, ROBOT_ID", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID=test_01", Notes: "Single-robot fallback; strong-auth sessions prefer registered credentials."},
		{Field: "asr.python_gateway_robot_secret", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET, GO_VOICE_GATEWAY_ROBOT_SECRET, ROBOT_SECRET", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET=...", Notes: "Secret is redacted by --print-config."},
		{Field: "asr.python_gateway_client_type", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CLIENT_TYPE", Default: "go_voice_gateway", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CLIENT_TYPE=go_voice_gateway", Notes: "Client type used for the internal Python Gateway register."},
		{Field: "asr.python_gateway_bot_id", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_BOT_ID, GO_VOICE_GATEWAY_BOT_ID, BOT_ID", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_BOT_ID=xiaowen", Notes: "Optional internal bridge bot override."},
		{Field: "asr.python_gateway_timeout_ms", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TIMEOUT_MS", Default: "30000", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TIMEOUT_MS=30000", Notes: "Internal Python Gateway bridge connect/read/write idle timeout."},
		{Field: "asr.python_gateway_total_timeout_ms", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS, GO_VOICE_GATEWAY_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS", Default: fmt.Sprint(defaultPythonGatewayTotalTimeoutMS), Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS=45000", Notes: "Total budget for one internal Python Gateway action while waiting for done; set 0 to disable."},
		{Field: "asr.python_gateway_max_message_kb", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_MAX_MESSAGE_KB", Default: "1024", Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_MAX_MESSAGE_KB=1024", Notes: "Internal bridge WebSocket message limit."},
		{Field: "asr.python_gateway_session_idle_recycle_ms", Env: "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS", Default: fmt.Sprint(defaultPythonGatewaySessionIdleRecycleMS), Example: "export GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS=30000", Notes: "Recycle an idle reused Python Gateway session WS before the next turn; set 0 to keep it until disconnect."},
		{Field: "asr.turn_gate_shadow_enabled", Env: "GO_VOICE_GATEWAY_TURN_GATE_SHADOW_ENABLED", Default: "false", Example: "export GO_VOICE_GATEWAY_TURN_GATE_SHADOW_ENABLED=true", Notes: "Send provisional candidate audio to Python on an isolated connection; never commits a turn."},
		{Field: "asr.turn_gate_active_enabled", Env: "GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED", Default: "false", Example: "export GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED=true", Notes: "Allow non-shadow candidates to promote cached ASR after cloud freshness checks; keep false until the client active protocol is enabled."},
		{Field: "asr.turn_gate_active_deadline_ms", Env: "GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_DEADLINE_MS", Default: "200", Example: "export GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_DEADLINE_MS=200", Notes: "Absolute candidate-to-commit budget including queue and bridge wait."},
		{Field: "asr.turn_gate_shadow_timeout_ms", Env: "GO_VOICE_GATEWAY_TURN_GATE_SHADOW_TIMEOUT_MS", Default: "5000", Example: "export GO_VOICE_GATEWAY_TURN_GATE_SHADOW_TIMEOUT_MS=5000", Notes: "Total timeout for an isolated Python turn-candidate probe."},
		{Field: "asr.turn_gate_shadow_snapshot_grace_ms", Env: "GO_VOICE_GATEWAY_TURN_GATE_SHADOW_SNAPSHOT_GRACE_MS", Default: "30", Example: "export GO_VOICE_GATEWAY_TURN_GATE_SHADOW_SNAPSHOT_GRACE_MS=30", Notes: "Short non-blocking wait only when RTP packets trail the candidate watermark."},
		{Field: "internal_voice.mode", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE", Default: defaultInternalVoiceMode, Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE=m0", Notes: "Production path owner: m1 is the default typed protocol, shadow keeps M0 active while probing M1, and m0 is the explicit rollback."},
		{Field: "internal_voice.enabled", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_ENABLED", Default: "true", Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_ENABLED=false", Notes: "Legacy migration knob. GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE owns production routing."},
		{Field: "internal_voice.client_event_enabled", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_ENABLED", Default: "true", Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_ENABLED=false", Notes: "Enable M1 client_event handling. Default active path handles wake/interrupt/sleep event TTS through /internal/voice/ws."},
		{Field: "internal_voice.client_event_mode", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", Default: defaultInternalVoiceClientEventMode, Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE=ack_only", Notes: "active routes client_event TTS through /internal/voice/ws with M0 fallback before response bytes are seen; ack_only keeps M0 active and sends only M1 probes."},
		{Field: "internal_voice.client_event_timeout_ms", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_TIMEOUT_MS", Default: fmt.Sprint(defaultInternalVoiceClientEventTimeoutMS), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_TIMEOUT_MS=5000", Notes: "Fast fallback budget for active wake/interrupt/sleep client_event response frames from Python Gateway."},
		{Field: "internal_voice.input_audio_enabled", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_ENABLED", Default: "false", Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_ENABLED=true", Notes: "Enable completed Opus batches on M1; input_audio_mode selects shadow or active ownership."},
		{Field: "internal_voice.input_audio_mode", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_MODE", Default: defaultInternalVoiceInputAudioMode, Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_MODE=active", Notes: "shadow keeps M0 active; active makes M1 own accepted user-audio turns."},
		{Field: "internal_voice.interrupt_enabled", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_INTERRUPT_ENABLED", Default: "false", Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_INTERRUPT_ENABLED=true", Notes: "Send an additional M1 interrupt ACK probe after the existing control bridge path."},
		{Field: "internal_voice.playback_report_enabled", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_PLAYBACK_REPORT_ENABLED", Default: "false", Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_PLAYBACK_REPORT_ENABLED=true", Notes: "Send additional M1 playback.report ACK probes after existing playback completion/interruption reports."},
		{Field: "internal_voice.ws_url", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL", Default: defaultInternalVoiceWSURL, Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL=ws://127.0.0.1:7860/internal/voice/ws", Notes: "Python Gateway M1 internal voice endpoint."},
		{Field: "internal_voice.timeout_ms", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_TIMEOUT_MS", Default: fmt.Sprint(defaultInternalVoiceTimeoutMS), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_TIMEOUT_MS=3000", Notes: "Connect/read/write timeout for the experimental internal voice handshake."},
		{Field: "internal_voice.max_message_kb", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_MAX_MESSAGE_KB", Default: fmt.Sprint(defaultInternalVoiceMaxMessageKB), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_MAX_MESSAGE_KB=1024", Notes: "Maximum JSON message size accepted from the internal voice endpoint."},
		{Field: "internal_voice.heartbeat_interval_ms", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_INTERVAL_MS", Default: fmt.Sprint(defaultInternalVoiceHeartbeatIntervalMS), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_INTERVAL_MS=15000", Notes: "Send an application heartbeat after this much M1 receive-idle time; set 0 to disable."},
		{Field: "internal_voice.heartbeat_timeout_ms", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_TIMEOUT_MS", Default: fmt.Sprint(defaultInternalVoiceHeartbeatTimeoutMS), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_TIMEOUT_MS=5000", Notes: "Maximum wait for a matching M1 heartbeat.pong."},
		{Field: "internal_voice.heartbeat_miss_limit", Env: "GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_MISS_LIMIT", Default: fmt.Sprint(defaultInternalVoiceHeartbeatMissLimit), Example: "export GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_MISS_LIMIT=2", Notes: "Consecutive heartbeat failures before the M1 session is marked dead."},
		{Field: "vision_internal_auth", Env: "GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN=...", Notes: "Bearer token required by the internal latest-snapshot HTTP endpoint; the endpoint stays unavailable when empty and the value is redacted by --print-config."},
		{Field: "device_auth.require_robot_secret", Env: "GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET, GATEWAY_REQUIRE_ROBOT_SECRET", Default: "false", Example: "export GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET=true", Notes: "Production should keep this true."},
		{Field: "device_auth.backend", Env: "GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND", Default: "python_gateway when strong auth is enabled, otherwise none", Example: "export GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND=python_gateway", Notes: "Valid values: python_gateway, none."},
		{Field: "local_interface_name", Env: "GO_VOICE_GATEWAY_INTERFACE", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_INTERFACE=eth0", Notes: "Optional local network-interface filter."},
		{Field: "local_ip", Env: "GO_VOICE_GATEWAY_IP", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_IP=192.0.2.10", Notes: "Optional local IP filter."},
		{Field: "allowed_origins", Env: "GO_VOICE_GATEWAY_ALLOWED_ORIGINS", Default: "*", Example: "export GO_VOICE_GATEWAY_ALLOWED_ORIGINS=*", Notes: "Comma-separated WebSocket Origin allowlist."},
		{Field: "message_limit_bytes", Env: "GO_VOICE_GATEWAY_MESSAGE_LIMIT_BYTES", Default: fmt.Sprint(defaultMessageMaxByte), Example: "export GO_VOICE_GATEWAY_MESSAGE_LIMIT_BYTES=1048576", Notes: "Client WebSocket message limit."},
		{Field: "negotiation_timeout_ms", Env: "GO_VOICE_GATEWAY_NEGOTIATION_TIMEOUT_MS", Default: "5000", Example: "export GO_VOICE_GATEWAY_NEGOTIATION_TIMEOUT_MS=5000", Notes: "SDP/ICE signaling write timeout budget."},
		{Field: "write_timeout_ms", Env: "GO_VOICE_GATEWAY_WRITE_TIMEOUT_MS", Default: "3000", Example: "export GO_VOICE_GATEWAY_WRITE_TIMEOUT_MS=3000", Notes: "WebSocket write timeout."},
		{Field: "log_format", Env: "GO_VOICE_GATEWAY_LOG_FORMAT, VOICE_LOG_FORMAT, LOG_FORMAT", Default: defaultLogFormat, Example: "export GO_VOICE_GATEWAY_LOG_FORMAT=json", Notes: "Valid values include text and json."},
		{Field: "log_level", Env: "GO_VOICE_GATEWAY_LOG_LEVEL, VOICE_LOG_LEVEL, LOG_LEVEL", Default: defaultLogLevel, Example: "export GO_VOICE_GATEWAY_LOG_LEVEL=info", Notes: "Valid values include debug, info, warn, error."},
		{Field: "log_file", Env: "GO_VOICE_GATEWAY_LOG_FILE, VOICE_LOG_FILE, LOG_FILE", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_LOG_FILE=/var/log/go_voice_gateway.log", Notes: "Optional explicit log file."},
		{Field: "log_dir", Env: "GO_VOICE_GATEWAY_LOG_DIR, VOICE_LOG_DIR, LOG_DIR", Default: "<empty>", Example: "export GO_VOICE_GATEWAY_LOG_DIR=/var/log/ai-voice", Notes: "Optional log directory; service name is used as filename."},
		{Field: "log_max_bytes", Env: "GO_VOICE_GATEWAY_LOG_MAX_BYTES, VOICE_LOG_MAX_BYTES, LOG_MAX_BYTES", Default: fmt.Sprint(defaultLogMaxBytes), Example: "export GO_VOICE_GATEWAY_LOG_MAX_BYTES=10485760", Notes: "Set 0 to disable rotation limit."},
		{Field: "log_backup_count", Env: "GO_VOICE_GATEWAY_LOG_BACKUP_COUNT, VOICE_LOG_BACKUP_COUNT, LOG_BACKUP_COUNT", Default: fmt.Sprint(defaultLogBackupCount), Example: "export GO_VOICE_GATEWAY_LOG_BACKUP_COUNT=5", Notes: "Number of rotated log files to keep."},
	}
}

func printEnvHelp(w io.Writer, title string, entries []EnvHelpEntry) error {
	fmt.Fprintf(w, "%s\n\n", title)
	fmt.Fprintln(w, "Usage:")
	fmt.Fprintln(w, "  export NAME=value")
	fmt.Fprintln(w, "  NAME=value ./go_voice_gateway --print-config")
	fmt.Fprintln(w)

	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, "FIELD\tENV VARS\tDEFAULT\tEXAMPLE\tNOTES")
	for _, entry := range entries {
		fmt.Fprintf(
			tw,
			"%s\t%s\t%s\t%s\t%s\n",
			entry.Field,
			entry.Env,
			entry.Default,
			entry.Example,
			normalizeEnvHelpWhitespace(entry.Notes),
		)
	}
	return tw.Flush()
}

func normalizeEnvHelpWhitespace(value string) string {
	return strings.Join(strings.Fields(value), " ")
}
