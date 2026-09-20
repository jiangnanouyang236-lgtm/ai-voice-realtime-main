package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/pion/ice/v4"
	"github.com/pion/webrtc/v4"
)

const (
	defaultAddr           = "127.0.0.1:8282"
	defaultWebSocketPath  = "/ws"
	defaultBotID          = "xiaowen"
	defaultICEServers     = `[{"urls":["stun:frp.wzk.icu:3478"]}]`
	defaultICENetworks    = "udp4"
	defaultRTCUDPMinPort  = 35500
	defaultRTCUDPMaxPort  = 35600
	defaultMessageMaxByte = 1 << 20

	asrProcessorDryRun                       = "dry_run"
	asrProcessorPythonGateway                = "python_gateway"
	defaultPythonGatewayWSURL                = "ws://127.0.0.1:7860/ws"
	defaultPythonGatewayTotalTimeoutMS       = 45000
	defaultPythonGatewaySessionIdleRecycleMS = 30000
	defaultASRHandoffEndGraceMS              = defaultASRPrototypeEndGraceMS
	defaultInternalVoiceWSURL                = "ws://127.0.0.1:7860/internal/voice/ws"
	defaultInternalVoiceMode                 = "m1"
	defaultInternalVoiceTimeoutMS            = 3000
	defaultInternalVoiceClientEventMode      = "active"
	defaultInternalVoiceInputAudioMode       = "shadow"
	defaultInternalVoiceClientEventTimeoutMS = 5000
	defaultInternalVoiceMaxMessageKB         = 1024
	defaultInternalVoiceHeartbeatIntervalMS  = 15000
	defaultInternalVoiceHeartbeatTimeoutMS   = 5000
	defaultInternalVoiceHeartbeatMissLimit   = 2

	pythonGatewayConnectionModePerTurn = "per_turn"
	pythonGatewayConnectionModeSession = "session"

	deviceAuthBackendNone          = "none"
	deviceAuthBackendPythonGateway = "python_gateway"
)

type Config struct {
	Addr                string
	WebSocketPath       string
	BotID               string
	LogFormat           string
	LogLevel            string
	LogFile             string
	LogDir              string
	LogMaxBytes         int
	LogBackupCount      int
	RTCEnabled          bool
	ICEServers          []ICEServerConfig
	ICETransportPolicy  webrtc.ICETransportPolicy
	ICENetworkTypes     []webrtc.NetworkType
	RTCUDPMinPort       int
	RTCUDPMaxPort       int
	Audio               AudioConfig
	ASR                 ASRConfig
	InternalVoice       InternalVoiceConfig
	VisionInternalToken string
	DeviceAuth          DeviceAuthConfig
	LocalInterfaceName  string
	LocalIP             string
	AllowedOrigins      []string
	MessageLimitBytes   int64
	NegotiationTimeout  time.Duration
	WriteTimeout        time.Duration
}

type AudioConfig struct {
	Direction         string `json:"direction"`
	Codec             string `json:"codec"`
	SampleRate        uint32 `json:"sample_rate"`
	Channels          uint16 `json:"channels"`
	PtimeMS           uint16 `json:"ptime_ms"`
	DownlinkTransport string `json:"downlink_transport"`
}

type ASRConfig struct {
	Processor                         string `json:"processor,omitempty"`
	HandoffDryRunEnabled              bool   `json:"handoff_dry_run_enabled"`
	HandoffQueueSize                  int    `json:"handoff_queue_size"`
	HandoffEndGraceMS                 int    `json:"handoff_end_grace_ms"`
	PythonGatewayWSURL                string `json:"python_gateway_ws_url,omitempty"`
	PythonGatewayRobotID              string `json:"python_gateway_robot_id,omitempty"`
	PythonGatewayRobotSecret          string `json:"-"`
	PythonGatewayClientType           string `json:"python_gateway_client_type,omitempty"`
	PythonGatewayBotID                string `json:"python_gateway_bot_id,omitempty"`
	PythonGatewayTimeoutMS            int    `json:"python_gateway_timeout_ms,omitempty"`
	PythonGatewayTotalTimeoutMS       int    `json:"python_gateway_total_timeout_ms,omitempty"`
	PythonGatewayMaxMessageKB         int    `json:"python_gateway_max_message_kb,omitempty"`
	PythonGatewayConnectionMode       string `json:"python_gateway_connection_mode,omitempty"`
	PythonGatewaySessionIdleRecycleMS int    `json:"python_gateway_session_idle_recycle_ms"`
	TurnGateShadowEnabled             bool   `json:"turn_gate_shadow_enabled"`
	TurnGateActiveEnabled             bool   `json:"turn_gate_active_enabled"`
	TurnGateActiveDeadlineMS          int    `json:"turn_gate_active_deadline_ms"`
	TurnGateShadowTimeoutMS           int    `json:"turn_gate_shadow_timeout_ms"`
	TurnGateShadowSnapshotGraceMS     int    `json:"turn_gate_shadow_snapshot_grace_ms"`
	BargeInEnabled                    bool   `json:"barge_in_enabled"`
}

