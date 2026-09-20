package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

func TestParseICEServersFromCSV(t *testing.T) {
	servers, err := parseICEServers("stun:one.example:3478, turn:two.example:3478?transport=udp")
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 2 {
		t.Fatalf("len(servers) = %d, want 2", len(servers))
	}
	if servers[0].URLs[0] != "stun:one.example:3478" {
		t.Fatalf("first URL = %q", servers[0].URLs[0])
	}
	if servers[1].URLs[0] != "turn:two.example:3478?transport=udp" {
		t.Fatalf("second URL = %q", servers[1].URLs[0])
	}
}

func TestParseICEServersFromJSONArray(t *testing.T) {
	raw := `[
		"stun:stun.example.com:3478",
		{
			"urls": [
				"turn:turn.example.com:3478?transport=udp",
				"turns:turn.example.com:5349?transport=tcp"
			],
			"username": "short_user",
			"credential": "short_secret"
		}
	]`

	servers, err := parseICEServers(raw)
	if err != nil {
		t.Fatal(err)
	}
	if len(servers) != 2 {
		t.Fatalf("len(servers) = %d, want 2", len(servers))
	}
	if got := len(servers[1].URLs); got != 2 {
		t.Fatalf("turn URL count = %d, want 2", got)
	}
	if servers[1].Username != "short_user" {
		t.Fatalf("username = %q", servers[1].Username)
	}
	if servers[1].Credential != "short_secret" {
		t.Fatalf("credential = %q", servers[1].Credential)
	}
}

func TestParseICETransportPolicy(t *testing.T) {
	if _, err := parseICETransportPolicy("all"); err != nil {
		t.Fatal(err)
	}
	if _, err := parseICETransportPolicy("relay"); err != nil {
		t.Fatal(err)
	}
	if _, err := parseICETransportPolicy("bad"); err == nil {
		t.Fatal("expected unsupported policy error")
	}
}

func TestParseICENetworkTypes(t *testing.T) {
	networkTypes, err := parseICENetworkTypes("udp4, udp6, ipv4")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(iceNetworkTypeStrings(networkTypes), ","); got != "udp4,udp6" {
		t.Fatalf("network types = %q, want udp4,udp6", got)
	}
	if _, err := parseICENetworkTypes("bad"); err == nil {
		t.Fatal("expected unsupported network type error")
	}
	allTypes, err := parseICENetworkTypes("all")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(iceNetworkTypeStrings(allTypes), ","); got != "udp4,udp6" {
		t.Fatalf("all network types = %q, want udp4,udp6", got)
	}
}

func TestLoadConfigFromEnvDefaultsToPublicSTUNOnly(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "")
	t.Setenv("GATEWAY_RTC_ICE_SERVERS", "")
	t.Setenv("WEBRTC_ICE_SERVERS", "")
	t.Setenv("GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY", "")
	t.Setenv("WEBRTC_ICE_TRANSPORT_POLICY", "")
	t.Setenv("GO_VOICE_GATEWAY_ICE_NETWORK_TYPES", "")
	t.Setenv("GATEWAY_RTC_ICE_NETWORK_TYPES", "")
	t.Setenv("WEBRTC_ICE_NETWORK_TYPES", "")
	t.Setenv("GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT", "")
	t.Setenv("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT", "")
	t.Setenv("GO_VOICE_GATEWAY_LOG_FORMAT", "")
	t.Setenv("GO_VOICE_GATEWAY_LOG_LEVEL", "")
	t.Setenv("VOICE_LOG_FORMAT", "")
	t.Setenv("VOICE_LOG_LEVEL", "")
	t.Setenv("LOG_FORMAT", "")
	t.Setenv("LOG_LEVEL", "")
	t.Setenv("GO_VOICE_GATEWAY_LOG_MAX_BYTES", "")
	t.Setenv("VOICE_LOG_MAX_BYTES", "")
	t.Setenv("LOG_MAX_BYTES", "")
	t.Setenv("GO_VOICE_GATEWAY_LOG_BACKUP_COUNT", "")
	t.Setenv("VOICE_LOG_BACKUP_COUNT", "")
	t.Setenv("LOG_BACKUP_COUNT", "")
	t.Setenv("GO_VOICE_GATEWAY_ALLOWED_ORIGINS", "")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_ENABLED", "")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE", "")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_ENABLED", "")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", "")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.ICETransportPolicy.String(); got != "all" {
		t.Fatalf("ice policy = %q, want all", got)
	}
	if got := strings.Join(iceNetworkTypeStrings(cfg.ICENetworkTypes), ","); got != "udp4" {
		t.Fatalf("ice networks = %q, want udp4", got)
	}
	if cfg.RTCUDPMinPort != defaultRTCUDPMinPort || cfg.RTCUDPMaxPort != defaultRTCUDPMaxPort {
		t.Fatalf("rtc udp port range = %d-%d, want %d-%d", cfg.RTCUDPMinPort, cfg.RTCUDPMaxPort, defaultRTCUDPMinPort, defaultRTCUDPMaxPort)
	}

	var hasSTUN, hasTURN bool
	for _, server := range cfg.ICEServers {
		if server.Username != "" || server.Credential != "" {
			t.Fatal("default ICE config must not contain credentials")
		}
		for _, url := range server.URLs {
			if url == "stun:frp.wzk.icu:3478" {
				hasSTUN = true
			}
			if strings.HasPrefix(url, "turn:") || strings.HasPrefix(url, "turns:") {
				hasTURN = true
			}
		}
	}
	if !hasSTUN || hasTURN {
		t.Fatalf("default ICE servers must contain public STUN only: %+v", cfg.ICEServers)
	}
	if cfg.LogFormat != "text" || cfg.LogLevel != "info" {
		t.Fatalf("log defaults = %s/%s, want text/info", cfg.LogFormat, cfg.LogLevel)
	}
	if cfg.LogMaxBytes != defaultLogMaxBytes || cfg.LogBackupCount != defaultLogBackupCount {
		t.Fatalf("log rotation defaults = %d/%d", cfg.LogMaxBytes, cfg.LogBackupCount)
	}
	if got := strings.Join(cfg.AllowedOrigins, ","); got != "*" {
		t.Fatalf("allowed origins = %q, want *", got)
	}
	if !cfg.InternalVoice.Enabled ||
		cfg.InternalVoice.Mode != "m1" ||
		!cfg.InternalVoice.ClientEventEnabled ||
		cfg.InternalVoice.ClientEventMode != "active" ||
		!cfg.InternalVoice.InputAudioEnabled ||
		cfg.InternalVoice.InputAudioMode != "active" ||
		cfg.InternalVoice.WSURL != defaultInternalVoiceWSURL {
		t.Fatalf("internal voice defaults = %+v, want M1 to own the production path", cfg.InternalVoice)
	}
}

func TestLoadConfigRejectsInvalidRTCUDPPortRange(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT", "35600")
	t.Setenv("GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT", "35500")

	if _, err := LoadConfigFromEnv(); err == nil {
		t.Fatal("expected invalid UDP port range error")
	}
}

func TestLoadConfigUsesSharedLogEnvAliases(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_LOG_FORMAT", "")
	t.Setenv("GO_VOICE_GATEWAY_LOG_LEVEL", "")
	t.Setenv("VOICE_LOG_FORMAT", "json")
	t.Setenv("VOICE_LOG_LEVEL", "debug")
	t.Setenv("VOICE_LOG_MAX_BYTES", "2048")
	t.Setenv("VOICE_LOG_BACKUP_COUNT", "2")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.LogFormat != "json" || cfg.LogLevel != "debug" {
		t.Fatalf("log config = %s/%s, want json/debug", cfg.LogFormat, cfg.LogLevel)
	}
	if cfg.LogMaxBytes != 2048 || cfg.LogBackupCount != 2 {
		t.Fatalf("rotation config = %d/%d, want 2048/2", cfg.LogMaxBytes, cfg.LogBackupCount)
	}
}

func TestPrintableConfigRedactsSecrets(t *testing.T) {
	cfg := Config{
		Addr:               "127.0.0.1:8282",
		WebSocketPath:      "/ws",
		BotID:              "xiaowen",
		LogFormat:          "json",
		LogLevel:           "debug",
		RTCEnabled:         true,
		ICETransportPolicy: webrtc.ICETransportPolicyAll,
		ICENetworkTypes:    []webrtc.NetworkType{webrtc.NetworkTypeUDP4},
		RTCUDPMinPort:      defaultRTCUDPMinPort,
		RTCUDPMaxPort:      defaultRTCUDPMaxPort,
		ICEServers: []ICEServerConfig{
			{
				URLs:       []string{"turn:frp.wzk.icu:3478?transport=udp"},
				Username:   "wzkicu",
				Credential: "turn-secret",
			},
		},
		ASR: ASRConfig{
			Processor:                asrProcessorPythonGateway,
			PythonGatewayWSURL:       defaultPythonGatewayWSURL,
			PythonGatewayRobotSecret: "robot-secret",
		},
		VisionInternalToken: "vision-secret",
		NegotiationTimeout:  5 * time.Second,
		WriteTimeout:        3 * time.Second,
	}

	printable := cfg.Printable()
	payload, err := json.Marshal(printable)
	if err != nil {
		t.Fatal(err)
	}
	text := string(payload)
	if strings.Contains(text, "turn-secret") ||
		strings.Contains(text, "robot-secret") ||
		strings.Contains(text, "vision-secret") {
		t.Fatalf("printable config leaked secret: %s", text)
	}
	if !printable.VisionInternalAuth {
		t.Fatal("printable config should report configured vision internal auth")
	}
	if !strings.Contains(text, `"credential":"\u003cset\u003e"`) ||
		!strings.Contains(text, `"python_gateway_robot_secret":"\u003cset\u003e"`) {
		t.Fatalf("printable config did not mark configured secrets: %s", text)
	}
	if !strings.Contains(text, `"rtc_udp_min_port":35500`) ||
		!strings.Contains(text, `"rtc_udp_max_port":35600`) {
		t.Fatalf("printable config did not include UDP port range: %s", text)
	}
}