type InternalVoiceConfig struct {
	Mode                  string        `json:"mode"`
	Enabled               bool          `json:"enabled"`
	ClientEventEnabled    bool          `json:"client_event_enabled"`
	ClientEventMode       string        `json:"client_event_mode"`
	ClientEventTimeout    time.Duration `json:"-"`
	ClientEventTimeoutMS  int           `json:"client_event_timeout_ms,omitempty"`
	InputAudioEnabled     bool          `json:"input_audio_enabled"`
	InputAudioMode        string        `json:"input_audio_mode"`
	InterruptEnabled      bool          `json:"interrupt_enabled"`
	PlaybackReportEnabled bool          `json:"playback_report_enabled"`
	WSURL                 string        `json:"ws_url,omitempty"`
	Timeout               time.Duration `json:"-"`
	TimeoutMS             int           `json:"timeout_ms,omitempty"`
	MaxMessageKB          int           `json:"max_message_kb,omitempty"`
	MaxMessageBytes       int           `json:"-"`
	HeartbeatInterval     time.Duration `json:"-"`
	HeartbeatIntervalMS   int           `json:"heartbeat_interval_ms"`
	HeartbeatTimeout      time.Duration `json:"-"`
	HeartbeatTimeoutMS    int           `json:"heartbeat_timeout_ms"`
	HeartbeatMissLimit    int           `json:"heartbeat_miss_limit"`
}

type DeviceAuthConfig struct {
	RequireRobotSecret bool   `json:"require_robot_secret"`
	Backend            string `json:"backend"`
}

type ICEServerConfig struct {
	URLs       []string `json:"urls"`
	Username   string   `json:"username,omitempty"`
	Credential string   `json:"credential,omitempty"`
}

type PrintableConfig struct {
	Addr               string                       `json:"addr"`
	WebSocketPath      string                       `json:"websocket_path"`
	BotID              string                       `json:"bot_id"`
	LogFormat          string                       `json:"log_format"`
	LogLevel           string                       `json:"log_level"`
	LogFile            string                       `json:"log_file,omitempty"`
	LogDir             string                       `json:"log_dir,omitempty"`
	LogMaxBytes        int                          `json:"log_max_bytes"`
	LogBackupCount     int                          `json:"log_backup_count"`
	RTCEnabled         bool                         `json:"rtc_enabled"`
	ICEServers         []PrintableICEServerConfig   `json:"ice_servers"`
	ICETransportPolicy string                       `json:"ice_transport_policy"`
	ICENetworkTypes    []string                     `json:"ice_network_types"`
	RTCUDPMinPort      int                          `json:"rtc_udp_min_port"`
	RTCUDPMaxPort      int                          `json:"rtc_udp_max_port"`
	Audio              AudioConfig                  `json:"audio"`
	ASR                PrintableASRConfig           `json:"asr"`
	InternalVoice      PrintableInternalVoiceConfig `json:"internal_voice"`
	VisionInternalAuth bool                         `json:"vision_internal_auth"`
	DeviceAuth         DeviceAuthConfig             `json:"device_auth"`
	LocalInterfaceName string                       `json:"local_interface_name,omitempty"`
	LocalIP            string                       `json:"local_ip,omitempty"`
	AllowedOrigins     []string                     `json:"allowed_origins,omitempty"`
	MessageLimitBytes  int64                        `json:"message_limit_bytes"`
	NegotiationTimeout int64                        `json:"negotiation_timeout_ms"`
	WriteTimeout       int64                        `json:"write_timeout_ms"`
}

type PrintableICEServerConfig struct {
	URLs       []string `json:"urls"`
	Username   string   `json:"username,omitempty"`
	Credential string   `json:"credential,omitempty"`
}

type PrintableASRConfig struct {
	Processor                         string `json:"processor,omitempty"`
	HandoffDryRunEnabled              bool   `json:"handoff_dry_run_enabled"`
	HandoffQueueSize                  int    `json:"handoff_queue_size"`
	HandoffEndGraceMS                 int    `json:"handoff_end_grace_ms"`
	PythonGatewayWSURL                string `json:"python_gateway_ws_url,omitempty"`
	PythonGatewayRobotID              string `json:"python_gateway_robot_id,omitempty"`
	PythonGatewayRobotSecret          string `json:"python_gateway_robot_secret,omitempty"`
	PythonGatewayClientType           string `json:"python_gateway_client_type,omitempty"`
	PythonGatewayBotID                string `json:"python_gateway_bot_id,omitempty"`
	PythonGatewayTimeoutMS            int    `json:"python_gateway_timeout_ms,omitempty"`
	PythonGatewayTotalTimeoutMS       int    `json:"python_gateway_total_timeout_ms,omitempty"`
	PythonGatewayMaxMessageKB         int    `json:"python_gateway_max_message_kb,omitempty"`
	PythonGatewayConnectionMode       string `json:"python_gateway_connection_mode,omitempty"`
	PythonGatewaySessionIdleRecycleMS int    `json:"python_gateway_session_idle_recycle_ms"`
	TurnGateShadowEnabled             bool   `json:"turn_gate_shadow_enabled"`
	TurnGateActiveEnabled             bool   `json:"turn_gate_active_enabled"`
	TurnGateActiveDeadlineMS          int    `json:"turn_gate_active_deadline_ms"`
	TurnGateShadowTimeoutMS           int    `json:"turn_gate_shadow_timeout_ms"`
	TurnGateShadowSnapshotGraceMS     int    `json:"turn_gate_shadow_snapshot_grace_ms"`
	BargeInEnabled                    bool   `json:"barge_in_enabled"`
}

type PrintableInternalVoiceConfig struct {
	Mode                  string `json:"mode"`
	Enabled               bool   `json:"enabled"`
	ClientEventEnabled    bool   `json:"client_event_enabled"`
	ClientEventMode       string `json:"client_event_mode"`
	ClientEventTimeoutMS  int    `json:"client_event_timeout_ms,omitempty"`
	InputAudioEnabled     bool   `json:"input_audio_enabled"`
	InputAudioMode        string `json:"input_audio_mode"`
	InterruptEnabled      bool   `json:"interrupt_enabled"`
	PlaybackReportEnabled bool   `json:"playback_report_enabled"`
	WSURL                 string `json:"ws_url,omitempty"`
	TimeoutMS             int    `json:"timeout_ms,omitempty"`
	MaxMessageKB          int    `json:"max_message_kb,omitempty"`
	HeartbeatIntervalMS   int    `json:"heartbeat_interval_ms"`
	HeartbeatTimeoutMS    int    `json:"heartbeat_timeout_ms"`
	HeartbeatMissLimit    int    `json:"heartbeat_miss_limit"`
}

func (cfg Config) Printable() PrintableConfig {
	printableICEServers := make([]PrintableICEServerConfig, 0, len(cfg.ICEServers))
	for _, server := range cfg.ICEServers {
		printableICEServers = append(printableICEServers, PrintableICEServerConfig{
			URLs:       append([]string(nil), server.URLs...),
			Username:   server.Username,
			Credential: secretStatus(server.Credential),
		})
	}
	return PrintableConfig{
		Addr:               cfg.Addr,
		WebSocketPath:      cfg.WebSocketPath,
		BotID:              cfg.BotID,
		LogFormat:          cfg.LogFormat,
		LogLevel:           cfg.LogLevel,
		LogFile:            cfg.LogFile,
		LogDir:             cfg.LogDir,
		LogMaxBytes:        cfg.LogMaxBytes,
		LogBackupCount:     cfg.LogBackupCount,
		RTCEnabled:         cfg.RTCEnabled,
		ICEServers:         printableICEServers,
		ICETransportPolicy: cfg.ICETransportPolicy.String(),
		ICENetworkTypes:    iceNetworkTypeStrings(cfg.ICENetworkTypes),
		RTCUDPMinPort:      cfg.RTCUDPMinPort,
		RTCUDPMaxPort:      cfg.RTCUDPMaxPort,
		Audio:              cfg.Audio,
		ASR: PrintableASRConfig{
			Processor:                         cfg.ASR.Processor,
			HandoffDryRunEnabled:              cfg.ASR.HandoffDryRunEnabled,
			HandoffQueueSize:                  cfg.ASR.HandoffQueueSize,
			HandoffEndGraceMS:                 cfg.ASR.HandoffEndGraceMS,
			PythonGatewayWSURL:                cfg.ASR.PythonGatewayWSURL,
			PythonGatewayRobotID:              cfg.ASR.PythonGatewayRobotID,
			PythonGatewayRobotSecret:          secretStatus(cfg.ASR.PythonGatewayRobotSecret),
			PythonGatewayClientType:           cfg.ASR.PythonGatewayClientType,
			PythonGatewayBotID:                cfg.ASR.PythonGatewayBotID,
			PythonGatewayTimeoutMS:            cfg.ASR.PythonGatewayTimeoutMS,
			PythonGatewayTotalTimeoutMS:       cfg.ASR.PythonGatewayTotalTimeoutMS,
			PythonGatewayMaxMessageKB:         cfg.ASR.PythonGatewayMaxMessageKB,
			PythonGatewayConnectionMode:       cfg.ASR.PythonGatewayConnectionMode,
			PythonGatewaySessionIdleRecycleMS: cfg.ASR.PythonGatewaySessionIdleRecycleMS,
			TurnGateShadowEnabled:             cfg.ASR.TurnGateShadowEnabled,
			TurnGateActiveEnabled:             cfg.ASR.TurnGateActiveEnabled,
			TurnGateActiveDeadlineMS:          cfg.ASR.TurnGateActiveDeadlineMS,
			TurnGateShadowTimeoutMS:           cfg.ASR.TurnGateShadowTimeoutMS,
			TurnGateShadowSnapshotGraceMS:     cfg.ASR.TurnGateShadowSnapshotGraceMS,
			BargeInEnabled:                    cfg.ASR.BargeInEnabled,
		},
		InternalVoice: PrintableInternalVoiceConfig{
			Mode:                  cfg.InternalVoice.Mode,
			Enabled:               cfg.InternalVoice.Enabled,
			ClientEventEnabled:    cfg.InternalVoice.ClientEventEnabled,
			ClientEventMode:       cfg.InternalVoice.ClientEventMode,
			ClientEventTimeoutMS:  cfg.InternalVoice.ClientEventTimeoutMS,
			InputAudioEnabled:     cfg.InternalVoice.InputAudioEnabled,
			InputAudioMode:        cfg.InternalVoice.InputAudioMode,
			InterruptEnabled:      cfg.InternalVoice.InterruptEnabled,
			PlaybackReportEnabled: cfg.InternalVoice.PlaybackReportEnabled,
			WSURL:                 cfg.InternalVoice.WSURL,
			TimeoutMS:             cfg.InternalVoice.TimeoutMS,
			MaxMessageKB:          cfg.InternalVoice.MaxMessageKB,
			HeartbeatIntervalMS:   cfg.InternalVoice.HeartbeatIntervalMS,
			HeartbeatTimeoutMS:    cfg.InternalVoice.HeartbeatTimeoutMS,
			HeartbeatMissLimit:    cfg.InternalVoice.HeartbeatMissLimit,
		},
		VisionInternalAuth: strings.TrimSpace(cfg.VisionInternalToken) != "",
		DeviceAuth:         cfg.DeviceAuth,
		LocalInterfaceName: cfg.LocalInterfaceName,
		LocalIP:            cfg.LocalIP,
		AllowedOrigins:     append([]string(nil), cfg.AllowedOrigins...),
		MessageLimitBytes:  cfg.MessageLimitBytes,
		NegotiationTimeout: cfg.NegotiationTimeout.Milliseconds(),
		WriteTimeout:       cfg.WriteTimeout.Milliseconds(),
	}
}