func TestPrintEnvHelpIncludesConfigFieldMappings(t *testing.T) {
	var buf bytes.Buffer
	if err := printEnvHelp(&buf, "Go Voice Gateway environment variables", goGatewayEnvHelpEntries()); err != nil {
		t.Fatal(err)
	}
	text := buf.String()

	for _, expected := range []string{
		"addr",
		"GO_VOICE_GATEWAY_ADDR",
		"asr.python_gateway_ws_url",
		"GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL",
		"asr.python_gateway_total_timeout_ms",
		"GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS",
		"asr.python_gateway_session_idle_recycle_ms",
		"GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS",
		"rtc_udp_min_port",
		"GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT",
		"turns:turn.example.com:443?transport=tcp",
		"export GO_VOICE_GATEWAY_ADDR=0.0.0.0:8282",
	} {
		if !strings.Contains(text, expected) {
			t.Fatalf("env help missing %q:\n%s", expected, text)
		}
	}
}

func TestConnectionInfoPrefersForwardedIPAndKeepsRemoteAddr(t *testing.T) {
	req, err := http.NewRequest("GET", "http://voice-gateway.test/ws", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.RemoteAddr = "172.18.0.2:51234"
	req.Header.Set("X-Forwarded-For", "203.0.113.10, 10.0.0.8")
	req.Header.Set("X-Real-IP", "198.51.100.7")
	req.Header.Set("User-Agent", "rust-client-test")
	req.Header.Set("Origin", "http://voice-gateway.test")

	info := connectionInfoFromRequest("172.18.0.3:60000", req)

	if info.SourceIP != "203.0.113.10" {
		t.Fatalf("source ip = %q, want forwarded client ip", info.SourceIP)
	}
	if info.RemoteAddr != "172.18.0.3:60000" || info.RemoteIP != "172.18.0.3" {
		t.Fatalf("remote = %q/%q", info.RemoteAddr, info.RemoteIP)
	}
	if info.XRealIP != "198.51.100.7" || info.UserAgent != "rust-client-test" || info.Origin == "" {
		t.Fatalf("unexpected request metadata: %+v", info)
	}
}

func TestStructuredLoggerTextAndJSON(t *testing.T) {
	var text bytes.Buffer
	textLogger, err := newStructuredLogger(&text, "text", "debug")
	if err != nil {
		t.Fatal(err)
	}
	textLogger.Info("ws_connected", "session", "rtc_test", "source_ip", "203.0.113.10")
	textOutput := text.String()
	if !strings.Contains(textOutput, "msg=ws_connected") ||
		!strings.Contains(textOutput, "service=go_voice_gateway") ||
		!strings.Contains(textOutput, "source_ip=203.0.113.10") {
		t.Fatalf("unexpected text log output: %s", textOutput)
	}

	var jsonBuf bytes.Buffer
	jsonLogger, err := newStructuredLogger(&jsonBuf, "json", "info")
	if err != nil {
		t.Fatal(err)
	}
	jsonLogger.Info("client_registered", "session", "rtc_test")
	var decoded map[string]any
	if err := json.Unmarshal(jsonBuf.Bytes(), &decoded); err != nil {
		t.Fatalf("json log is invalid: %v output=%s", err, jsonBuf.String())
	}
	if decoded["msg"] != "client_registered" || decoded["service"] != "go_voice_gateway" || decoded["session"] != "rtc_test" {
		t.Fatalf("unexpected json log: %+v", decoded)
	}
}

func TestRotatingFileWriterRotatesBySize(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "gateway.log")
	writer, err := newRotatingFileWriter(path, 12, 2)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("first-line\n")); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("second-line\n")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}

	current, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	rotated, err := os.ReadFile(path + ".1")
	if err != nil {
		t.Fatal(err)
	}
	if string(current) != "second-line\n" || string(rotated) != "first-line\n" {
		t.Fatalf("rotation mismatch current=%q rotated=%q", current, rotated)
	}
}

func TestGatewaySessionObservesRTPStatsAndCachesRecentPackets(t *testing.T) {
	session := &gatewaySession{id: "session_test"}
	key := rtpTrackKey{kind: "audio", id: "microphone", streamID: "voice"}
	firstPayload := []byte{0x01, 0x02, 0x03}

	for i := 0; i < maxCachedRTPPackets+1; i++ {
		payload := []byte{byte(i), 0xf8, 0xff}
		sequenceNumber := uint16(i + 1)
		if i == 8 {
			sequenceNumber = 11
		}
		if i == 0 {
			payload = firstPayload
		}
		session.observeRTPPacket(key, &rtp.Packet{
			Header: rtp.Header{
				Version:        2,
				Marker:         i%2 == 0,
				PayloadType:    111,
				SequenceNumber: sequenceNumber,
				Timestamp:      uint32(i * 960),
			},
			Payload: payload,
		})
		if i == 0 {
			firstPayload[0] = 0xff
		}
	}

	snapshot, ok := session.rtpTrackSnapshot(key)
	if !ok {
		t.Fatal("missing RTP stats")
	}
	if snapshot.packetCount != maxCachedRTPPackets+1 {
		t.Fatalf("packetCount = %d, want %d", snapshot.packetCount, maxCachedRTPPackets+1)
	}
	if snapshot.payloadBytes != uint64((maxCachedRTPPackets+1)*3) {
		t.Fatalf("payloadBytes = %d", snapshot.payloadBytes)
	}
	if snapshot.firstSequenceNumber != 1 {
		t.Fatalf("first sequence = %d, want 1", snapshot.firstSequenceNumber)
	}
	if snapshot.lastSequenceNumber != maxCachedRTPPackets+1 {
		t.Fatalf("last sequence = %d, want %d", snapshot.lastSequenceNumber, maxCachedRTPPackets+1)
	}
	if snapshot.cachedPackets != maxCachedRTPPackets {
		t.Fatalf("cached packets = %d, want %d", snapshot.cachedPackets, maxCachedRTPPackets)
	}
	if snapshot.sequenceGaps != 2 {
		t.Fatalf("sequence gaps = %d, want 2", snapshot.sequenceGaps)
	}
	if snapshot.timestampRegressions != 0 {
		t.Fatalf("timestamp regressions = %d, want 0", snapshot.timestampRegressions)
	}

	cached := session.cachedRTPPackets(key)
	if len(cached) != maxCachedRTPPackets {
		t.Fatalf("len(cached) = %d, want %d", len(cached), maxCachedRTPPackets)
	}
	if cached[0].sequenceNumber != 2 {
		t.Fatalf("first cached sequence = %d, want 2", cached[0].sequenceNumber)
	}
	if cached[0].payload[0] != 1 {
		t.Fatalf("cached payload was mutated: %v", cached[0].payload)
	}

	events := session.recentCanonicalEvents()
	if len(events) != maxCachedRTPPackets+1 {
		t.Fatalf("canonical event count = %d, want %d", len(events), maxCachedRTPPackets+1)
	}
	last := events[len(events)-1]
	if last.Type != canonicalEventAudioPacket || last.Transport != canonicalTransportWebRTCRTP {
		t.Fatalf("last canonical RTP event = %+v", last)
	}
	if last.TrackID != "microphone" || last.StreamID != "voice" {
		t.Fatalf("last canonical RTP track fields = %+v", last)
	}
	if last.PacketCount != maxCachedRTPPackets+1 || last.CachedPackets != maxCachedRTPPackets {
		t.Fatalf("last canonical RTP counters = %+v", last)
	}
}

func TestGatewaySessionRecordsCanonicalControlEvents(t *testing.T) {
	session := &gatewaySession{
		id:     "session_test",
		server: &Server{logger: log.New(io.Discard, "", 0)},
	}

	if err := session.handleClientMessage(context.Background(), clientMessage{
		Type:        "audio_start",
		BotID:       "bot_1",
		UtteranceID: "utt_1",
	}); err != nil {
		t.Fatal(err)
	}
	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:   "interrupt",
		Reason: "user_interrupt",
	}); err != nil {
		t.Fatal(err)
	}

	events := session.recentCanonicalEvents()
	if len(events) != 2 {
		t.Fatalf("canonical event count = %d, want 2", len(events))
	}
	if events[0].Type != "audio_start" ||
		events[0].Transport != canonicalTransportWebSocket ||
		events[0].BotID != "bot_1" ||
		events[0].UtteranceID != "utt_1" {
		t.Fatalf("unexpected websocket canonical event: %+v", events[0])
	}
	if events[1].Type != "interrupt" ||
		events[1].Transport != canonicalTransportWebRTCControl ||
		events[1].Reason != "user_interrupt" {
		t.Fatalf("unexpected datachannel canonical event: %+v", events[1])
	}
	if events[0].Sequence != 1 || events[1].Sequence != 2 {
		t.Fatalf("unexpected event sequences: %+v", events)
	}
}