func secretStatus(value string) string {
	if strings.TrimSpace(value) == "" {
		return ""
	}
	return "<set>"
}

func LoadConfigFromEnv() (Config, error) {
	iceServers, err := parseICEServers(envFirstNonEmpty(
		defaultICEServers,
		"GO_VOICE_GATEWAY_ICE_SERVERS",
		"GATEWAY_RTC_ICE_SERVERS",
		"WEBRTC_ICE_SERVERS",
	))
	if err != nil {
		return Config{}, err
	}
	policy, err := parseICETransportPolicy(envFirstNonEmpty(
		"all",
		"GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY",
		"WEBRTC_ICE_TRANSPORT_POLICY",
	))
	if err != nil {
		return Config{}, err
	}
	networkTypes, err := parseICENetworkTypes(envFirstNonEmpty(
		defaultICENetworks,
		"GO_VOICE_GATEWAY_ICE_NETWORK_TYPES",
		"GATEWAY_RTC_ICE_NETWORK_TYPES",
		"WEBRTC_ICE_NETWORK_TYPES",
	))
	if err != nil {
		return Config{}, err
	}
	downlinkAudioTransport, err := parseDownlinkAudioTransport(envFirstNonEmpty(
		defaultDownlinkAudioTransport,
		"GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT",
		"GATEWAY_DOWNLINK_AUDIO_TRANSPORT",
	))
	if err != nil {
		return Config{}, err
	}

	asrDryRunEnabled := envBool("GO_VOICE_GATEWAY_ASR_HANDOFF_DRY_RUN", false)
	asrProcessor := strings.ToLower(envFirstNonEmpty("", "GO_VOICE_GATEWAY_ASR_PROCESSOR"))
	if asrProcessor == "" {
		if asrDryRunEnabled {
			asrProcessor = asrProcessorDryRun
		} else {
			asrProcessor = asrProcessorPythonGateway
		}
	}
	pythonGatewayConnectionMode, err := parsePythonGatewayConnectionMode(envFirstNonEmpty(
		pythonGatewayConnectionModeSession,
		"GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE",
		"GO_VOICE_GATEWAY_PYTHON_GATEWAY_CONNECTION_MODE",
	))
	if err != nil {
		return Config{}, err
	}
	requireRobotSecret := envBool(
		"GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET",
		envBool("GATEWAY_REQUIRE_ROBOT_SECRET", false),
	)
	deviceAuthBackend := strings.ToLower(envFirstNonEmpty(
		"",
		"GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND",
	))
	if deviceAuthBackend == "" {
		if requireRobotSecret {
			deviceAuthBackend = deviceAuthBackendPythonGateway
		} else {
			deviceAuthBackend = deviceAuthBackendNone
		}
	}

	cfg := Config{
		Addr:               envFirstNonEmpty(defaultAddr, "GO_VOICE_GATEWAY_ADDR"),
		WebSocketPath:      normalizePath(envFirstNonEmpty(defaultWebSocketPath, "GO_VOICE_GATEWAY_WS_PATH")),
		BotID:              envFirstNonEmpty(defaultBotID, "GO_VOICE_GATEWAY_BOT_ID", "BOT_ID"),
		LogFormat:          strings.ToLower(envFirstNonEmpty(defaultLogFormat, "GO_VOICE_GATEWAY_LOG_FORMAT", "VOICE_LOG_FORMAT", "LOG_FORMAT")),
		LogLevel:           strings.ToLower(envFirstNonEmpty(defaultLogLevel, "GO_VOICE_GATEWAY_LOG_LEVEL", "VOICE_LOG_LEVEL", "LOG_LEVEL")),
		LogFile:            envFirstNonEmpty("", "GO_VOICE_GATEWAY_LOG_FILE", "VOICE_LOG_FILE", "LOG_FILE"),
		LogDir:             envFirstNonEmpty("", "GO_VOICE_GATEWAY_LOG_DIR", "VOICE_LOG_DIR", "LOG_DIR"),
		LogMaxBytes:        envIntFirst(defaultLogMaxBytes, "GO_VOICE_GATEWAY_LOG_MAX_BYTES", "VOICE_LOG_MAX_BYTES", "LOG_MAX_BYTES"),
		LogBackupCount:     envIntFirst(defaultLogBackupCount, "GO_VOICE_GATEWAY_LOG_BACKUP_COUNT", "VOICE_LOG_BACKUP_COUNT", "LOG_BACKUP_COUNT"),
		RTCEnabled:         envBool("GO_VOICE_GATEWAY_RTC_ENABLED", true),
		ICEServers:         iceServers,
		ICETransportPolicy: policy,
		ICENetworkTypes:    networkTypes,
		RTCUDPMinPort:      envInt("GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT", defaultRTCUDPMinPort),
		RTCUDPMaxPort:      envInt("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT", defaultRTCUDPMaxPort),
		Audio: AudioConfig{
			Direction:         envFirstNonEmpty("sendrecv", "GO_VOICE_GATEWAY_RTC_AUDIO_DIRECTION", "GATEWAY_RTC_AUDIO_DIRECTION"),
			Codec:             envFirstNonEmpty("opus", "GO_VOICE_GATEWAY_RTC_AUDIO_CODEC", "GATEWAY_RTC_AUDIO_CODEC"),
			SampleRate:        uint32(envInt("GO_VOICE_GATEWAY_RTC_AUDIO_SAMPLE_RATE", envInt("GATEWAY_RTC_AUDIO_SAMPLE_RATE", 48000))),
			Channels:          uint16(envInt("GO_VOICE_GATEWAY_RTC_AUDIO_CHANNELS", envInt("GATEWAY_RTC_AUDIO_CHANNELS", 1))),
			PtimeMS:           uint16(envInt("GO_VOICE_GATEWAY_RTC_AUDIO_PTIME_MS", envInt("GATEWAY_RTC_AUDIO_PTIME_MS", 20))),
			DownlinkTransport: downlinkAudioTransport,
		},
		ASR: ASRConfig{
			Processor:                         asrProcessor,
			HandoffDryRunEnabled:              asrDryRunEnabled || asrProcessor == asrProcessorDryRun,
			HandoffQueueSize:                  envInt("GO_VOICE_GATEWAY_ASR_HANDOFF_QUEUE_SIZE", defaultASREncodedAudioHandoffQueueSize),
			HandoffEndGraceMS:                 envInt("GO_VOICE_GATEWAY_ASR_HANDOFF_END_GRACE_MS", defaultASRHandoffEndGraceMS),
			PythonGatewayWSURL:                envFirstNonEmpty(defaultPythonGatewayWSURL, "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL", "GO_VOICE_GATEWAY_PYTHON_GATEWAY_WS_URL"),
			PythonGatewayRobotID:              envFirstNonEmpty("", "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID", "GO_VOICE_GATEWAY_ROBOT_ID", "ROBOT_ID"),
			PythonGatewayRobotSecret:          envFirstNonEmpty("", "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET", "GO_VOICE_GATEWAY_ROBOT_SECRET", "ROBOT_SECRET"),
			PythonGatewayClientType:           envFirstNonEmpty("go_voice_gateway", "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CLIENT_TYPE"),
			PythonGatewayBotID:                envFirstNonEmpty("", "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_BOT_ID", "GO_VOICE_GATEWAY_BOT_ID", "BOT_ID"),
			PythonGatewayTimeoutMS:            envInt("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TIMEOUT_MS", 30000),
			PythonGatewayTotalTimeoutMS:       envIntFirst(defaultPythonGatewayTotalTimeoutMS, "GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS", "GO_VOICE_GATEWAY_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS"),
			PythonGatewayMaxMessageKB:         envInt("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_MAX_MESSAGE_KB", 1024),
			PythonGatewayConnectionMode:       pythonGatewayConnectionMode,
			PythonGatewaySessionIdleRecycleMS: envInt("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS", defaultPythonGatewaySessionIdleRecycleMS),
			TurnGateShadowEnabled:             envBool("GO_VOICE_GATEWAY_TURN_GATE_SHADOW_ENABLED", false),
			TurnGateActiveEnabled:             envBool("GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED", false),
			TurnGateActiveDeadlineMS:          envInt("GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_DEADLINE_MS", 200),
			TurnGateShadowTimeoutMS:           envInt("GO_VOICE_GATEWAY_TURN_GATE_SHADOW_TIMEOUT_MS", 5000),
			TurnGateShadowSnapshotGraceMS:     envInt("GO_VOICE_GATEWAY_TURN_GATE_SHADOW_SNAPSHOT_GRACE_MS", 30),
			BargeInEnabled:                    envBool("GO_VOICE_GATEWAY_BARGE_IN_ENABLED", asrProcessor == asrProcessorPythonGateway),
		},
		InternalVoice: InternalVoiceConfig{
			Mode:                  envFirstNonEmpty(defaultInternalVoiceMode, "GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE"),
			Enabled:               envBool("GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_ENABLED", true),
			ClientEventEnabled:    envBool("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_ENABLED", true),
			ClientEventMode:       envFirstNonEmpty(defaultInternalVoiceClientEventMode, "GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE"),
			ClientEventTimeoutMS:  envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_TIMEOUT_MS", defaultInternalVoiceClientEventTimeoutMS),
			InputAudioEnabled:     envBool("GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_ENABLED", false),
			InputAudioMode:        envFirstNonEmpty(defaultInternalVoiceInputAudioMode, "GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_MODE"),
			InterruptEnabled:      envBool("GO_VOICE_GATEWAY_INTERNAL_VOICE_INTERRUPT_ENABLED", false),
			PlaybackReportEnabled: envBool("GO_VOICE_GATEWAY_INTERNAL_VOICE_PLAYBACK_REPORT_ENABLED", false),
			WSURL:                 envFirstNonEmpty(defaultInternalVoiceWSURL, "GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL"),
			TimeoutMS:             envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_TIMEOUT_MS", defaultInternalVoiceTimeoutMS),
			MaxMessageKB:          envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_MAX_MESSAGE_KB", defaultInternalVoiceMaxMessageKB),
			HeartbeatIntervalMS:   envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_INTERVAL_MS", defaultInternalVoiceHeartbeatIntervalMS),
			HeartbeatTimeoutMS:    envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_TIMEOUT_MS", defaultInternalVoiceHeartbeatTimeoutMS),
			HeartbeatMissLimit:    envInt("GO_VOICE_GATEWAY_INTERNAL_VOICE_HEARTBEAT_MISS_LIMIT", defaultInternalVoiceHeartbeatMissLimit),
		},
		VisionInternalToken: strings.TrimSpace(os.Getenv("GO_VOICE_GATEWAY_VISION_INTERNAL_TOKEN")),
		DeviceAuth: DeviceAuthConfig{
			RequireRobotSecret: requireRobotSecret,
			Backend:            deviceAuthBackend,
		},
		LocalInterfaceName: strings.TrimSpace(os.Getenv("GO_VOICE_GATEWAY_INTERFACE")),
		LocalIP:            strings.TrimSpace(os.Getenv("GO_VOICE_GATEWAY_IP")),
		AllowedOrigins:     csvEnvDefault("*", "GO_VOICE_GATEWAY_ALLOWED_ORIGINS"),
		MessageLimitBytes:  int64(envInt("GO_VOICE_GATEWAY_MESSAGE_LIMIT_BYTES", defaultMessageMaxByte)),
		NegotiationTimeout: time.Duration(maxInt(envInt("GO_VOICE_GATEWAY_NEGOTIATION_TIMEOUT_MS", 5000), 1)) * time.Millisecond,
		WriteTimeout:       time.Duration(maxInt(envInt("GO_VOICE_GATEWAY_WRITE_TIMEOUT_MS", 3000), 1)) * time.Millisecond,
	}
	if cfg.MessageLimitBytes <= 0 {
		cfg.MessageLimitBytes = defaultMessageMaxByte
	}
	if cfg.LogMaxBytes < 0 {
		cfg.LogMaxBytes = defaultLogMaxBytes
	}
	if cfg.LogBackupCount < 0 {
		cfg.LogBackupCount = defaultLogBackupCount
	}
	if cfg.RTCUDPMinPort <= 0 || cfg.RTCUDPMinPort > 65535 {
		return Config{}, fmt.Errorf("GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT must be between 1 and 65535")
	}
	if cfg.RTCUDPMaxPort <= 0 || cfg.RTCUDPMaxPort > 65535 {
		return Config{}, fmt.Errorf("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT must be between 1 and 65535")
	}
	if cfg.RTCUDPMaxPort < cfg.RTCUDPMinPort {
		return Config{}, fmt.Errorf("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT must be >= GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT")
	}
	if cfg.Audio.Direction == "" {
		cfg.Audio.Direction = "sendrecv"
	}
	if cfg.Audio.Codec == "" {
		cfg.Audio.Codec = "opus"
	}
	if cfg.Audio.SampleRate == 0 {
		cfg.Audio.SampleRate = 48000
	}
	if cfg.Audio.Channels == 0 {
		cfg.Audio.Channels = 1
	}
	if cfg.Audio.PtimeMS == 0 {
		cfg.Audio.PtimeMS = 20
	}
	if cfg.Audio.DownlinkTransport == "" {
		cfg.Audio.DownlinkTransport = defaultDownlinkAudioTransport
	}
	if cfg.ASR.HandoffQueueSize <= 0 {
		cfg.ASR.HandoffQueueSize = defaultASREncodedAudioHandoffQueueSize
	}
	if cfg.ASR.HandoffEndGraceMS < 0 {
		cfg.ASR.HandoffEndGraceMS = defaultASRHandoffEndGraceMS
	}
	if cfg.ASR.PythonGatewayTimeoutMS <= 0 {
		cfg.ASR.PythonGatewayTimeoutMS = 30000
	}
	if cfg.ASR.PythonGatewayTotalTimeoutMS < 0 {
		cfg.ASR.PythonGatewayTotalTimeoutMS = defaultPythonGatewayTotalTimeoutMS
	}
	if cfg.ASR.PythonGatewayMaxMessageKB <= 0 {
		cfg.ASR.PythonGatewayMaxMessageKB = 1024
	}
	if cfg.ASR.PythonGatewaySessionIdleRecycleMS < 0 {
		cfg.ASR.PythonGatewaySessionIdleRecycleMS = defaultPythonGatewaySessionIdleRecycleMS
	}
	if cfg.ASR.TurnGateShadowTimeoutMS <= 0 {
		cfg.ASR.TurnGateShadowTimeoutMS = 5000
	}
	if cfg.ASR.TurnGateShadowSnapshotGraceMS < 0 {
		cfg.ASR.TurnGateShadowSnapshotGraceMS = 30
	}
	if cfg.ASR.TurnGateActiveEnabled {
		if cfg.ASR.Processor != asrProcessorPythonGateway {
			return Config{}, fmt.Errorf("Turn Gate active mode requires GO_VOICE_GATEWAY_ASR_PROCESSOR=%s", asrProcessorPythonGateway)
		}
		cfg.ASR.TurnGateShadowEnabled = true
	}
	if cfg.ASR.BargeInEnabled && cfg.ASR.Processor != asrProcessorPythonGateway {
		return Config{}, fmt.Errorf("natural barge-in requires GO_VOICE_GATEWAY_ASR_PROCESSOR=%s", asrProcessorPythonGateway)
	}
	if cfg.ASR.TurnGateActiveDeadlineMS <= 0 {
		cfg.ASR.TurnGateActiveDeadlineMS = 200
	}
	normalizedInternalVoice, err := normalizeInternalVoiceConfig(cfg.InternalVoice)
	if err != nil {
		return Config{}, err
	}
	cfg.InternalVoice = normalizedInternalVoice
	return cfg, nil
}

func normalizeInternalVoiceConfig(cfg InternalVoiceConfig) (InternalVoiceConfig, error) {
	sessionMode, err := normalizeInternalVoiceMode(cfg.Mode)
	if err != nil {
		return InternalVoiceConfig{}, err
	}
	cfg.Mode = sessionMode
	mode, err := normalizeInternalVoiceClientEventMode(cfg.ClientEventMode)
	if err != nil {
		return InternalVoiceConfig{}, err
	}
	cfg.ClientEventMode = mode
	inputAudioMode, err := normalizeInternalVoiceInputAudioMode(cfg.InputAudioMode)
	if err != nil {
		return InternalVoiceConfig{}, err
	}
	cfg.InputAudioMode = inputAudioMode
	switch cfg.Mode {
	case "m1":
		cfg.Enabled = true
		cfg.ClientEventEnabled = true
		cfg.ClientEventMode = "active"
		cfg.InputAudioEnabled = true
		cfg.InputAudioMode = "active"
		cfg.InterruptEnabled = true
		cfg.PlaybackReportEnabled = true
	case "shadow":
		cfg.Enabled = true
		cfg.ClientEventEnabled = true
		cfg.ClientEventMode = "ack_only"
		cfg.InputAudioEnabled = true
		cfg.InputAudioMode = "shadow"
		cfg.InterruptEnabled = true
		cfg.PlaybackReportEnabled = true
	case "m0":
		cfg.Enabled = false
		cfg.ClientEventEnabled = false
		cfg.InputAudioEnabled = false
		cfg.InterruptEnabled = false
		cfg.PlaybackReportEnabled = false
	}
	cfg.WSURL = strings.TrimSpace(cfg.WSURL)
	if cfg.WSURL == "" {
		cfg.WSURL = defaultInternalVoiceWSURL
	}
	if _, err := parseInternalVoiceWSURL(cfg.WSURL); err != nil {
		return InternalVoiceConfig{}, err
	}
	if cfg.TimeoutMS <= 0 {
		cfg.TimeoutMS = defaultInternalVoiceTimeoutMS
	}
	cfg.Timeout = time.Duration(cfg.TimeoutMS) * time.Millisecond
	if cfg.ClientEventTimeoutMS <= 0 {
		cfg.ClientEventTimeoutMS = defaultInternalVoiceClientEventTimeoutMS
	}
	cfg.ClientEventTimeout = time.Duration(cfg.ClientEventTimeoutMS) * time.Millisecond
	if cfg.MaxMessageKB <= 0 {
		cfg.MaxMessageKB = defaultInternalVoiceMaxMessageKB
	}
	cfg.MaxMessageBytes = cfg.MaxMessageKB * 1024
	if cfg.HeartbeatIntervalMS < 0 {
		cfg.HeartbeatIntervalMS = defaultInternalVoiceHeartbeatIntervalMS
	}
	cfg.HeartbeatInterval = time.Duration(cfg.HeartbeatIntervalMS) * time.Millisecond
	if cfg.HeartbeatTimeoutMS <= 0 {
		cfg.HeartbeatTimeoutMS = defaultInternalVoiceHeartbeatTimeoutMS
	}
	cfg.HeartbeatTimeout = time.Duration(cfg.HeartbeatTimeoutMS) * time.Millisecond
	if cfg.HeartbeatMissLimit <= 0 {
		cfg.HeartbeatMissLimit = defaultInternalVoiceHeartbeatMissLimit
	}
	return cfg, nil
}