func TestServerDispatchesCanonicalEventsToSink(t *testing.T) {
	var mu sync.Mutex
	var sinkEvents []CanonicalVoiceEvent
	server, err := NewServerWithVoiceEventSink(
		testConfig(),
		log.New(io.Discard, "", 0),
		VoiceEventSinkFunc(func(event CanonicalVoiceEvent) {
			mu.Lock()
			defer mu.Unlock()
			sinkEvents = append(sinkEvents, event)
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	session := &gatewaySession{
		id:     "session_test",
		server: server,
	}

	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:        "audio_start",
		BotID:       "bot_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	}); err != nil {
		t.Fatal(err)
	}
	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:           "turn_candidate",
		BotID:          "bot_1",
		TraceID:        "trace_1",
		UtteranceID:    "utt_1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		SilenceMS:      300,
		Shadow:         true,
	}); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if len(sinkEvents) != 2 {
		t.Fatalf("sink event count = %d, want 2", len(sinkEvents))
	}
	event := sinkEvents[0]
	if event.Sequence != 1 ||
		event.SessionID != "session_test" ||
		event.Type != "audio_start" ||
		event.Transport != canonicalTransportWebRTCControl ||
		event.BotID != "bot_1" ||
		event.TraceID != "trace_1" ||
		event.UtteranceID != "utt_1" {
		t.Fatalf("unexpected sink event: %+v", event)
	}
	candidate := sinkEvents[1]
	if candidate.Sequence != 2 ||
		candidate.Type != "turn_candidate" ||
		candidate.UtteranceID != "utt_1" ||
		candidate.CandidateSeq != 1 ||
		candidate.AudioWatermark != 16_000 ||
		candidate.SilenceMS != 300 ||
		!candidate.Shadow {
		t.Fatalf("unexpected candidate sink event: %+v", candidate)
	}
}

func TestGatewaySessionForwardsClientControlEvents(t *testing.T) {
	processor := &recordingClientEventProcessor{controls: make(chan clientMessage, 2)}
	session := &gatewaySession{
		id: "session_test",
		server: &Server{
			logger:       log.New(io.Discard, "", 0),
			clientEvents: processor,
		},
	}

	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:        "audio_cancel",
		UtteranceID: "utt_1",
		Reason:      "wake_interrupt",
	}); err != nil {
		t.Fatal(err)
	}
	if err := session.handleClientMessage(context.Background(), clientMessage{
		Type:   "interrupt",
		Reason: "response_timeout",
	}); err != nil {
		t.Fatal(err)
	}

	controls := map[string]clientMessage{}
	for i := 0; i < 2; i++ {
		message := waitForClientMessage(t, processor.controls, "control")
		controls[message.Type] = message
	}
	audioCancel := controls["audio_cancel"]
	if audioCancel.Type != "audio_cancel" ||
		audioCancel.UtteranceID != "utt_1" ||
		audioCancel.Reason != "wake_interrupt" {
		t.Fatalf("unexpected audio_cancel control: %+v", audioCancel)
	}
	interrupt := controls["interrupt"]
	if interrupt.Type != "interrupt" || interrupt.Reason != "response_timeout" {
		t.Fatalf("unexpected interrupt control: %+v", interrupt)
	}
}

func TestWakeInterruptClientEventAlsoInterruptsPrimaryGateway(t *testing.T) {
	processor := &recordingClientEventProcessor{
		messages: make(chan clientMessage, 1),
		controls: make(chan clientMessage, 1),
	}
	session := &gatewaySession{
		id: "session_test",
		server: &Server{
			logger:       log.New(io.Discard, "", 0),
			clientEvents: processor,
		},
	}

	session.dispatchClientEvent(clientMessage{
		Type:        "client_event",
		SessionID:   "session_test",
		Event:       "wake_interrupt",
		EventID:     "wake_interrupt-1",
		UtteranceID: "utterance-2",
		TraceID:     "trace-2",
	}, canonicalTransportWebRTCControl)

	control := waitForClientMessage(t, processor.controls, "wake interrupt control")
	if control.Type != "interrupt" || control.Reason != "wake_interrupt" ||
		control.SessionID != "session_test" || control.UtteranceID != "utterance-2" ||
		control.TraceID != "trace-2" {
		t.Fatalf("unexpected wake interrupt control: %+v", control)
	}
	event := waitForClientMessage(t, processor.messages, "wake client event")
	if event.Type != "client_event" || event.Event != "wake_interrupt" {
		t.Fatalf("unexpected wake client event: %+v", event)
	}

	session.dispatchClientEvent(clientMessage{
		Type:    "client_event",
		Event:   "listening_started",
		EventID: "listening-1",
	}, canonicalTransportWebRTCControl)
	ordinaryEvent := waitForClientMessage(t, processor.messages, "ordinary client event")
	if ordinaryEvent.Event != "listening_started" {
		t.Fatalf("unexpected ordinary client event: %+v", ordinaryEvent)
	}
	select {
	case unexpected := <-processor.controls:
		t.Fatalf("ordinary client event forwarded an interrupt: %+v", unexpected)
	case <-time.After(100 * time.Millisecond):
	}
}

func TestWakeInterruptBlocksOldRTPAndLatePlaybackReportCannotCancelReplacement(t *testing.T) {
	session := &gatewaySession{
		id: "session_test",
		server: &Server{
			logger: log.New(io.Discard, "", 0),
		},
		recentClientEvents: make(map[string]time.Time),
	}
	session.startDownlinkPlayback(
		"trace_old",
		"round_old",
		"round_old:playback",
		"test_old_playback",
	)

	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:    "client_event",
		Event:   "wake_interrupt",
		EventID: "wake_interrupt-1",
		Source:  "hardware_wake",
	}); err != nil {
		t.Fatal(err)
	}
	if !session.downBlocked.Load() {
		t.Fatal("wake_interrupt must block the old RTP downlink immediately")
	}

	session.startDownlinkPlayback(
		"trace_new",
		"round_new",
		"round_new:playback",
		"test_replacement_playback",
	)
	if session.downBlocked.Load() {
		t.Fatal("replacement playback_start must unblock RTP downlink")
	}

	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:       "playback_interrupted",
		TraceID:    "trace_old",
		RoundID:    "round_old",
		PlaybackID: "round_old:playback",
		Reason:     "wake_interrupt",
	}); err != nil {
		t.Fatal(err)
	}
	if session.downBlocked.Load() {
		t.Fatal("late old playback_interrupted must not cancel replacement playback")
	}
	session.downMu.Lock()
	roundID := session.downRoundID
	playbackID := session.downPlayID
	session.downMu.Unlock()
	if roundID != "round_new" || playbackID != "round_new:playback" {
		t.Fatalf(
			"replacement playback state changed: round=%s playback=%s",
			roundID,
			playbackID,
		)
	}
}

func TestShouldFallbackInternalVoiceClientEventActiveOnlyBeforeCommit(t *testing.T) {
	if !shouldFallbackInternalVoiceClientEventActive(pythonGatewayBridgeSummary{
		RequestCommitted: false,
	}) {
		t.Fatal("expected active client_event fallback before commit")
	}
	if shouldFallbackInternalVoiceClientEventActive(pythonGatewayBridgeSummary{
		RequestCommitted: true,
		Accepted:         false,
	}) {
		t.Fatal("did not expect active client_event fallback after commit attempt")
	}
}