func normalizeInternalVoiceMode(raw string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return "", nil
	case "m1", "active":
		return "m1", nil
	case "m0", "legacy":
		return "m0", nil
	case "shadow", "probe":
		return "shadow", nil
	default:
		return "", fmt.Errorf("unsupported internal voice mode: %s", raw)
	}
}

func normalizeInternalVoiceInputAudioMode(raw string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", "shadow", "probe":
		return defaultInternalVoiceInputAudioMode, nil
	case "active":
		return "active", nil
	default:
		return "", fmt.Errorf("unsupported internal voice input_audio mode: %s", raw)
	}
}

func normalizeInternalVoiceClientEventMode(raw string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return defaultInternalVoiceClientEventMode, nil
	case "ack", "ack_only", "probe":
		return "ack_only", nil
	case "active":
		return "active", nil
	default:
		return "", fmt.Errorf("unsupported internal voice client_event mode: %s", raw)
	}
}

func parseICETransportPolicy(raw string) (webrtc.ICETransportPolicy, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", "all":
		return webrtc.ICETransportPolicyAll, nil
	case "relay", "turn":
		return webrtc.ICETransportPolicyRelay, nil
	default:
		return webrtc.ICETransportPolicyAll, fmt.Errorf("unsupported ICE transport policy: %s", raw)
	}
}

func parseICENetworkTypes(raw string) ([]webrtc.NetworkType, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	if strings.EqualFold(raw, "all") {
		return []webrtc.NetworkType{webrtc.NetworkTypeUDP4, webrtc.NetworkTypeUDP6}, nil
	}

	parts := strings.Split(raw, ",")
	networkTypes := make([]webrtc.NetworkType, 0, len(parts))
	seen := make(map[webrtc.NetworkType]bool, len(parts))
	for _, part := range parts {
		normalized := strings.ToLower(strings.TrimSpace(part))
		if normalized == "" {
			continue
		}
		switch normalized {
		case "ipv4", "v4":
			normalized = "udp4"
		case "ipv6", "v6":
			normalized = "udp6"
		}
		networkType, err := webrtc.NewNetworkType(normalized)
		if err != nil || networkType == webrtc.NetworkTypeUnknown {
			return nil, fmt.Errorf("unsupported ICE network type: %s", part)
		}
		if !seen[networkType] {
			networkTypes = append(networkTypes, networkType)
			seen[networkType] = true
		}
	}
	return networkTypes, nil
}

func iceNetworkTypeStrings(networkTypes []webrtc.NetworkType) []string {
	values := make([]string, 0, len(networkTypes))
	for _, networkType := range networkTypes {
		values = append(values, networkType.String())
	}
	return values
}

func parsePythonGatewayConnectionMode(raw string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", pythonGatewayConnectionModeSession:
		return pythonGatewayConnectionModeSession, nil
	case pythonGatewayConnectionModePerTurn, "per-turn", "per_turn_ws":
		return pythonGatewayConnectionModePerTurn, nil
	default:
		return "", fmt.Errorf("unsupported Python Gateway connection mode: %s", raw)
	}
}