func TestGatewaySessionCloseClosesClientSessionBridge(t *testing.T) {
	processor := &recordingClientEventProcessor{closedSessions: make(chan string, 1)}
	session := &gatewaySession{
		id: "session_test",
		server: &Server{
			logger:       log.New(io.Discard, "", 0),
			clientEvents: processor,
		},
	}

	session.close()

	select {
	case sessionID := <-processor.closedSessions:
		if sessionID != "session_test" {
			t.Fatalf("closed session id = %q, want session_test", sessionID)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for closed client session")
	}
}

func TestRecordCanonicalEventDispatchesPayloadWithoutCachingIt(t *testing.T) {
	var mu sync.Mutex
	var sinkPayloadBytes int
	var sinkFirstByte byte
	server, err := NewServerWithVoiceEventSink(
		testConfig(),
		log.New(io.Discard, "", 0),
		VoiceEventSinkFunc(func(event CanonicalVoiceEvent) {
			mu.Lock()
			defer mu.Unlock()
			sinkPayloadBytes = len(event.rtpPayload)
			if len(event.rtpPayload) > 0 {
				sinkFirstByte = event.rtpPayload[0]
			}
		}),
	)
	if err != nil {
		t.Fatal(err)
	}
	session := &gatewaySession{
		id:     "session_test",
		server: server,
	}

	session.recordCanonicalEvent(CanonicalVoiceEvent{
		Type:       canonicalEventAudioPacket,
		Transport:  canonicalTransportWebRTCRTP,
		rtpPayload: []byte{7, 8, 9},
	})

	events := session.recentCanonicalEvents()
	if len(events) != 1 {
		t.Fatalf("canonical event count = %d, want 1", len(events))
	}
	if len(events[0].rtpPayload) != 0 {
		t.Fatalf("cached event retained RTP payload: %d bytes", len(events[0].rtpPayload))
	}
	mu.Lock()
	defer mu.Unlock()
	if sinkPayloadBytes != 3 || sinkFirstByte != 7 {
		t.Fatalf("sink payload bytes = %d first_byte = %d, want 3 and 7", sinkPayloadBytes, sinkFirstByte)
	}
}

func TestLoadConfigFromEnvReadsASRHandoffDryRun(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY", "all")
	t.Setenv("GO_VOICE_GATEWAY_ASR_HANDOFF_DRY_RUN", "true")
	t.Setenv("GO_VOICE_GATEWAY_ASR_HANDOFF_QUEUE_SIZE", "7")
	t.Setenv("GO_VOICE_GATEWAY_ASR_HANDOFF_END_GRACE_MS", "123")
	t.Setenv("GO_VOICE_GATEWAY_BARGE_IN_ENABLED", "")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.ASR.HandoffDryRunEnabled ||
		cfg.ASR.HandoffQueueSize != 7 ||
		cfg.ASR.HandoffEndGraceMS != 123 {
		t.Fatalf("unexpected ASR config: %+v", cfg.ASR)
	}
	if cfg.ASR.Processor != asrProcessorDryRun {
		t.Fatalf("ASR processor = %q, want %q", cfg.ASR.Processor, asrProcessorDryRun)
	}
	if cfg.ASR.BargeInEnabled {
		t.Fatal("barge-in must not default on for the dry-run processor")
	}
}

func TestLoadConfigFromEnvReadsASRPythonGatewayBridge(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PROCESSOR", asrProcessorPythonGateway)
	t.Setenv("GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT", downlinkAudioTransportWebRTCMirror)
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL", "ws://127.0.0.1:18000/ws")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID", "robot_1")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET", "secret_1")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CLIENT_TYPE", "go_test")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_BOT_ID", "xiaowen")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TIMEOUT_MS", "1234")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS", "5678")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_MAX_MESSAGE_KB", "256")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE", pythonGatewayConnectionModePerTurn)
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS", "4321")
	t.Setenv("GO_VOICE_GATEWAY_BARGE_IN_ENABLED", "")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ASR.Processor != asrProcessorPythonGateway ||
		cfg.ASR.PythonGatewayWSURL != "ws://127.0.0.1:18000/ws" ||
		cfg.ASR.PythonGatewayRobotID != "robot_1" ||
		cfg.ASR.PythonGatewayRobotSecret != "secret_1" ||
		cfg.ASR.PythonGatewayClientType != "go_test" ||
		cfg.ASR.PythonGatewayBotID != "xiaowen" ||
		cfg.ASR.PythonGatewayTimeoutMS != 1234 ||
		cfg.ASR.PythonGatewayTotalTimeoutMS != 5678 ||
		cfg.ASR.PythonGatewayMaxMessageKB != 256 ||
		cfg.ASR.PythonGatewayConnectionMode != pythonGatewayConnectionModePerTurn ||
		cfg.ASR.PythonGatewaySessionIdleRecycleMS != 4321 ||
		cfg.Audio.DownlinkTransport != downlinkAudioTransportWebRTCMirror {
		t.Fatalf("unexpected ASR Python Gateway config: %+v", cfg.ASR)
	}
	if !cfg.ASR.BargeInEnabled {
		t.Fatal("barge-in must default on for the Python Gateway processor")
	}
}

func TestLoadConfigFromEnvAllowsExplicitBargeInRollback(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ASR_PROCESSOR", asrProcessorPythonGateway)
	t.Setenv("GO_VOICE_GATEWAY_BARGE_IN_ENABLED", "false")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ASR.BargeInEnabled {
		t.Fatal("explicit false must disable natural barge-in")
	}
}

func TestLoadConfigFromEnvRejectsTurnGateActiveWithoutPythonGateway(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PROCESSOR", asrProcessorDryRun)
	t.Setenv("GO_VOICE_GATEWAY_TURN_GATE_ACTIVE_ENABLED", "true")

	_, err := LoadConfigFromEnv()
	if err == nil || !strings.Contains(err.Error(), asrProcessorPythonGateway) {
		t.Fatalf("expected active-mode processor validation error, got %v", err)
	}
}

func TestLoadConfigFromEnvRejectsBargeInWithoutPythonGateway(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ASR_PROCESSOR", asrProcessorDryRun)
	t.Setenv("GO_VOICE_GATEWAY_BARGE_IN_ENABLED", "true")
	if _, err := LoadConfigFromEnv(); err == nil || !strings.Contains(err.Error(), "natural barge-in requires") {
		t.Fatalf("expected barge-in processor validation error, got %v", err)
	}
}

func TestLoadConfigFromEnvDoesNotInheritOtherProcessTurnGateFlags(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("TURN_GATE_SHADOW_ENABLED", "true")
	t.Setenv("TURN_GATE_ACTIVE_ENABLED", "true")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ASR.TurnGateShadowEnabled || cfg.ASR.TurnGateActiveEnabled {
		t.Fatalf("Go Gateway inherited another process's Turn Gate flags: %+v", cfg.ASR)
	}
}

func TestLoadConfigFromEnvReadsInternalVoiceProtocol(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_MODE", "m1")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_ENABLED", "true")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_ENABLED", "true")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_MODE", "active")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_CLIENT_EVENT_TIMEOUT_MS", "12345")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_ENABLED", "true")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_INPUT_AUDIO_MODE", "active")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_INTERRUPT_ENABLED", "true")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_PLAYBACK_REPORT_ENABLED", "true")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_WS_URL", "ws://127.0.0.1:7860/internal/voice/ws")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_TIMEOUT_MS", "2345")
	t.Setenv("GO_VOICE_GATEWAY_INTERNAL_VOICE_MAX_MESSAGE_KB", "128")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.InternalVoice.Enabled ||
		cfg.InternalVoice.Mode != "m1" ||
		!cfg.InternalVoice.ClientEventEnabled ||
		cfg.InternalVoice.ClientEventMode != "active" ||
		cfg.InternalVoice.ClientEventTimeoutMS != 12345 ||
		cfg.InternalVoice.ClientEventTimeout != 12345*time.Millisecond ||
		!cfg.InternalVoice.InputAudioEnabled ||
		cfg.InternalVoice.InputAudioMode != "active" ||
		!cfg.InternalVoice.InterruptEnabled ||
		!cfg.InternalVoice.PlaybackReportEnabled ||
		cfg.InternalVoice.WSURL != "ws://127.0.0.1:7860/internal/voice/ws" ||
		cfg.InternalVoice.TimeoutMS != 2345 ||
		cfg.InternalVoice.Timeout != 2345*time.Millisecond ||
		cfg.InternalVoice.MaxMessageKB != 128 ||
		cfg.InternalVoice.MaxMessageBytes != 128*1024 {
		t.Fatalf("unexpected internal voice config: %+v", cfg.InternalVoice)
	}

	printable := cfg.Printable()
	if !printable.InternalVoice.Enabled ||
		printable.InternalVoice.Mode != "m1" ||
		!printable.InternalVoice.ClientEventEnabled ||
		printable.InternalVoice.ClientEventMode != "active" ||
		printable.InternalVoice.ClientEventTimeoutMS != 12345 ||
		!printable.InternalVoice.InputAudioEnabled ||
		printable.InternalVoice.InputAudioMode != "active" ||
		!printable.InternalVoice.InterruptEnabled ||
		!printable.InternalVoice.PlaybackReportEnabled ||
		printable.InternalVoice.WSURL != cfg.InternalVoice.WSURL ||
		printable.InternalVoice.TimeoutMS != 2345 ||
		printable.InternalVoice.MaxMessageKB != 128 {
		t.Fatalf("unexpected printable internal voice config: %+v", printable.InternalVoice)
	}
}

func TestNormalizeInternalVoiceModeOwnsMigrationFlags(t *testing.T) {
	tests := []struct {
		name              string
		mode              string
		enabled           bool
		clientEventMode   string
		inputAudioEnabled bool
		inputAudioMode    string
	}{
		{name: "m1", mode: "m1", enabled: true, clientEventMode: "active", inputAudioEnabled: true, inputAudioMode: "active"},
		{name: "shadow", mode: "shadow", enabled: true, clientEventMode: "ack_only", inputAudioEnabled: true, inputAudioMode: "shadow"},
		{name: "m0", mode: "m0", enabled: false, clientEventMode: "active", inputAudioEnabled: false, inputAudioMode: "shadow"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			cfg, err := normalizeInternalVoiceConfig(InternalVoiceConfig{
				Mode:               test.mode,
				ClientEventMode:    "active",
				InputAudioMode:     "shadow",
				WSURL:              defaultInternalVoiceWSURL,
				InterruptEnabled:   true,
				InputAudioEnabled:  true,
				ClientEventEnabled: true,
			})
			if err != nil {
				t.Fatal(err)
			}
			if cfg.Enabled != test.enabled ||
				cfg.ClientEventMode != test.clientEventMode ||
				cfg.InputAudioEnabled != test.inputAudioEnabled ||
				cfg.InputAudioMode != test.inputAudioMode {
				t.Fatalf("mode %s normalized to unexpected config: %+v", test.mode, cfg)
			}
			if test.mode == "m0" && (cfg.ClientEventEnabled || cfg.InterruptEnabled || cfg.PlaybackReportEnabled) {
				t.Fatalf("M0 rollback left M1 features enabled: %+v", cfg)
			}
		})
	}
}

func TestNormalizeInternalVoiceModeRejectsUnknownValue(t *testing.T) {
	_, err := normalizeInternalVoiceConfig(InternalVoiceConfig{
		Mode:  "hybrid",
		WSURL: defaultInternalVoiceWSURL,
	})
	if err == nil {
		t.Fatal("expected unsupported internal voice mode error")
	}
}

func TestLoadConfigFromEnvDefaultsPythonGatewayConnectionModeToSession(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ASR.Processor != asrProcessorPythonGateway {
		t.Fatalf("ASR processor = %q, want %q", cfg.ASR.Processor, asrProcessorPythonGateway)
	}
	if cfg.ASR.PythonGatewayConnectionMode != pythonGatewayConnectionModeSession {
		t.Fatalf("Python Gateway connection mode = %q, want %q", cfg.ASR.PythonGatewayConnectionMode, pythonGatewayConnectionModeSession)
	}
	if cfg.ASR.PythonGatewayTotalTimeoutMS != defaultPythonGatewayTotalTimeoutMS {
		t.Fatalf("Python Gateway total timeout = %d, want %d", cfg.ASR.PythonGatewayTotalTimeoutMS, defaultPythonGatewayTotalTimeoutMS)
	}
	if cfg.ASR.PythonGatewaySessionIdleRecycleMS != defaultPythonGatewaySessionIdleRecycleMS {
		t.Fatalf("Python Gateway session idle recycle = %d, want %d", cfg.ASR.PythonGatewaySessionIdleRecycleMS, defaultPythonGatewaySessionIdleRecycleMS)
	}
}

func TestLoadConfigFromEnvAllowsDisablingPythonGatewayTotalTimeout(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TOTAL_TIMEOUT_MS", "0")

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.ASR.PythonGatewayTotalTimeoutMS != 0 {
		t.Fatalf("Python Gateway total timeout = %d, want disabled 0", cfg.ASR.PythonGatewayTotalTimeoutMS)
	}
}

func TestLoadConfigFromEnvRejectsInvalidPythonGatewayConnectionMode(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE", "sticky")

	_, err := LoadConfigFromEnv()
	if err == nil {
		t.Fatal("expected invalid Python Gateway connection mode error")
	}
	if !strings.Contains(err.Error(), "unsupported Python Gateway connection mode") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestLoadConfigFromEnvReadsDeviceAuth(t *testing.T) {
	t.Setenv("GO_VOICE_GATEWAY_ICE_SERVERS", "stun:stun.example.com:3478")
	t.Setenv("GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET", "true")
	t.Setenv("GO_VOICE_GATEWAY_DEVICE_AUTH_BACKEND", deviceAuthBackendPythonGateway)

	cfg, err := LoadConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.DeviceAuth.RequireRobotSecret ||
		cfg.DeviceAuth.Backend != deviceAuthBackendPythonGateway {
		t.Fatalf("unexpected device auth config: %+v", cfg.DeviceAuth)
	}
}

func TestNewServerConfiguresASRDryRunSink(t *testing.T) {
	cfg := testConfig()
	cfg.ASR = ASRConfig{
		HandoffDryRunEnabled: true,
		HandoffQueueSize:     2,
	}
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	managed, ok := server.voiceEvents.(*configuredASRVoiceEventSink)
	if !ok {
		t.Fatalf("voiceEvents = %T, want configuredASRVoiceEventSink", server.voiceEvents)
	}
	if managed.processor != asrProcessorDryRun {
		t.Fatalf("processor = %q, want dry run", managed.processor)
	}
	session := &gatewaySession{
		id:     "session_test",
		server: server,
	}
	session.recordCanonicalEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		UtteranceID: "utt_1",
	})
	session.recordCanonicalEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		Transport:        canonicalTransportWebRTCRTP,
		TrackID:          "microphone",
		StreamID:         "voice",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       []byte("abc"),
	})
	session.recordCanonicalEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		UtteranceID: "utt_1",
	})
	server.Close()

	prototypeSnapshot := managed.prototype.Snapshot()
	if len(prototypeSnapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", prototypeSnapshot.Completed)
	}
	queueSnapshot := managed.handoffQueue.Snapshot()
	if queueSnapshot.ProcessedCount != 1 ||
		queueSnapshot.DroppedCount != 0 ||
		queueSnapshot.ErrorCount != 0 {
		t.Fatalf("unexpected handoff queue snapshot: %+v", queueSnapshot)
	}
}

func TestNewServerConfiguresASRPythonGatewayBridgeSink(t *testing.T) {
	cfg := testConfig()
	cfg.ASR = ASRConfig{
		Processor:                 asrProcessorPythonGateway,
		HandoffQueueSize:          2,
		PythonGatewayWSURL:        "ws://127.0.0.1:18000/ws",
		PythonGatewayClientType:   "go_test",
		PythonGatewayTimeoutMS:    1000,
		PythonGatewayMaxMessageKB: 64,
	}
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	managed, ok := server.voiceEvents.(*configuredASRVoiceEventSink)
	if !ok {
		t.Fatalf("voiceEvents = %T, want configuredASRVoiceEventSink", server.voiceEvents)
	}
	if managed.processor != asrProcessorPythonGateway {
		t.Fatalf("processor = %q, want python gateway", managed.processor)
	}
	adapter, ok := managed.handoffQueue.processor.(ASRAudioRequestProcessorAdapter)
	if !ok {
		t.Fatalf("handoff processor = %T, want ASRAudioRequestProcessorAdapter", managed.handoffQueue.processor)
	}
	reporting, ok := adapter.processor.(asrBridgeErrorReportingProcessor)
	if !ok {
		t.Fatalf("ASR request processor = %T, want asrBridgeErrorReportingProcessor", adapter.processor)
	}
	bridge, ok := reporting.processor.(*PythonGatewayBridgeProcessor)
	if !ok {
		t.Fatalf("wrapped ASR request processor = %T, want *PythonGatewayBridgeProcessor", reporting.processor)
	}
	if server.clientEvents != bridge {
		t.Fatalf("client event processor and ASR bridge are not shared")
	}
	if bridge.credentialResolver != server.sessions {
		t.Fatalf("Python Gateway bridge credential resolver is not the session registry")
	}
}

func TestNewServerRoutesCandidateCommitsThroughActiveInternalVoiceSession(t *testing.T) {
	cfg := testConfig()
	cfg.ASR = ASRConfig{
		Processor:                   asrProcessorPythonGateway,
		HandoffQueueSize:            2,
		PythonGatewayWSURL:          "ws://127.0.0.1:18000/ws",
		PythonGatewayClientType:     "go_test",
		PythonGatewayTimeoutMS:      1000,
		PythonGatewayTotalTimeoutMS: 2000,
		PythonGatewayMaxMessageKB:   64,
	}
	cfg.InternalVoice.Enabled = true
	cfg.InternalVoice.InputAudioEnabled = true
	cfg.InternalVoice.InputAudioMode = "active"
	cfg.InternalVoice.WSURL = "ws://127.0.0.1:18000/internal/voice/ws"
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	managed, ok := server.voiceEvents.(*configuredASRVoiceEventSink)
	if !ok {
		t.Fatalf("voiceEvents = %T, want configuredASRVoiceEventSink", server.voiceEvents)
	}
	commit, ok := managed.primaryBridge.(internalVoiceCommitProcessor)
	if !ok {
		t.Fatalf("candidate commit processor = %T, want internalVoiceCommitProcessor", managed.primaryBridge)
	}
	if commit.sessions != server.sessions || commit.downlink != server.sessions {
		t.Fatal("candidate commit processor must reuse the active gateway session registry")
	}
	adapter := managed.handoffQueue.processor.(ASRAudioRequestProcessorAdapter)
	reporting := adapter.processor.(asrBridgeErrorReportingProcessor)
	if _, ok := reporting.processor.(internalVoiceAudioProcessor); !ok {
		t.Fatalf("audio processor = %T, want internalVoiceAudioProcessor", reporting.processor)
	}
}