func parseICEServers(raw string) ([]ICEServerConfig, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}

	if strings.HasPrefix(raw, "[") || strings.HasPrefix(raw, "{") {
		var value any
		if err := json.Unmarshal([]byte(raw), &value); err != nil {
			return nil, fmt.Errorf("parse ICE server JSON: %w", err)
		}
		return normalizeICEServerValue(value)
	}

	parts := strings.Split(raw, ",")
	servers := make([]ICEServerConfig, 0, len(parts))
	for _, part := range parts {
		url := strings.TrimSpace(part)
		if url != "" {
			servers = append(servers, ICEServerConfig{URLs: []string{url}})
		}
	}
	return servers, nil
}

func normalizeICEServerValue(value any) ([]ICEServerConfig, error) {
	switch typed := value.(type) {
	case []any:
		servers := make([]ICEServerConfig, 0, len(typed))
		for _, item := range typed {
			normalized, err := normalizeICEServerValue(item)
			if err != nil {
				return nil, err
			}
			servers = append(servers, normalized...)
		}
		return servers, nil
	case map[string]any:
		server, ok := normalizeICEServerObject(typed)
		if !ok {
			return nil, nil
		}
		return []ICEServerConfig{server}, nil
	case string:
		url := strings.TrimSpace(typed)
		if url == "" {
			return nil, nil
		}
		return []ICEServerConfig{{URLs: []string{url}}}, nil
	default:
		return nil, fmt.Errorf("unsupported ICE server value: %T", value)
	}
}

func normalizeICEServerObject(value map[string]any) (ICEServerConfig, bool) {
	urls := normalizeStringList(value["urls"])
	if len(urls) == 0 {
		return ICEServerConfig{}, false
	}
	return ICEServerConfig{
		URLs:       urls,
		Username:   stringValue(value["username"]),
		Credential: stringValue(value["credential"]),
	}, true
}

func normalizeStringList(value any) []string {
	switch typed := value.(type) {
	case string:
		trimmed := strings.TrimSpace(typed)
		if trimmed == "" {
			return nil
		}
		return []string{trimmed}
	case []any:
		values := make([]string, 0, len(typed))
		for _, item := range typed {
			if text := stringValue(item); text != "" {
				values = append(values, text)
			}
		}
		return values
	default:
		return nil
	}
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return strings.TrimSpace(typed)
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func (cfg Config) pionICEServers() []webrtc.ICEServer {
	servers := make([]webrtc.ICEServer, 0, len(cfg.ICEServers))
	for _, server := range cfg.ICEServers {
		pionServer := webrtc.ICEServer{URLs: append([]string(nil), server.URLs...)}
		if server.Username != "" {
			pionServer.Username = server.Username
		}
		if server.Credential != "" {
			pionServer.Credential = server.Credential
		}
		servers = append(servers, pionServer)
	}
	return servers
}

func newWebRTCAPI(cfg Config) (*webrtc.API, error) {
	settingEngine := webrtc.SettingEngine{}
	settingEngine.SetICEMulticastDNSMode(ice.MulticastDNSModeDisabled)
	settingEngine.SetIncludeLoopbackCandidate(true)
	settingEngine.SetFireOnTrackBeforeFirstRTP(true)
	if len(cfg.ICENetworkTypes) > 0 {
		settingEngine.SetNetworkTypes(cfg.ICENetworkTypes)
	}
	if err := settingEngine.SetEphemeralUDPPortRange(uint16(cfg.RTCUDPMinPort), uint16(cfg.RTCUDPMaxPort)); err != nil {
		return nil, fmt.Errorf("set WebRTC UDP port range: %w", err)
	}
	if cfg.LocalInterfaceName != "" {
		settingEngine.SetInterfaceFilter(func(interfaceName string) bool {
			return interfaceName == cfg.LocalInterfaceName
		})
	}
	if cfg.LocalIP != "" {
		wanted := net.ParseIP(cfg.LocalIP)
		if wanted == nil {
			return nil, fmt.Errorf("invalid local IP filter: %s", cfg.LocalIP)
		}
		settingEngine.SetIPFilter(func(ip net.IP) bool {
			return ip.Equal(wanted)
		})
	}
	return webrtc.NewAPI(webrtc.WithSettingEngine(settingEngine)), nil
}

func envFirstNonEmpty(fallback string, names ...string) string {
	for _, name := range names {
		if value := strings.TrimSpace(os.Getenv(name)); value != "" {
			return value
		}
	}
	return fallback
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if trimmed := strings.TrimSpace(value); trimmed != "" {
			return trimmed
		}
	}
	return ""
}

func envBool(name string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	switch strings.ToLower(value) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func envIntFirst(fallback int, names ...string) int {
	for _, name := range names {
		value := strings.TrimSpace(os.Getenv(name))
		if value == "" {
			continue
		}
		parsed, err := strconv.Atoi(value)
		if err != nil {
			return fallback
		}
		return parsed
	}
	return fallback
}

func csvEnv(name string) []string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return nil
	}
	return csvList(value)
}

func csvEnvDefault(fallback string, name string) []string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		value = fallback
	}
	return csvList(value)
}

func csvList(value string) []string {
	parts := strings.Split(value, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func normalizePath(path string) string {
	path = strings.TrimSpace(path)
	if path == "" {
		return defaultWebSocketPath
	}
	if !strings.HasPrefix(path, "/") {
		return "/" + path
	}
	return path
}

func maxInt(value, floor int) int {
	if value < floor {
		return floor
	}
	return value
}