func TestNewServerConfiguresTurnCandidateBridgeSessionCredentials(t *testing.T) {
	cfg := testConfig()
	cfg.ASR = ASRConfig{
		Processor:                     asrProcessorPythonGateway,
		HandoffQueueSize:              2,
		PythonGatewayWSURL:            "ws://127.0.0.1:18000/ws",
		PythonGatewayClientType:       "go_test",
		PythonGatewayTimeoutMS:        1000,
		PythonGatewayMaxMessageKB:     64,
		TurnGateShadowEnabled:         true,
		TurnGateActiveEnabled:         true,
		TurnGateActiveDeadlineMS:      300,
		TurnGateShadowTimeoutMS:       5000,
		TurnGateShadowSnapshotGraceMS: 30,
	}
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	managed, ok := server.voiceEvents.(*configuredASRVoiceEventSink)
	if !ok {
		t.Fatalf("voiceEvents = %T, want configuredASRVoiceEventSink", server.voiceEvents)
	}
	if managed.candidateBridge == nil {
		t.Fatal("Turn candidate bridge was not configured")
	}
	if managed.candidateBridge == managed.primaryBridge {
		t.Fatal("Turn candidate bridge must stay isolated from the primary bridge")
	}
	if managed.candidateBridge.credentialResolver != server.sessions {
		t.Fatal("Turn candidate bridge credential resolver is not the session registry")
	}

	secret := "session_secret"
	session := &gatewaySession{id: "session_1"}
	session.storeRegistrationCredentials(clientMessage{RobotSecret: &secret}, deviceRegistration{
		RobotID:    "session_robot",
		BotID:      "xiaowen",
		ClientType: "rust",
	})
	server.sessions.Add(session)
	registerCh := make(chan map[string]any, 1)
	managed.candidateBridge.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "candidate_python_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive candidate register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "candidate_python_session"})
			for {
				var message any
				if err := websocket.JSON.Receive(conn, &message); err != nil {
					return
				}
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}
	if err := managed.candidateBridge.WarmSession("session_1"); err != nil {
		t.Fatal(err)
	}
	select {
	case register := <-registerCh:
		if register["robot_id"] != "session_robot" ||
			register["robot_secret"] != "session_secret" ||
			register["client_type"] != "rust" {
			t.Fatalf("unexpected candidate registration: %+v", register)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for candidate bridge registration")
	}
}

func TestWebSocketRegisterUsesPythonGatewayDeviceAuth(t *testing.T) {
	cfg := testConfig()
	cfg.DeviceAuth = DeviceAuthConfig{
		RequireRobotSecret: true,
		Backend:            deviceAuthBackendPythonGateway,
	}
	auth := &recordingDeviceAuthenticator{
		registration: deviceRegistration{
			RobotID:    "robot_1",
			BotID:      "bot_from_db",
			BotName:    "Bot From DB",
			ClientType: "rust",
		},
	}
	server, err := newServer(
		normalizeServerConfig(cfg),
		log.New(io.Discard, "", 0),
		nil,
		nil,
		nil,
		auth,
		nil,
		newGatewaySessionRegistry(),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()
	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	secret := "secret_1"
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:        "register",
		RobotID:     "robot_1",
		RobotSecret: &secret,
		ClientType:  "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	if registered.Type != "registered" ||
		registered.SessionID != connected.SessionID ||
		registered.RobotID != "robot_1" ||
		registered.BotID != "bot_from_db" ||
		registered.BotName != "Bot From DB" ||
		registered.ClientType != "rust" {
		t.Fatalf("unexpected registered message: %+v", registered)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if rtcConfig.Type != "rtc_config" || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("unexpected rtc_config: %+v", rtcConfig)
	}

	authRequest := auth.lastMessage
	if authRequest.RobotID != "robot_1" ||
		authRequest.RobotSecret == nil ||
		*authRequest.RobotSecret != "secret_1" ||
		authRequest.ClientType != "rust" {
		t.Fatalf("unexpected auth register: %+v", authRequest)
	}
}

func TestWebSocketRegisterAuthFailureReturnsRegisterFailed(t *testing.T) {
	cfg := testConfig()
	cfg.DeviceAuth = DeviceAuthConfig{
		RequireRobotSecret: true,
		Backend:            deviceAuthBackendPythonGateway,
	}
	server, err := newServer(
		normalizeServerConfig(cfg),
		log.New(io.Discard, "", 0),
		nil,
		nil,
		nil,
		&recordingDeviceAuthenticator{err: errors.New("robot_secret 校验失败")},
		nil,
		newGatewaySessionRegistry(),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()
	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	secret := "wrong"
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:        "register",
		RobotID:     "robot_1",
		RobotSecret: &secret,
		ClientType:  "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var errMsg errorMessage
	if err := websocket.JSON.Receive(conn, &errMsg); err != nil {
		t.Fatal(err)
	}
	if errMsg.Type != "error" ||
		errMsg.Code != "REGISTER_FAILED" ||
		!strings.Contains(errMsg.Message, "robot_secret 校验失败") {
		t.Fatalf("unexpected register error: %+v", errMsg)
	}
}

func TestServerStatusIncludesASRDryRunState(t *testing.T) {
	cfg := testConfig()
	cfg.ASR = ASRConfig{
		HandoffDryRunEnabled: true,
		HandoffQueueSize:     3,
	}
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	rtpKey := rtpTrackKey{kind: "audio", id: "microphone", streamID: "voice"}
	session := &gatewaySession{
		id:         "rtc_status",
		server:     server,
		connInfo:   connectionInfo{SourceIP: "203.0.113.10", RemoteIP: "127.0.0.1", RemoteAddr: "127.0.0.1:50000"},
		createdAt:  time.Now().Add(-2 * time.Second),
		robotID:    "test_01",
		clientType: "rust",
		botID:      "xiaowen",
		rtp: map[rtpTrackKey]*rtpTrackStats{
			rtpKey: {
				key:          rtpKey,
				packetCount:  3,
				payloadBytes: 99,
				recent:       []cachedRTPPacket{{payload: []byte{1, 2, 3}}},
			},
		},
	}
	session.downBlocked.Store(true)
	session.downGen.Store(2)
	server.sessions.Add(session)

	recorder := httptest.NewRecorder()
	server.handleStatus(recorder, httptest.NewRequest(http.MethodGet, "/internal/status", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status code = %d, want %d", recorder.Code, http.StatusOK)
	}

	var payload map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	asr, ok := payload["asr"].(map[string]any)
	if !ok {
		t.Fatalf("missing ASR status: %+v", payload)
	}
	if asr["handoff_dry_run_enabled"] != true || asr["handoff_queue_size"] != float64(3) {
		t.Fatalf("unexpected ASR status: %+v", asr)
	}
	audio, ok := payload["audio"].(map[string]any)
	if !ok {
		t.Fatalf("missing audio status: %+v", payload)
	}
	if audio["downlink_transport"] != downlinkAudioTransportWebRTCRTP {
		t.Fatalf("unexpected audio status: %+v", audio)
	}
	iceNetworks, ok := payload["ice_networks"].([]any)
	if !ok || len(iceNetworks) != 1 || iceNetworks[0] != "udp4" {
		t.Fatalf("unexpected ice_networks status: %+v", payload["ice_networks"])
	}
	voiceEvents, ok := payload["voice_events"].(map[string]any)
	if !ok {
		t.Fatalf("missing voice_events status: %+v", payload)
	}
	if voiceEvents["type"] != "asr_prototype" || voiceEvents["processor"] != asrProcessorDryRun {
		t.Fatalf("unexpected voice_events status: %+v", voiceEvents)
	}
	sessions, ok := payload["sessions"].(map[string]any)
	if !ok {
		t.Fatalf("missing sessions status: %+v", payload)
	}
	if sessions["total"] != float64(1) || sessions["registered"] != float64(1) {
		t.Fatalf("unexpected session counts: %+v", sessions)
	}
	items, ok := sessions["items"].([]any)
	if !ok || len(items) != 1 {
		t.Fatalf("unexpected session items: %+v", sessions["items"])
	}
	item, ok := items[0].(map[string]any)
	if !ok {
		t.Fatalf("unexpected session item: %+v", items[0])
	}
	if item["session_id"] != "rtc_status" ||
		item["source_ip"] != "203.0.113.10" ||
		item["robot_id"] != "test_01" ||
		item["client_type"] != "rust" ||
		item["bot_id"] != "xiaowen" ||
		item["registered"] != true ||
		item["downlink_blocked"] != true ||
		item["downlink_generation"] != float64(2) {
		t.Fatalf("unexpected session item: %+v", item)
	}
	tracks, ok := item["rtp_tracks"].([]any)
	if !ok || len(tracks) != 1 {
		t.Fatalf("unexpected RTP tracks: %+v", item["rtp_tracks"])
	}
	track, ok := tracks[0].(map[string]any)
	if !ok ||
		track["kind"] != "audio" ||
		track["track_id"] != "microphone" ||
		track["packets"] != float64(3) ||
		track["payload_bytes"] != float64(99) {
		t.Fatalf("unexpected RTP track: %+v", tracks[0])
	}

	healthRecorder := httptest.NewRecorder()
	server.handleHealthz(healthRecorder, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if healthRecorder.Code != http.StatusOK {
		t.Fatalf("health code = %d, want %d", healthRecorder.Code, http.StatusOK)
	}
	var health map[string]any
	if err := json.Unmarshal(healthRecorder.Body.Bytes(), &health); err != nil {
		t.Fatal(err)
	}
	if health["active_sessions"] != float64(1) ||
		health["asr_processor"] != asrProcessorDryRun ||
		health["downlink_transport"] != downlinkAudioTransportWebRTCRTP {
		t.Fatalf("unexpected health payload: %+v", health)
	}
}

func TestServerStatusIncludesPythonGatewayBridgeHealth(t *testing.T) {
	cfg := testConfig()
	clientEvents := &recordingBridgeHealthClientEventProcessor{
		health: map[string]any{
			"connection_mode":          "session",
			"active_connections":       1,
			"connection_open_failures": 0,
		},
	}
	server, err := newServer(
		cfg,
		log.New(io.Discard, "", 0),
		noopVoiceEventSink{},
		clientEvents,
		nil,
		nil,
		nil,
		newGatewaySessionRegistry(),
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	recorder := httptest.NewRecorder()
	server.handleStatus(recorder, httptest.NewRequest(http.MethodGet, "/internal/status", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status code = %d, want %d", recorder.Code, http.StatusOK)
	}

	var payload map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	bridge, ok := payload["python_gateway_bridge"].(map[string]any)
	if !ok {
		t.Fatalf("missing python_gateway_bridge status: %+v", payload)
	}
	if bridge["connection_mode"] != "session" ||
		bridge["active_connections"] != float64(1) ||
		bridge["connection_open_failures"] != float64(0) {
		t.Fatalf("unexpected python_gateway_bridge status: %+v", bridge)
	}
}

func TestArgValueSupportsSeparateAndEqualsForms(t *testing.T) {
	originalArgs := os.Args
	defer func() { os.Args = originalArgs }()

	os.Args = []string{"go_voice_gateway", "--healthcheck-url", "http://127.0.0.1:8282/healthz"}
	value, ok := argValue("--healthcheck-url")
	if !ok || value != "http://127.0.0.1:8282/healthz" {
		t.Fatalf("argValue separate form = %q, %t", value, ok)
	}

	os.Args = []string{"go_voice_gateway", "--healthcheck-url=http://127.0.0.1:8282/healthz"}
	value, ok = argValue("--healthcheck-url")
	if !ok || value != "http://127.0.0.1:8282/healthz" {
		t.Fatalf("argValue equals form = %q, %t", value, ok)
	}
}

func TestRunHTTPHealthcheck(t *testing.T) {
	if err := runHTTPHealthcheckWithClient("http://127.0.0.1:8282/healthz", fakeHealthcheckClient{
		statusCode: http.StatusOK,
		status:     "200 OK",
	}); err != nil {
		t.Fatalf("runHTTPHealthcheckWithClient ok failed: %v", err)
	}

	if err := runHTTPHealthcheckWithClient("http://127.0.0.1:8282/healthz", fakeHealthcheckClient{
		statusCode: http.StatusServiceUnavailable,
		status:     "503 Service Unavailable",
	}); err == nil {
		t.Fatal("runHTTPHealthcheck unexpectedly accepted 503")
	}

	if err := runHTTPHealthcheckWithClient("", fakeHealthcheckClient{}); err == nil {
		t.Fatal("runHTTPHealthcheck unexpectedly accepted empty URL")
	}
}

func TestHealthcheckURLFromListenAddr(t *testing.T) {
	tests := map[string]string{
		"":                 "http://127.0.0.1:8282/healthz",
		"0.0.0.0:8282":     "http://127.0.0.1:8282/healthz",
		":8283":            "http://127.0.0.1:8283/healthz",
		"8284":             "http://127.0.0.1:8284/healthz",
		"[::]:8285":        "http://127.0.0.1:8285/healthz",
		"invalid-listener": "http://127.0.0.1:8282/healthz",
	}

	for addr, want := range tests {
		if got := healthcheckURLFromListenAddr(addr); got != want {
			t.Fatalf("healthcheckURLFromListenAddr(%q) = %q, want %q", addr, got, want)
		}
	}
}

type fakeHealthcheckClient struct {
	statusCode int
	status     string
	err        error
}

func (c fakeHealthcheckClient) Do(*http.Request) (*http.Response, error) {
	if c.err != nil {
		return nil, c.err
	}
	return &http.Response{
		StatusCode: c.statusCode,
		Status:     c.status,
		Body:       io.NopCloser(strings.NewReader("")),
	}, nil
}

func TestWebSocketSignalingAnswersOfferAndDataChannel(t *testing.T) {
	cfg := testConfig()
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if connected.Type != "connected" || connected.SessionID == "" {
		t.Fatalf("unexpected connected message: %+v", connected)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	if registered.Type != "registered" || registered.SessionID != connected.SessionID {
		t.Fatalf("unexpected registered message: %+v", registered)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if rtcConfig.Type != "rtc_config" || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("unexpected rtc_config: %+v", rtcConfig)
	}

	clientAPI, err := newWebRTCAPI(cfg)
	if err != nil {
		t.Fatal(err)
	}
	clientPC, err := clientAPI.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	defer clientPC.Close()

	var candidateMu sync.Mutex
	candidateCount := 0
	clientPC.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		if candidate == nil {
			return
		}
		candidateMu.Lock()
		candidateCount++
		candidateMu.Unlock()
	})

	audioTrack, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus},
		"microphone",
		"voice",
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := clientPC.AddTrack(audioTrack); err != nil {
		t.Fatal(err)
	}

	pongCh := make(chan string, 1)
	openCh := make(chan struct{})
	visionOpenCh := make(chan struct{})
	visionAckCh := make(chan visionSnapshotAck, 1)
	rtpWriteCh := make(chan error, 1)
	dc, err := clientPC.CreateDataChannel("control", nil)
	if err != nil {
		t.Fatal(err)
	}
	dc.OnOpen(func() {
		close(openCh)
		rtpWriteCh <- audioTrack.WriteRTP(&rtp.Packet{
			Header: rtp.Header{
				Version:        2,
				PayloadType:    111,
				SequenceNumber: 1,
				Timestamp:      960,
				SSRC:           0x575a4b01,
			},
			Payload: []byte{0xf8, 0xff, 0xfe},
		})
		_ = dc.SendText("ping")
	})
	dc.OnMessage(func(message webrtc.DataChannelMessage) {
		pongCh <- string(message.Data)
	})
	visionOrdered := false
	visionMaxRetransmits := uint16(0)
	visionDC, err := clientPC.CreateDataChannel(visionDataChannelLabel, &webrtc.DataChannelInit{
		Ordered:        &visionOrdered,
		MaxRetransmits: &visionMaxRetransmits,
	})
	if err != nil {
		t.Fatal(err)
	}
	visionDC.OnOpen(func() {
		close(visionOpenCh)
	})
	visionDC.OnMessage(func(message webrtc.DataChannelMessage) {
		var ack visionSnapshotAck
		if err := json.Unmarshal(message.Data, &ack); err == nil {
			visionAckCh <- ack
		}
	})

	offer, err := clientPC.CreateOffer(nil)
	if err != nil {
		t.Fatal(err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(clientPC)
	if err := clientPC.SetLocalDescription(offer); err != nil {
		t.Fatal(err)
	}
	waitForTestChannel(t, gatherComplete, "client ICE gathering")
	candidateMu.Lock()
	gatheredCandidates := candidateCount
	candidateMu.Unlock()
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:      "rtc_offer",
		SessionID: connected.SessionID,
		SDP:       clientPC.LocalDescription().SDP,
	}); err != nil {
		t.Fatal(err)
	}

	var answer rtcAnswerMessage
	if err := websocket.JSON.Receive(conn, &answer); err != nil {
		t.Fatal(err)
	}
	if answer.Type != "rtc_answer" || answer.SessionID != connected.SessionID {
		t.Fatalf("unexpected rtc_answer: %+v", answer)
	}
	if !strings.Contains(answer.SDP, "m=audio") {
		t.Fatalf("rtc_answer missing audio m-line: %s", answer.SDP)
	}
	if !strings.Contains(strings.ToLower(answer.SDP), "opus") {
		t.Fatalf("rtc_answer missing opus codec: %s", answer.SDP)
	}
	if err := clientPC.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answer.SDP,
	}); err != nil {
		t.Fatal(err)
	}
	if gatheredCandidates == 0 {
		t.Skip("local sandbox exposed no ICE host candidates; rtc_offer/rtc_answer passed, skipping DataChannel connectivity check")
	}

	waitForTestChannel(t, openCh, "data channel open")
	waitForTestChannel(t, visionOpenCh, "vision data channel open")
	select {
	case err := <-rtpWriteCh:
		if err != nil {
			t.Fatalf("write RTP probe: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for RTP probe write")
	}
	select {
	case pong := <-pongCh:
		if pong != "pong:ping" {
			t.Fatalf("data channel pong = %q, want pong:ping", pong)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for data channel pong")
	}
	if err := dc.SendText(`{"type":"heartbeat"}`); err != nil {
		t.Fatalf("send data channel heartbeat: %v", err)
	}
	select {
	case pong := <-pongCh:
		if pong != `{"type":"heartbeat_ack"}` {
			t.Fatalf("data channel heartbeat ack = %q", pong)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for data channel heartbeat ack")
	}

	visionNow := time.Now()
	visionJPEG := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	visionChunks := buildVisionChunks(t, 21, visionNow.UnixMilli(), visionJPEG, 3)
	for _, chunk := range visionChunks {
		if err := visionDC.Send(chunk); err != nil {
			t.Fatalf("send vision chunk: %v", err)
		}
	}
	select {
	case ack := <-visionAckCh:
		if ack.Status != "accepted" || ack.FrameID != "21" {
			t.Fatalf("vision data channel ack = %+v", ack)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for vision data channel ack")
	}
	session := server.sessions.Lookup(connected.SessionID)
	if session == nil {
		t.Fatal("gateway session missing after vision upload")
	}
	snapshot, ok := session.latestUsableVisionSnapshot(time.Now())
	if !ok || snapshot.frameID != 21 || !bytes.Equal(snapshot.jpeg, visionJPEG) {
		t.Fatal("gateway did not retain the latest vision snapshot")
	}
}

func TestWebSocketSignalingAcceptsClientFallback(t *testing.T) {
	cfg := testConfig()
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if registered.SessionID != connected.SessionID || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("session mismatch connected=%s registered=%s rtc_config=%s", connected.SessionID, registered.SessionID, rtcConfig.SessionID)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:          "transport_fallback_start",
		SessionID:     connected.SessionID,
		FromTransport: "webrtc_rtp",
		ToTransport:   "websocket",
		Reason:        "client_webrtc_offer_not_available",
	}); err != nil {
		t.Fatal(err)
	}
	var ack transportFallbackAckMessage
	if err := websocket.JSON.Receive(conn, &ack); err != nil {
		t.Fatal(err)
	}
	if ack.Type != "transport_fallback_ack" || ack.ActiveTransport != "websocket" {
		t.Fatalf("unexpected fallback ack: %+v", ack)
	}
	if ack.Reason == nil || *ack.Reason != "client_webrtc_offer_not_available" {
		t.Fatalf("fallback reason = %v", ack.Reason)
	}
}

func TestWebSocketSignalingForwardsClientEvent(t *testing.T) {
	cfg := testConfig()
	processor := &recordingClientEventProcessor{messages: make(chan clientMessage, 1)}
	server, err := newServer(
		cfg,
		log.New(io.Discard, "", 0),
		nil,
		processor,
		nil,
		nil,
		nil,
		nil,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if registered.SessionID != connected.SessionID || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("session mismatch connected=%s registered=%s rtc_config=%s", connected.SessionID, registered.SessionID, rtcConfig.SessionID)
	}

	if err := websocket.JSON.Send(conn, clientMessage{
		Type:      "client_event",
		SessionID: connected.SessionID,
		Event:     "wake_interrupt",
		EventID:   "wake_interrupt-1",
		Source:    "hardware_wake",
		BotID:     "xiaowen",
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case msg := <-processor.messages:
		if msg.Type != "client_event" ||
			msg.Event != "wake_interrupt" ||
			msg.EventID != "wake_interrupt-1" ||
			msg.Source != "hardware_wake" ||
			msg.BotID != "xiaowen" {
			t.Fatalf("forwarded client_event = %+v", msg)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for forwarded client_event")
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:      "client_event",
		SessionID: connected.SessionID,
		Event:     "wake_interrupt",
		EventID:   "wake_interrupt-1",
		Source:    "hardware_wake",
		BotID:     "xiaowen",
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case msg := <-processor.messages:
		t.Fatalf("duplicate client_event was forwarded: %+v", msg)
	case <-time.After(100 * time.Millisecond):
	}
	if err := websocket.JSON.Send(conn, clientMessage{Type: "ping"}); err != nil {
		t.Fatal(err)
	}
	var pong simpleTypeMessage
	if err := websocket.JSON.Receive(conn, &pong); err != nil {
		t.Fatal(err)
	}
	if pong.Type != "pong" {
		t.Fatalf("message after client_event = %+v, want pong", pong)
	}
}

func TestWebSocketSignalingForwardsText(t *testing.T) {
	cfg := testConfig()
	processor := &recordingClientEventProcessor{messages: make(chan clientMessage, 1)}
	server, err := newServer(
		cfg,
		log.New(io.Discard, "", 0),
		nil,
		processor,
		nil,
		nil,
		nil,
		nil,
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if registered.SessionID != connected.SessionID || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("session mismatch connected=%s registered=%s rtc_config=%s", connected.SessionID, registered.SessionID, rtcConfig.SessionID)
	}

	if err := websocket.JSON.Send(conn, clientMessage{
		Type:      "text",
		SessionID: connected.SessionID,
		Content:   "请根据以下环境主动回应：有人靠近",
		Source:    "environment_http",
		BotID:     "xiaowen",
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case msg := <-processor.messages:
		if msg.Type != "text" ||
			msg.Content != "请根据以下环境主动回应：有人靠近" ||
			msg.Source != "environment_http" ||
			msg.BotID != "xiaowen" {
			t.Fatalf("forwarded text = %+v", msg)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for forwarded text")
	}
	if err := websocket.JSON.Send(conn, clientMessage{Type: "ping"}); err != nil {
		t.Fatal(err)
	}
	var pong simpleTypeMessage
	if err := websocket.JSON.Receive(conn, &pong); err != nil {
		t.Fatal(err)
	}
	if pong.Type != "pong" {
		t.Fatalf("message after text = %+v, want pong", pong)
	}
}

func TestWebSocketSignalingAcceptsTransportReady(t *testing.T) {
	cfg := testConfig()
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if registered.SessionID != connected.SessionID || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("session mismatch connected=%s registered=%s rtc_config=%s", connected.SessionID, registered.SessionID, rtcConfig.SessionID)
	}

	if err := websocket.JSON.Send(conn, clientMessage{
		Type:            "transport_ready",
		SessionID:       connected.SessionID,
		ActiveTransport: "webrtc_datachannel",
	}); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{Type: "ping"}); err != nil {
		t.Fatal(err)
	}
	var pong simpleTypeMessage
	if err := websocket.JSON.Receive(conn, &pong); err != nil {
		t.Fatal(err)
	}
	if pong.Type != "pong" {
		t.Fatalf("message after transport_ready = %+v, want pong", pong)
	}
}

func TestWebSocketSignalingAcceptsInterrupt(t *testing.T) {
	cfg := testConfig()
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{Type: "interrupt"}); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(conn, clientMessage{Type: "ping"}); err != nil {
		t.Fatal(err)
	}
	var pong simpleTypeMessage
	if err := websocket.JSON.Receive(conn, &pong); err != nil {
		t.Fatal(err)
	}
	if pong.Type != "pong" {
		t.Fatalf("message after interrupt = %+v, want pong", pong)
	}
}

func TestActiveInternalVoiceInterruptAlsoCancelsActiveLegacyText(t *testing.T) {
	processor := &recordingClientEventProcessor{
		activeControls: make(chan clientMessage, 1),
	}
	cfg := testConfig()
	cfg.InternalVoice.Enabled = true
	cfg.InternalVoice.InputAudioEnabled = true
	cfg.InternalVoice.InputAudioMode = "active"
	session := &gatewaySession{
		id: "session_test",
		server: &Server{
			cfg:          cfg,
			logger:       log.New(io.Discard, "", 0),
			clientEvents: processor,
		},
	}
	if err := session.handleDataChannelClientMessage(nil, clientMessage{
		Type:   "interrupt",
		Reason: "user_interrupt",
	}); err != nil {
		t.Fatal(err)
	}
	control := waitForClientMessage(t, processor.activeControls, "active text control")
	if control.Type != "interrupt" || control.Reason != "user_interrupt" {
		t.Fatalf("unexpected active text control: %+v", control)
	}
}

func TestRTCIceCandidateMessageConstructor(t *testing.T) {
	mid := "0"
	lineIndex := uint16(0)
	msg := newRTCIceCandidateMessage(
		"session_1",
		"candidate:1 1 udp 2130706431 127.0.0.1 9 typ host",
		&mid,
		&lineIndex,
	)

	if msg.Type != "rtc_ice_candidate" {
		t.Fatalf("Type = %q, want rtc_ice_candidate", msg.Type)
	}
	if msg.SessionID != "session_1" || msg.Candidate == "" {
		t.Fatalf("unexpected candidate message: %+v", msg)
	}
	if msg.SDPMid == nil || *msg.SDPMid != "0" {
		t.Fatalf("SDPMid = %v, want 0", msg.SDPMid)
	}
	if msg.SDPMLineIndex == nil || *msg.SDPMLineIndex != 0 {
		t.Fatalf("SDPMLineIndex = %v, want 0", msg.SDPMLineIndex)
	}
}

func testConfig() Config {
	return Config{
		Addr:               "127.0.0.1:0",
		WebSocketPath:      "/ws",
		BotID:              "xiaowen",
		RTCEnabled:         true,
		ICETransportPolicy: webrtc.ICETransportPolicyAll,
		ICENetworkTypes:    []webrtc.NetworkType{webrtc.NetworkTypeUDP4},
		Audio: AudioConfig{
			Direction:  "sendrecv",
			Codec:      "opus",
			SampleRate: 48000,
			Channels:   1,
			PtimeMS:    20,
		},
		MessageLimitBytes:  defaultMessageMaxByte,
		NegotiationTimeout: 5 * time.Second,
		WriteTimeout:       3 * time.Second,
	}
}

type recordingDeviceAuthenticator struct {
	registration deviceRegistration
	err          error
	lastMessage  clientMessage
}

type recordingClientEventProcessor struct {
	messages       chan clientMessage
	controls       chan clientMessage
	activeControls chan clientMessage
	closedSessions chan string
}

type recordingBridgeHealthClientEventProcessor struct {
	recordingClientEventProcessor
	health map[string]any
}

func (p *recordingBridgeHealthClientEventProcessor) BridgeHealthStatus() map[string]any {
	return p.health
}

func (p *recordingClientEventProcessor) ProcessClientEvent(_ string, message clientMessage) error {
	if p.messages != nil {
		p.messages <- message
	}
	return nil
}

func (p *recordingClientEventProcessor) ProcessClientText(_ string, message clientMessage) error {
	if p.messages != nil {
		p.messages <- message
	}
	return nil
}

func (p *recordingClientEventProcessor) ProcessClientControl(_ string, message clientMessage) error {
	if p.controls != nil {
		p.controls <- message
	}
	return nil
}

func (p *recordingClientEventProcessor) ProcessClientControlDuringActive(_ string, message clientMessage) (bool, error) {
	if p.activeControls != nil {
		p.activeControls <- message
		return true, nil
	}
	return false, nil
}

func (p *recordingClientEventProcessor) CloseClientSession(sessionID string) {
	if p.closedSessions != nil {
		p.closedSessions <- sessionID
	}
}

func (a *recordingDeviceAuthenticator) AuthenticateRegister(_ context.Context, msg clientMessage) (deviceRegistration, error) {
	a.lastMessage = msg
	if a.err != nil {
		return deviceRegistration{}, a.err
	}
	return a.registration, nil
}

func waitForClientMessage(t *testing.T, ch <-chan clientMessage, label string) clientMessage {
	t.Helper()
	select {
	case message := <-ch:
		return message
	case <-time.After(5 * time.Second):
		t.Fatalf("timeout waiting for %s", label)
		return clientMessage{}
	}
}

func waitForTestChannel(t *testing.T, ch <-chan struct{}, label string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatalf("timeout waiting for %s", label)
	}
}

func newInMemoryWebSocket(t *testing.T, handler http.Handler, path string) (*websocket.Conn, func()) {
	t.Helper()
	serverConn, clientConn := net.Pipe()
	done := make(chan error, 1)

	go func() {
		reader := bufio.NewReader(serverConn)
		readWriter := bufio.NewReadWriter(reader, bufio.NewWriter(serverConn))
		req, err := http.ReadRequest(reader)
		if err != nil {
			done <- err
			return
		}
		handler.ServeHTTP(&hijackResponseWriter{
			conn:   serverConn,
			rw:     readWriter,
			header: http.Header{},
		}, req)
		done <- nil
	}()

	url := "ws://voice-gateway.test" + path
	config, err := websocket.NewConfig(url, "http://voice-gateway.test")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := websocket.NewClient(config, clientConn)
	if err != nil {
		_ = clientConn.Close()
		t.Fatal(err)
	}

	closeFn := func() {
		_ = conn.Close()
		select {
		case err := <-done:
			if err != nil && !strings.Contains(err.Error(), "closed") && err != io.EOF {
				t.Logf("in-memory websocket server closed with: %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Log("timeout waiting for in-memory websocket server shutdown")
		}
	}
	return conn, closeFn
}

type hijackResponseWriter struct {
	conn   net.Conn
	rw     *bufio.ReadWriter
	header http.Header
}

func (w *hijackResponseWriter) Header() http.Header {
	return w.header
}

func (w *hijackResponseWriter) WriteHeader(_ int) {}

func (w *hijackResponseWriter) Write(payload []byte) (int, error) {
	return w.rw.Write(payload)
}

func (w *hijackResponseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return w.conn, w.rw, nil
}
