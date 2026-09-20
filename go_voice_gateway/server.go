package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

const maxCachedRTPPackets = 64
const downlinkQueueCapacity = 128

const (
	clientEventDedupeWindow     = 2 * time.Second
	maxInternalVoiceLazyOpen    = 800 * time.Millisecond
	maxRecentClientEventDedupes = 64
)

const (
	downlinkAudioTrackID  = "tts"
	downlinkAudioStreamID = "voice"
)

type Server struct {
	cfg             Config
	api             *webrtc.API
	logger          *log.Logger
	structured      *slog.Logger
	startedAt       time.Time
	voiceEvents     VoiceEventSink
	clientEvents    ClientEventProcessor
	playbackReports ClientPlaybackReportProcessor
	deviceAuth      DeviceAuthenticator
	internalVoice   *InternalVoiceClient
	pythonGateway   *PythonGatewayBridgeProcessor
	sessions        *gatewaySessionRegistry
	closeFns        []func()
	closeOnce       sync.Once
}

type gatewaySession struct {
	id                  string
	server              *Server
	conn                *websocket.Conn
	connInfo            connectionInfo
	createdAt           time.Time
	closeOnce           sync.Once
	writeMu             sync.Mutex
	identityMu          sync.RWMutex
	robotID             string
	robotSecret         string
	clientType          string
	botID               string
	pcMu                sync.Mutex
	pc                  *webrtc.PeerConnection
	controlMu           sync.Mutex
	controlDC           *webrtc.DataChannel
	downMu              sync.Mutex
	downlink            *webrtc.TrackLocalStaticRTP
	downRTP             downlinkRTPSendState
	downlinkQueue       chan queuedPythonGatewayDownlink
	downlinkDone        chan struct{}
	downlinkFailureMu   sync.Mutex
	downlinkFailureGen  uint64
	downlinkFailureErr  error
	downGen             atomic.Uint64
	downBlocked         atomic.Bool
	downTraceID         string
	downRoundID         string
	downPlayID          string
	rtpMu               sync.Mutex
	rtp                 map[rtpTrackKey]*rtpTrackStats
	eventMu             sync.Mutex
	eventSeq            uint64
	events              []CanonicalVoiceEvent
	clientEventMu       sync.Mutex
	recentClientEvents  map[string]time.Time
	internalVoiceOpenMu sync.Mutex
	internalVoiceMu     sync.Mutex
	internalVoice       *InternalVoiceSession
	vision              visionSessionState
}

func shouldForwardClientInterruptToPrimary(reason string) bool {
	return strings.TrimSpace(reason) != "natural_barge_in"
}

type queuedPythonGatewayDownlink struct {
	message    pythonGatewayBridgeMessage
	generation uint64
}

type rtpTrackKey struct {
	kind     string
	id       string
	streamID string
}

type cachedRTPPacket struct {
	sequenceNumber uint16
	timestamp      uint32
	marker         bool
	payload        []byte
}

type rtpTrackStats struct {
	key                  rtpTrackKey
	packetCount          uint64
	payloadBytes         uint64
	firstSequenceNumber  uint16
	lastSequenceNumber   uint16
	firstTimestamp       uint32
	lastTimestamp        uint32
	sequenceGaps         uint64
	timestampRegressions uint64
	recent               []cachedRTPPacket
}

type rtpTrackSnapshot struct {
	key                         rtpTrackKey
	packetCount                 uint64
	payloadBytes                uint64
	firstSequenceNumber         uint16
	lastSequenceNumber          uint16
	firstTimestamp              uint32
	lastTimestamp               uint32
	lastPayloadBytes            int
	cachedPackets               int
	sequenceGaps                uint64
	timestampRegressions        uint64
	sequenceGapDetected         bool
	timestampRegressionDetected bool
}

type rtcAnswerResult struct {
	sdp        string
	candidates []rtcIceCandidateMessage
}

func NewServer(cfg Config, logger *log.Logger) (*Server, error) {
	return NewServerWithStructuredLogger(cfg, logger, nil)
}

func NewServerWithStructuredLogger(cfg Config, logger *log.Logger, structured *slog.Logger) (*Server, error) {
	cfg = normalizeServerConfig(cfg)
	sessions := newGatewaySessionRegistry()
	var sharedPythonGatewayBridge *PythonGatewayBridgeProcessor
	if cfg.ASR.Processor == asrProcessorPythonGateway {
		var err error
		sharedPythonGatewayBridge, err = NewPythonGatewayBridgeProcessorFromASRConfig(cfg.ASR, cfg.BotID, logger)
		if err != nil {
			return nil, err
		}
		sharedPythonGatewayBridge.downlink = sessions
		sharedPythonGatewayBridge.credentialResolver = sessions
	}
	sink, closeFn, err := newConfiguredVoiceEventSink(cfg, logger, sessions, sharedPythonGatewayBridge)
	if err != nil {
		return nil, err
	}
	var closeFns []func()
	if sharedPythonGatewayBridge != nil {
		closeFns = append(closeFns, sharedPythonGatewayBridge.Close)
	}
	if closeFn != nil {
		closeFns = append(closeFns, closeFn)
	}
	var internalVoice *InternalVoiceClient
	if cfg.InternalVoice.Enabled {
		internalVoice, err = NewInternalVoiceClient(cfg.InternalVoice, logger)
		if err != nil {
			return nil, err
		}
	}
	clientEvents, err := newConfiguredClientEventProcessor(cfg, logger, sessions, sharedPythonGatewayBridge)
	if err != nil {
		return nil, err
	}
	var playbackReports ClientPlaybackReportProcessor
	if sharedPythonGatewayBridge != nil {
		playbackReports = sharedPythonGatewayBridge
	}
	server, err := newServer(cfg, logger, sink, clientEvents, playbackReports, nil, internalVoice, sessions, closeFns, structured)
	if server != nil {
		server.pythonGateway = sharedPythonGatewayBridge
	}
	return server, err
}

func NewServerWithVoiceEventSink(cfg Config, logger *log.Logger, sink VoiceEventSink) (*Server, error) {
	cfg = normalizeServerConfig(cfg)
	return newServer(cfg, logger, sink, nil, nil, nil, nil, newGatewaySessionRegistry(), nil)
}

func normalizeServerConfig(cfg Config) Config {
	if cfg.ASR.Processor == "" && cfg.ASR.HandoffDryRunEnabled {
		cfg.ASR.Processor = asrProcessorDryRun
	}
	if cfg.ASR.Processor == asrProcessorDryRun {
		cfg.ASR.HandoffDryRunEnabled = true
	}
	if cfg.ASR.HandoffQueueSize <= 0 {
		cfg.ASR.HandoffQueueSize = defaultASREncodedAudioHandoffQueueSize
	}
	if cfg.ASR.PythonGatewayWSURL == "" {
		cfg.ASR.PythonGatewayWSURL = defaultPythonGatewayWSURL
	}
	if cfg.ASR.PythonGatewayClientType == "" {
		cfg.ASR.PythonGatewayClientType = "go_voice_gateway"
	}
	if cfg.ASR.PythonGatewayTimeoutMS <= 0 {
		cfg.ASR.PythonGatewayTimeoutMS = 30000
	}
	if cfg.ASR.PythonGatewayMaxMessageKB <= 0 {
		cfg.ASR.PythonGatewayMaxMessageKB = 1024
	}
	if cfg.DeviceAuth.Backend == "" {
		if cfg.DeviceAuth.RequireRobotSecret {
			cfg.DeviceAuth.Backend = deviceAuthBackendPythonGateway
		} else {
			cfg.DeviceAuth.Backend = deviceAuthBackendNone
		}
	}
	if cfg.Audio.DownlinkTransport == "" {
		cfg.Audio.DownlinkTransport = defaultDownlinkAudioTransport
	}
	normalizedInternalVoice, err := normalizeInternalVoiceConfig(cfg.InternalVoice)
	if err == nil {
		cfg.InternalVoice = normalizedInternalVoice
	}
	return cfg
}

func newServer(cfg Config, logger *log.Logger, sink VoiceEventSink, clientEvents ClientEventProcessor, playbackReports ClientPlaybackReportProcessor, deviceAuth DeviceAuthenticator, internalVoice *InternalVoiceClient, sessions *gatewaySessionRegistry, closeFns []func(), structuredLogger ...*slog.Logger) (*Server, error) {
	api, err := newWebRTCAPI(cfg)
	if err != nil {
		return nil, err
	}
	if logger == nil {
		logger = log.Default()
	}
	if sink == nil {
		sink = noopVoiceEventSink{}
	}
	if sessions == nil {
		sessions = newGatewaySessionRegistry()
	}
	if deviceAuth == nil {
		deviceAuth, err = newConfiguredDeviceAuthenticator(cfg, logger)
		if err != nil {
			return nil, err
		}
	}
	var structured *slog.Logger
	if len(structuredLogger) > 0 {
		structured = structuredLogger[0]
	}
	s := &Server{
		cfg:             cfg,
		api:             api,
		logger:          logger,
		structured:      structured,
		startedAt:       time.Now(),
		voiceEvents:     sink,
		clientEvents:    clientEvents,
		playbackReports: playbackReports,
		deviceAuth:      deviceAuth,
		internalVoice:   internalVoice,
		sessions:        sessions,
		closeFns:        append([]func(){}, closeFns...),
	}
	return s, nil
}

func (s *Server) Close() {
	s.closeOnce.Do(func() {
		closedSessions := s.sessions.CloseAll()
		if closedSessions > 0 {
			s.logInfo("server_sessions_closed", "count", closedSessions)
		}
		for i := len(s.closeFns) - 1; i >= 0; i-- {
			s.closeFns[i]()
		}
	})
}

func (s *Server) logInfo(message string, args ...any) {
	if s != nil && s.structured != nil {
		s.structured.Info(message, args...)
		return
	}
	if s != nil && s.logger != nil {
		s.logger.Printf("%s %s", message, logAttrText(args...))
	}
}

func (s *Server) logWarn(message string, args ...any) {
	if s != nil && s.structured != nil {
		s.structured.Warn(message, args...)
		return
	}
	if s != nil && s.logger != nil {
		s.logger.Printf("WARN %s %s", message, logAttrText(args...))
	}
}

func (s *Server) logError(message string, args ...any) {
	if s != nil && s.structured != nil {
		s.structured.Error(message, args...)
		return
	}
	if s != nil && s.logger != nil {
		s.logger.Printf("ERROR %s %s", message, logAttrText(args...))
	}
}

func logAttrText(args ...any) string {
	if len(args) == 0 {
		return ""
	}
	var builder strings.Builder
	for i := 0; i < len(args); i += 2 {
		if i > 0 {
			builder.WriteByte(' ')
		}
		builder.WriteString(fmt.Sprint(args[i]))
		builder.WriteByte('=')
		if i+1 >= len(args) {
			builder.WriteString("<missing>")
			break
		}
		builder.WriteString(fmt.Sprint(args[i+1]))
	}
	return builder.String()
}

func (s *gatewaySession) identityLogAttrs() []any {
	s.identityMu.RLock()
	defer s.identityMu.RUnlock()
	return []any{
		"robot_id", printable(s.robotID),
		"client_type", printable(s.clientType),
		"bot_id", printable(s.botID),
	}
}

func (s *gatewaySession) logAttrs(args ...any) []any {
	attrs := []any{"session", s.id}
	attrs = append(attrs, s.connInfo.logAttrs()...)
	attrs = append(attrs, s.identityLogAttrs()...)
	attrs = append(attrs, args...)
	return attrs
}

func (s *gatewaySession) logInfo(message string, args ...any) {
	if s == nil || s.server == nil {
		return
	}
	s.server.logInfo(message, s.logAttrs(args...)...)
}

func (s *gatewaySession) logWarn(message string, args ...any) {
	if s == nil || s.server == nil {
		return
	}
	s.server.logWarn(message, s.logAttrs(args...)...)
}

func (s *gatewaySession) shouldProcessClientEvent(msg clientMessage, transport string) bool {
	key := clientEventDedupeKey(msg)
	if key == "" {
		return true
	}
	now := time.Now()
	s.clientEventMu.Lock()
	defer s.clientEventMu.Unlock()
	if s.recentClientEvents == nil {
		s.recentClientEvents = make(map[string]time.Time)
	}
	for existingKey, seenAt := range s.recentClientEvents {
		if now.Sub(seenAt) > clientEventDedupeWindow {
			delete(s.recentClientEvents, existingKey)
		}
	}
	if seenAt, ok := s.recentClientEvents[key]; ok && now.Sub(seenAt) <= clientEventDedupeWindow {
		s.logInfo(
			"client_event_duplicate_dropped",
			"transport", printable(transport),
			"event", printable(msg.Event),
			"event_id", printable(msg.EventID),
		)
		return false
	}
	if len(s.recentClientEvents) >= maxRecentClientEventDedupes {
		oldestKey := ""
		var oldestAt time.Time
		for existingKey, seenAt := range s.recentClientEvents {
			if oldestKey == "" || seenAt.Before(oldestAt) {
				oldestKey = existingKey
				oldestAt = seenAt
			}
		}
		delete(s.recentClientEvents, oldestKey)
	}
	s.recentClientEvents[key] = now
	return true
}

func clientEventDedupeKey(msg clientMessage) string {
	if eventID := strings.TrimSpace(msg.EventID); eventID != "" {
		return "event_id:" + eventID
	}
	event := strings.TrimSpace(msg.Event)
	source := strings.TrimSpace(msg.Source)
	botID := strings.TrimSpace(msg.BotID)
	if event == "" && source == "" && botID == "" {
		return ""
	}
	return strings.Join([]string{"event", event, source, botID}, "\x00")
}

func (s *gatewaySession) logError(message string, args ...any) {
	if s == nil || s.server == nil {
		return
	}
	s.server.logError(message, s.logAttrs(args...)...)
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/internal/status", s.handleStatus)
	mux.HandleFunc("/internal/vision/snapshot", s.handleVisionSnapshot)
	mux.Handle(s.cfg.WebSocketPath, websocket.Server{
		Handshake: func(_ *websocket.Config, req *http.Request) error {
			if !isOriginAllowed(req, s.cfg.AllowedOrigins) {
				return errors.New("origin is not allowed")
			}
			return nil
		},
		Handler: websocket.Handler(s.handleWebSocketConn),
	})
	return mux
}

func (s *Server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":             "ok",
		"service":            "go_voice_gateway",
		"uptime_ms":          time.Since(s.startedAt).Milliseconds(),
		"active_sessions":    s.sessions.Count(),
		"rtc_enabled":        s.cfg.RTCEnabled,
		"asr_processor":      s.cfg.ASR.Processor,
		"downlink_transport": s.cfg.Audio.DownlinkTransport,
	})
}

func (s *Server) handleStatus(w http.ResponseWriter, _ *http.Request) {
	printableConfig := s.cfg.Printable()
	status := map[string]any{
		"service":             "go_voice_gateway",
		"started_at":          s.startedAt.Format(time.RFC3339Nano),
		"uptime_ms":           time.Since(s.startedAt).Milliseconds(),
		"addr":                s.cfg.Addr,
		"ws_path":             s.cfg.WebSocketPath,
		"rtc_enabled":         s.cfg.RTCEnabled,
		"ice_servers":         len(s.cfg.ICEServers),
		"ice_policy":          s.cfg.ICETransportPolicy.String(),
		"ice_networks":        iceNetworkTypeStrings(s.cfg.ICENetworkTypes),
		"rtc_udp_min_port":    s.cfg.RTCUDPMinPort,
		"rtc_udp_max_port":    s.cfg.RTCUDPMaxPort,
		"audio":               printableConfig.Audio,
		"asr":                 printableConfig.ASR,
		"internal_voice":      printableConfig.InternalVoice,
		"device_auth":         printableConfig.DeviceAuth,
		"interface":           printable(s.cfg.LocalInterfaceName),
		"ip":                  printable(s.cfg.LocalIP),
		"allowed_origins":     printableConfig.AllowedOrigins,
		"message_limit_bytes": s.cfg.MessageLimitBytes,
		"sessions":            s.sessions.Snapshot(time.Now(), 16),
	}
	if provider, ok := s.voiceEvents.(voiceEventStatusProvider); ok {
		status["voice_events"] = provider.VoiceEventStatus()
	}
	if provider, ok := s.clientEvents.(bridgeHealthStatusProvider); ok {
		status["python_gateway_bridge"] = provider.BridgeHealthStatus()
	}
	if s.internalVoice != nil {
		status["internal_voice_protocol"] = s.internalVoice.Status()
	}
	writeJSON(w, http.StatusOK, status)
}

func (s *Server) handleWebSocketConn(conn *websocket.Conn) {
	defer conn.Close()
	conn.MaxPayloadBytes = int(s.cfg.MessageLimitBytes)
	req := conn.Request()
	remoteAddr := ""
	if req != nil {
		remoteAddr = req.RemoteAddr
	}
	connInfo := connectionInfoFromRequest(remoteAddr, req)

	session := &gatewaySession{
		id:            newSessionID(),
		server:        s,
		conn:          conn,
		connInfo:      connInfo,
		createdAt:     time.Now(),
		downlinkQueue: make(chan queuedPythonGatewayDownlink, downlinkQueueCapacity),
		downlinkDone:  make(chan struct{}),
	}
	session.startDownlinkQueueWorker()
	s.sessions.Add(session)
	defer s.sessions.Remove(session.id, session)
	defer session.close()

	if err := session.sendJSON(newConnectedMessage(session.id)); err != nil {
		session.logError("ws_connected_send_failed", "error", err)
		return
	}
	session.logInfo(
		"ws_connected",
		"path", requestPath(req),
		"message_limit_bytes", s.cfg.MessageLimitBytes,
	)

	for {
		var msg clientMessage
		if err := websocket.JSON.Receive(conn, &msg); err != nil {
			if !errors.Is(err, io.EOF) {
				session.logWarn("ws_closed_read_error", "error", err)
			} else {
				session.logInfo("ws_closed")
			}
			return
		}
		if err := session.handleClientMessage(context.Background(), msg); err != nil {
			session.logWarn("client_message_failed", "type", printable(msg.Type), "error", err)
			code := "SIGNALING_ERROR"
			if msg.Type == "register" {
				code = "REGISTER_FAILED"
			}
			if sendErr := session.sendJSON(newErrorMessage(code, err.Error())); sendErr != nil {
				return
			}
		}
	}
}

func (s *gatewaySession) handleClientMessage(parent context.Context, msg clientMessage) error {
	switch msg.Type {
	case "register":
		registration, err := s.server.deviceAuth.AuthenticateRegister(parent, msg)
		if err != nil {
			return err
		}
		s.storeRegistrationCredentials(msg, registration)
		if err := s.sendJSON(newRegisteredMessageFromRegistration(s.id, registration)); err != nil {
			return err
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		s.logInfo(
			"client_registered",
			"auth_backend", printable(s.server.cfg.DeviceAuth.Backend),
			"rtc_enabled", s.server.cfg.RTCEnabled,
			"downlink_transport", s.server.cfg.Audio.DownlinkTransport,
			"asr_processor", printable(s.server.cfg.ASR.Processor),
		)
		if s.server.cfg.RTCEnabled {
			if err := s.sendJSON(newRTCConfigMessage(s.id, s.server.cfg)); err != nil {
				return err
			}
		}
		s.openInternalVoiceSession(parent, registration)
		return nil
	case "rtc_offer":
		if err := s.ensureMessageSession(msg.SessionID); err != nil {
			return err
		}
		if strings.TrimSpace(msg.SDP) == "" {
			return errors.New("rtc_offer missing sdp")
		}
		s.logInfo(
			"rtc_offer_received",
			"sdp_bytes", len(msg.SDP),
			"ice_servers", len(s.server.cfg.ICEServers),
			"ice_policy", s.server.cfg.ICETransportPolicy.String(),
			"ice_networks", strings.Join(iceNetworkTypeStrings(s.server.cfg.ICENetworkTypes), ","),
			"downlink_transport", s.server.cfg.Audio.DownlinkTransport,
		)
		ctx, cancel := context.WithTimeout(parent, s.server.cfg.NegotiationTimeout)
		defer cancel()
		answer, err := s.createAnswer(ctx, msg.SDP)
		if err != nil {
			return err
		}
		if err := s.sendJSON(newRTCAnswerMessage(s.id, answer.sdp)); err != nil {
			return err
		}
		for _, candidate := range answer.candidates {
			if err := s.sendJSON(candidate); err != nil {
				return err
			}
		}
		return nil
	case "rtc_ice_candidate":
		if err := s.ensureMessageSession(msg.SessionID); err != nil {
			return err
		}
		return s.addRemoteCandidate(msg)
	case "transport_ready":
		if err := s.ensureMessageSession(msg.SessionID); err != nil {
			return err
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		s.logInfo("transport_ready", "transport", canonicalTransportWebSocket, "active_transport", printable(msg.ActiveTransport))
		return nil
	case "client_event":
		if msg.SessionID != "" {
			if err := s.ensureMessageSession(msg.SessionID); err != nil {
				return err
			}
		}
		if !s.shouldProcessClientEvent(msg, canonicalTransportWebSocket) {
			return nil
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		s.logInfo(
			"client_event",
			"transport", canonicalTransportWebSocket,
			"event", printable(msg.Event),
			"source", printable(msg.Source),
			"event_id", printable(msg.EventID),
		)
		s.cancelDownlinkRTP("client_event:" + firstNonEmpty(strings.TrimSpace(msg.Event), "unknown"))
		s.dispatchClientEvent(msg, canonicalTransportWebSocket)
		return nil
	case "text":
		if msg.SessionID != "" {
			if err := s.ensureMessageSession(msg.SessionID); err != nil {
				return err
			}
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		s.logInfo(
			"text",
			"transport", canonicalTransportWebSocket,
			"source", printable(msg.Source),
			"content_chars", len([]rune(strings.TrimSpace(msg.Content))),
		)
		textProcessor, ok := s.server.clientEvents.(ClientTextProcessor)
		if !ok || textProcessor == nil {
			return fmt.Errorf("text bridge unavailable")
		}
		msgCopy := msg
		go func() {
			if err := textProcessor.ProcessClientText(s.id, msgCopy); err != nil {
				s.logWarn("text_bridge_failed", "source", printable(msgCopy.Source), "error", err)
			}
		}()
		return nil
	case "transport_fallback_start":
		if err := s.ensureMessageSession(msg.SessionID); err != nil {
			return err
		}
		activeTransport := msg.ToTransport
		if activeTransport == "" {
			activeTransport = "websocket"
		}
		reason := msg.Reason
		if reason == "" {
			reason = "client_requested_fallback"
		}
		eventMsg := msg
		eventMsg.ToTransport = activeTransport
		eventMsg.Reason = reason
		s.recordCanonicalEvent(canonicalEventFromClientMessage(eventMsg, canonicalTransportWebSocket))
		return s.sendJSON(newFallbackAckMessage(s.id, activeTransport, reason))
	case "heartbeat":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		return s.sendJSON(simpleTypeMessage{Type: "heartbeat_ack"})
	case "ping":
		return s.sendJSON(simpleTypeMessage{Type: "pong"})
	case "audio_start", "audio_end", "audio_cancel", "turn_candidate", "turn_candidate_cancel", "turn_commit_ack", "barge_in_start", "barge_in_probe", "barge_in_cancel", "barge_in_commit_ack", "playback_complete", "playback_interrupted":
		if msg.SessionID != "" {
			if err := s.ensureMessageSession(msg.SessionID); err != nil {
				return err
			}
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		switch msg.Type {
		case "audio_start":
			s.logInfo("audio_start", "transport", canonicalTransportWebSocket, "utterance_id", printable(msg.UtteranceID))
		case "audio_end":
			s.logInfo("audio_end", "transport", canonicalTransportWebSocket, "utterance_id", printable(msg.UtteranceID))
		case "turn_candidate":
			s.logInfo(
				"turn_candidate",
				"transport", canonicalTransportWebSocket,
				"utterance_id", printable(msg.UtteranceID),
				"candidate_seq", msg.CandidateSeq,
				"speech_epoch", msg.SpeechEpoch,
				"audio_watermark", msg.AudioWatermark,
				"silence_ms", msg.SilenceMS,
				"shadow", msg.Shadow,
			)
		case "turn_candidate_cancel":
			s.logInfo(
				"turn_candidate_cancel",
				"transport", canonicalTransportWebSocket,
				"utterance_id", printable(msg.UtteranceID),
				"candidate_seq", msg.CandidateSeq,
				"speech_epoch", msg.SpeechEpoch,
				"reason", printable(msg.Reason),
			)
		case "turn_commit_ack":
			s.logInfo(
				"turn_commit_ack",
				"transport", canonicalTransportWebSocket,
				"utterance_id", printable(msg.UtteranceID),
				"candidate_seq", msg.CandidateSeq,
				"speech_epoch", msg.SpeechEpoch,
				"accepted", msg.Accepted,
				"reason", printable(msg.Reason),
			)
		case "barge_in_start", "barge_in_probe", "barge_in_cancel", "barge_in_commit_ack":
			s.logInfo(
				msg.Type,
				"transport", canonicalTransportWebSocket,
				"utterance_id", printable(msg.UtteranceID),
				"round_id", printable(msg.RoundID),
				"playback_id", printable(msg.PlaybackID),
				"candidate_seq", msg.CandidateSeq,
				"speech_epoch", msg.SpeechEpoch,
				"accepted", msg.Accepted,
			)
		case "playback_complete":
			s.logInfo(
				"playback_complete",
				"transport", canonicalTransportWebSocket,
				"round_id", printable(msg.RoundID),
				"playback_id", printable(msg.PlaybackID),
			)
			if s.internalVoiceOwnsUserAudio() {
				s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebSocket)
			} else {
				s.forwardPlaybackReport(msg)
				s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebSocket)
			}
		case "playback_interrupted":
			s.logInfo(
				"playback_interrupted",
				"transport", canonicalTransportWebSocket,
				"round_id", printable(msg.RoundID),
				"playback_id", printable(msg.PlaybackID),
				"reason", printable(msg.Reason),
			)
			if s.internalVoiceOwnsUserAudio() {
				s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebSocket)
			} else {
				s.forwardPlaybackReport(msg)
				s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebSocket)
			}
			s.cancelDownlinkRTPForPlayback(
				msg.Type,
				msg.RoundID,
				msg.PlaybackID,
			)
		case "audio_cancel":
			s.cancelDownlinkRTP(msg.Type)
			if s.internalVoiceOwnsUserAudio() {
				s.forwardInternalVoiceInputAudioMetadata(msg, canonicalTransportWebSocket)
			} else {
				s.forwardClientControl(msg)
				s.forwardInternalVoiceInputAudioMetadata(msg, canonicalTransportWebSocket)
			}
		}
		return nil
	case "interrupt":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebSocket))
		s.logInfo("client_interrupt", "transport", canonicalTransportWebSocket)
		_, roundID, playbackID := s.cancelDownlinkRTP("interrupt")
		if roundID != "" && playbackID != "" {
			if err := s.sendJSON(playbackCancelMessage{
				Type: "playback_cancel", RoundID: roundID, PlaybackID: playbackID, Reason: "client_interrupt",
			}); err != nil {
				return err
			}
		}
		if s.internalVoiceOwnsUserAudio() {
			if shouldForwardClientInterruptToPrimary(msg.Reason) {
				s.forwardActiveClientControl(msg)
			}
			s.forwardInternalVoiceInterrupt(msg, canonicalTransportWebSocket)
		} else {
			if shouldForwardClientInterruptToPrimary(msg.Reason) {
				s.forwardClientControl(msg)
			}
			s.forwardInternalVoiceInterrupt(msg, canonicalTransportWebSocket)
		}
		return nil
	default:
		return fmt.Errorf("unknown message type: %s", msg.Type)
	}
}

func (s *gatewaySession) storeRegistrationCredentials(msg clientMessage, registration deviceRegistration) {
	robotSecret := ""
	if msg.RobotSecret != nil {
		robotSecret = strings.TrimSpace(*msg.RobotSecret)
	}
	s.identityMu.Lock()
	s.robotID = strings.TrimSpace(registration.RobotID)
	s.robotSecret = robotSecret
	s.clientType = strings.TrimSpace(registration.ClientType)
	s.botID = strings.TrimSpace(registration.BotID)
	s.identityMu.Unlock()
}

func (s *gatewaySession) openInternalVoiceSession(parent context.Context, registration deviceRegistration) *InternalVoiceSession {
	return s.openInternalVoiceSessionWithRegistration(parent, registration, "register")
}

func (s *gatewaySession) ensureInternalVoiceSession(parent context.Context, reason string) *InternalVoiceSession {
	if s == nil || s.server == nil || s.server.internalVoice == nil {
		return nil
	}
	s.internalVoiceMu.Lock()
	existing := s.internalVoice
	s.internalVoiceMu.Unlock()
	if existing != nil {
		return existing
	}
	s.identityMu.RLock()
	registration := deviceRegistration{
		RobotID:    strings.TrimSpace(s.robotID),
		ClientType: strings.TrimSpace(s.clientType),
		BotID:      strings.TrimSpace(s.botID),
	}
	s.identityMu.RUnlock()
	if registration.RobotID == "" {
		s.logWarn("internal_voice_session_open_skipped", "open_reason", printable(reason), "reason", "missing_registration")
		return nil
	}
	return s.openInternalVoiceSessionWithRegistration(parent, registration, reason)
}

func boundedInternalVoiceLazyOpenTimeout(timeout time.Duration) time.Duration {
	if timeout <= 0 || timeout > maxInternalVoiceLazyOpen {
		return maxInternalVoiceLazyOpen
	}
	return timeout
}

func (s *gatewaySession) openInternalVoiceSessionWithRegistration(parent context.Context, registration deviceRegistration, reason string) *InternalVoiceSession {
	if s == nil || s.server == nil || s.server.internalVoice == nil {
		return nil
	}
	openReason := strings.TrimSpace(reason)
	if openReason == "" {
		openReason = "internal_voice"
	}
	s.internalVoiceOpenMu.Lock()
	defer s.internalVoiceOpenMu.Unlock()
	s.internalVoiceMu.Lock()
	existing := s.internalVoice
	s.internalVoiceMu.Unlock()
	if existing != nil {
		return existing
	}
	s.identityMu.RLock()
	robotSecret := strings.TrimSpace(s.robotSecret)
	s.identityMu.RUnlock()
	session, err := s.server.internalVoice.OpenSession(parent, InternalVoiceSessionOpenRequest{
		SessionID:   s.id,
		RobotID:     registration.RobotID,
		RobotSecret: robotSecret,
		ClientType:  registration.ClientType,
		BotID:       registration.BotID,
		Source:      "go_voice_gateway",
		Transport:   "webrtc",
		BridgeMode:  firstNonEmpty(s.server.cfg.InternalVoice.Mode, "m1") + "_" + openReason,
		RequestedAt: time.Now(),
	})
	if err != nil {
		s.logWarn("internal_voice_session_open_failed", "open_reason", printable(openReason), "url", printable(s.server.internalVoice.cfg.WSURL), "error", err)
		return nil
	}
	s.internalVoiceMu.Lock()
	old := s.internalVoice
	s.internalVoice = session
	s.internalVoiceMu.Unlock()
	if old != nil {
		_ = old.Close(context.Background())
	}
	s.logInfo("internal_voice_session_opened", "open_reason", printable(openReason), "url", printable(s.server.internalVoice.cfg.WSURL))
	return session
}

func (s *gatewaySession) dispatchClientEvent(msg clientMessage, transport string) {
	if strings.EqualFold(strings.TrimSpace(msg.Event), "wake_interrupt") {
		// wake_interrupt 不只是客户端状态事件，也代表旧响应必须立即停止。
		// 显式转发到主 Python Gateway；其 session fast path 会优先命中仍在
		// 推歌的连接，避免事件被 M1 消费后 M0 歌曲继续占住请求队列。
		s.forwardClientControl(clientMessage{
			Type:        "interrupt",
			SessionID:   msg.SessionID,
			UtteranceID: msg.UtteranceID,
			TraceID:     msg.TraceID,
			RoundID:     msg.RoundID,
			PlaybackID:  msg.PlaybackID,
			Reason:      "wake_interrupt",
		})
	}
	if s.forwardInternalVoiceClientEventActive(msg, transport) {
		return
	}
	s.forwardPythonGatewayClientEvent(msg)
	s.forwardInternalVoiceClientEvent(msg, transport)
}

func shouldFallbackInternalVoiceClientEventActive(summary pythonGatewayBridgeSummary) bool {
	return !summary.RequestCommitted
}

func (s *gatewaySession) resetInternalVoiceSession(reason string) bool {
	if s == nil {
		return false
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoice = nil
	s.internalVoiceMu.Unlock()
	if session == nil {
		return false
	}
	s.logWarn("internal_voice_session_reset", "reason", printable(reason))
	go func() {
		ctx := context.Background()
		if s.server != nil && s.server.cfg.InternalVoice.Timeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
			defer cancel()
		}
		if err := session.Close(ctx); err != nil {
			s.logWarn("internal_voice_session_reset_close_failed", "reason", printable(reason), "error", err)
		}
	}()
	return true
}

func (s *gatewaySession) retryInternalVoiceClientEventActive(
	msg clientMessage,
	transport string,
	previousSummary pythonGatewayBridgeSummary,
	previousErr error,
) (pythonGatewayBridgeSummary, error, bool) {
	if !shouldFallbackInternalVoiceClientEventActive(previousSummary) {
		return previousSummary, previousErr, false
	}
	s.logWarn(
		"internal_voice_client_event_active_retrying",
		"event", printable(msg.Event),
		"event_id", printable(msg.EventID),
		"text_messages", previousSummary.TextMessages,
		"binary_messages", previousSummary.BinaryMessages,
		"last_type", printable(previousSummary.LastType),
		"last_message_ms", previousSummary.LastMessageMS,
		"previous_error", previousErr,
	)
	s.resetInternalVoiceSession("client_event_active_retry")
	lazyOpenCtx, cancel := context.WithTimeout(context.Background(), boundedInternalVoiceLazyOpenTimeout(s.server.cfg.InternalVoice.Timeout))
	session := s.ensureInternalVoiceSession(lazyOpenCtx, "client_event_active_retry")
	cancel()
	if session == nil {
		return previousSummary, fmt.Errorf("internal voice active retry session open failed"), true
	}
	ctx := context.Background()
	if s.server.cfg.InternalVoice.ClientEventTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.ClientEventTimeout)
		defer cancel()
	}
	summary, err := session.SendClientEventActive(ctx, msg, transport, s.server.sessions)
	return summary, err, true
}

func (s *gatewaySession) forwardPythonGatewayClientEvent(msg clientMessage) {
	if s == nil || s.server == nil || s.server.clientEvents == nil {
		return
	}
	msgCopy := msg
	go func() {
		if err := s.server.clientEvents.ProcessClientEvent(s.id, msgCopy); err != nil {
			s.logWarn("client_event_bridge_failed", "event", printable(msgCopy.Event), "error", err)
		}
	}()
}

func (s *gatewaySession) forwardInternalVoiceClientEventActive(msg clientMessage, transport string) bool {
	if s == nil || s.server == nil || s.server.internalVoice == nil {
		return false
	}
	if !s.server.cfg.InternalVoice.ClientEventEnabled || s.server.cfg.InternalVoice.ClientEventMode != "active" {
		return false
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoiceMu.Unlock()
	if session == nil {
		lazyOpenCtx, cancel := context.WithTimeout(context.Background(), boundedInternalVoiceLazyOpenTimeout(s.server.cfg.InternalVoice.Timeout))
		session = s.ensureInternalVoiceSession(lazyOpenCtx, "client_event_active")
		cancel()
		if session == nil {
			s.logWarn(
				"internal_voice_client_event_active_skipped",
				"event", printable(msg.Event),
				"event_id", printable(msg.EventID),
				"reason", "session_not_open",
			)
			return false
		}
	}
	msgCopy := msg
	go func() {
		ctx := context.Background()
		if s.server.cfg.InternalVoice.ClientEventTimeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.ClientEventTimeout)
			defer cancel()
		}
		summary, err := session.SendClientEventActive(ctx, msgCopy, transport, s.server.sessions)
		if shouldFallbackInternalVoiceClientEventActive(summary) {
			var retried bool
			summary, err, retried = s.retryInternalVoiceClientEventActive(msgCopy, transport, summary, err)
			if retried && shouldFallbackInternalVoiceClientEventActive(summary) {
				s.resetInternalVoiceSession("client_event_active_retry_failed")
			}
		}
		if err != nil {
			s.resetInternalVoiceSession("client_event_active_failed")
			s.logWarn(
				"internal_voice_client_event_active_failed",
				"event", printable(msgCopy.Event),
				"event_id", printable(msgCopy.EventID),
				"text_messages", summary.TextMessages,
				"binary_messages", summary.BinaryMessages,
				"binary_bytes", summary.BinaryBytes,
				"last_type", printable(summary.LastType),
				"last_message_ms", summary.LastMessageMS,
				"error", err,
			)
			if shouldFallbackInternalVoiceClientEventActive(summary) {
				s.logWarn(
					"internal_voice_client_event_active_fallback_to_m0",
					"event", printable(msgCopy.Event),
					"event_id", printable(msgCopy.EventID),
					"text_messages", summary.TextMessages,
					"last_type", printable(summary.LastType),
					"last_message_ms", summary.LastMessageMS,
				)
				s.forwardPythonGatewayClientEvent(msgCopy)
			}
			return
		}
		s.logInfo(
			"internal_voice_client_event_active_done",
			"event", printable(msgCopy.Event),
			"event_id", printable(msgCopy.EventID),
			"text_messages", summary.TextMessages,
			"binary_messages", summary.BinaryMessages,
			"binary_bytes", summary.BinaryBytes,
			"first_text_ms", summary.FirstTextMS,
			"first_binary_ms", summary.FirstBinaryMS,
			"last_message_ms", summary.LastMessageMS,
		)
		if shouldFallbackInternalVoiceClientEventActive(summary) {
			s.logWarn(
				"internal_voice_client_event_active_fallback_to_m0",
				"event", printable(msgCopy.Event),
				"event_id", printable(msgCopy.EventID),
				"text_messages", summary.TextMessages,
				"last_type", printable(summary.LastType),
				"last_message_ms", summary.LastMessageMS,
			)
			s.forwardPythonGatewayClientEvent(msgCopy)
		}
	}()
	return true
}

func (s *gatewaySession) forwardInternalVoiceClientEvent(msg clientMessage, transport string) {
	if s == nil || s.server == nil || s.server.internalVoice == nil || !s.server.cfg.InternalVoice.ClientEventEnabled {
		return
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoiceMu.Unlock()
	if session == nil {
		s.logWarn(
			"internal_voice_client_event_skipped",
			"event", printable(msg.Event),
			"event_id", printable(msg.EventID),
			"reason", "session_not_open",
		)
		return
	}
	msgCopy := msg
	go func() {
		ctx := context.Background()
		if s.server.cfg.InternalVoice.Timeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
			defer cancel()
		}
		if err := session.SendClientEvent(ctx, msgCopy, transport); err != nil {
			s.logWarn(
				"internal_voice_client_event_failed",
				"event", printable(msgCopy.Event),
				"event_id", printable(msgCopy.EventID),
				"error", err,
			)
		}
	}()
}

func (s *gatewaySession) forwardInternalVoiceInputAudioMetadata(msg clientMessage, transport string) {
	if s == nil || s.server == nil || s.server.internalVoice == nil || !s.server.cfg.InternalVoice.InputAudioEnabled {
		return
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoiceMu.Unlock()
	if session == nil {
		s.logWarn(
			"internal_voice_input_audio_skipped",
			"type", printable(msg.Type),
			"utterance_id", printable(msg.UtteranceID),
			"reason", "session_not_open",
		)
		return
	}
	msgCopy := msg
	go func() {
		ctx := context.Background()
		if s.server.cfg.InternalVoice.Timeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
			defer cancel()
		}
		if err := session.SendInputAudioMetadata(ctx, msgCopy, transport, s.server.cfg.Audio); err != nil {
			s.logWarn(
				"internal_voice_input_audio_failed",
				"type", printable(msgCopy.Type),
				"utterance_id", printable(msgCopy.UtteranceID),
				"error", err,
			)
		}
	}()
}

func (s *gatewaySession) internalVoiceOwnsUserAudio() bool {
	return s != nil &&
		s.server != nil &&
		s.server.cfg.InternalVoice.Enabled &&
		s.server.cfg.InternalVoice.InputAudioEnabled &&
		s.server.cfg.InternalVoice.InputAudioMode == "active"
}

func (s *gatewaySession) forwardInternalVoiceInterrupt(msg clientMessage, transport string) {
	if s == nil || s.server == nil || s.server.internalVoice == nil ||
		(!s.internalVoiceOwnsUserAudio() && !s.server.cfg.InternalVoice.InterruptEnabled) {
		return
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoiceMu.Unlock()
	if session == nil {
		s.logWarn(
			"internal_voice_interrupt_skipped",
			"utterance_id", printable(msg.UtteranceID),
			"reason", "session_not_open",
		)
		return
	}
	ctx := context.Background()
	if s.server.cfg.InternalVoice.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
		defer cancel()
	}
	if err := session.SendInterrupt(ctx, msg, transport); err != nil {
		s.logWarn(
			"internal_voice_interrupt_failed",
			"utterance_id", printable(msg.UtteranceID),
			"reason", printable(msg.Reason),
			"error", err,
		)
	}
}

func (s *gatewaySession) forwardInternalVoicePlaybackReport(msg clientMessage, transport string) {
	if s == nil || s.server == nil || s.server.internalVoice == nil ||
		(!s.internalVoiceOwnsUserAudio() && !s.server.cfg.InternalVoice.PlaybackReportEnabled) {
		return
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoiceMu.Unlock()
	if session == nil {
		s.logWarn(
			"internal_voice_playback_report_skipped",
			"type", printable(msg.Type),
			"round_id", printable(msg.RoundID),
			"playback_id", printable(msg.PlaybackID),
			"reason", "session_not_open",
		)
		return
	}
	msgCopy := msg
	go func() {
		ctx := context.Background()
		if s.server.cfg.InternalVoice.Timeout > 0 {
			var cancel context.CancelFunc
			ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
			defer cancel()
		}
		if err := session.SendPlaybackReport(ctx, msgCopy, transport); err != nil {
			s.logWarn(
				"internal_voice_playback_report_failed",
				"type", printable(msgCopy.Type),
				"round_id", printable(msgCopy.RoundID),
				"playback_id", printable(msgCopy.PlaybackID),
				"error", err,
			)
		}
	}()
}

func (s *gatewaySession) pythonGatewayCredentials() (pythonGatewaySessionCredentials, bool) {
	s.identityMu.RLock()
	defer s.identityMu.RUnlock()
	if strings.TrimSpace(s.robotID) == "" || strings.TrimSpace(s.robotSecret) == "" {
		return pythonGatewaySessionCredentials{}, false
	}
	return pythonGatewaySessionCredentials{
		RobotID:     s.robotID,
		RobotSecret: s.robotSecret,
		ClientType:  s.clientType,
	}, true
}

func (s *gatewaySession) createAnswer(ctx context.Context, offerSDP string) (rtcAnswerResult, error) {
	startedAt := time.Now()
	peerConfig := webrtc.Configuration{
		ICEServers:         s.server.cfg.pionICEServers(),
		ICETransportPolicy: s.server.cfg.ICETransportPolicy,
	}
	pc, err := s.server.api.NewPeerConnection(peerConfig)
	if err != nil {
		return rtcAnswerResult{}, err
	}
	downlinkTrack, err := s.addDownlinkRTPTrack(pc)
	if err != nil {
		return rtcAnswerResult{}, err
	}
	success := false
	defer func() {
		if !success {
			_ = pc.Close()
		}
	}()

	pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		s.logInfo("datachannel_created", "label", dc.Label())
		dc.OnOpen(func() {
			if dc.Label() == "control" {
				s.controlMu.Lock()
				s.controlDC = dc
				s.controlMu.Unlock()
			}
			s.logInfo(
				"datachannel_open",
				"label", dc.Label(),
				"since_offer_ms", time.Since(startedAt).Milliseconds(),
			)
		})
		dc.OnClose(func() {
			if dc.Label() == "control" {
				s.controlMu.Lock()
				if s.controlDC == dc {
					s.controlDC = nil
				}
				s.controlMu.Unlock()
			}
		})
		dc.OnMessage(func(message webrtc.DataChannelMessage) {
			if dc.Label() == visionDataChannelLabel {
				if message.IsString {
					s.markVisionDropped()
					ack := newVisionAck(0, "dropped", "text_not_supported", time.Now())
					if err := sendDataChannelJSON(dc, ack); err != nil {
						s.logWarn("vision_snapshot_ack_failed", "error", err)
					}
					return
				}
				s.handleVisionDataChannelBinary(dc, message.Data)
				return
			}
			if message.IsString {
				if err := s.handleDataChannelText(dc, string(message.Data)); err != nil {
					s.logWarn("datachannel_control_error", "label", dc.Label(), "error", err)
					if sendErr := sendDataChannelJSON(dc, newErrorMessage("DATA_CHANNEL_CONTROL_ERROR", err.Error())); sendErr != nil {
						s.logWarn("datachannel_error_send_failed", "label", dc.Label(), "error", sendErr)
					}
				}
				return
			}
			if err := dc.Send(message.Data); err != nil {
				s.logWarn("datachannel_binary_send_failed", "label", dc.Label(), "bytes", len(message.Data), "error", err)
			}
		})
	})
	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		codec := track.Codec().RTPCodecCapability
		s.recordCanonicalEvent(CanonicalVoiceEvent{
			Type:        canonicalEventRTPTrack,
			Transport:   canonicalTransportWebRTCRTP,
			TrackKind:   track.Kind().String(),
			TrackID:     track.ID(),
			StreamID:    track.StreamID(),
			Codec:       codec.MimeType,
			ClockRate:   codec.ClockRate,
			Channels:    codec.Channels,
			PayloadType: int(track.PayloadType()),
		})
		s.logInfo(
			"rtp_track_open",
			"kind", track.Kind().String(),
			"track_id", track.ID(),
			"stream_id", track.StreamID(),
			"codec", codec.MimeType,
			"clock_rate", codec.ClockRate,
			"channels", codec.Channels,
			"payload_type", track.PayloadType(),
		)
		go s.drainRemoteRTPTrack(track)
	})
	var localCandidatesMu sync.Mutex
	var localCandidates []rtcIceCandidateMessage
	var selectedPairLogged atomic.Bool
	pc.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		if candidate == nil {
			return
		}
		init := candidate.ToJSON()
		if strings.TrimSpace(init.Candidate) == "" {
			return
		}
		localCandidatesMu.Lock()
		localCandidates = append(
			localCandidates,
			newRTCIceCandidateMessage(s.id, init.Candidate, init.SDPMid, init.SDPMLineIndex),
		)
		localCandidatesMu.Unlock()
	})
	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		s.logInfo("ice_state_changed", "ice_state", state.String())
		switch state {
		case webrtc.ICEConnectionStateConnected, webrtc.ICEConnectionStateCompleted:
			if !selectedPairLogged.Load() {
				go s.logSelectedICECandidatePair(pc, state, startedAt, &selectedPairLogged)
			}
		}
	})

	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeOffer,
		SDP:  offerSDP,
	}); err != nil {
		return rtcAnswerResult{}, err
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		return rtcAnswerResult{}, err
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		return rtcAnswerResult{}, err
	}
	select {
	case <-gatherComplete:
	case <-ctx.Done():
		return rtcAnswerResult{}, fmt.Errorf("ICE gathering timeout: %w", ctx.Err())
	}
	local := pc.LocalDescription()
	if local == nil {
		return rtcAnswerResult{}, errors.New("local rtc answer unavailable")
	}
	localCandidatesMu.Lock()
	candidates := append([]rtcIceCandidateMessage(nil), localCandidates...)
	localCandidatesMu.Unlock()
	s.replacePeerConnection(pc)
	s.replaceDownlinkRTPTrack(downlinkTrack)
	success = true
	s.logInfo(
		"rtc_answer_ready",
		"sdp_bytes", len(local.SDP),
		"local_candidates", len(candidates),
		"duration_ms", time.Since(startedAt).Milliseconds(),
	)
	return rtcAnswerResult{sdp: local.SDP, candidates: candidates}, nil
}

func (s *gatewaySession) logSelectedICECandidatePair(pc *webrtc.PeerConnection, state webrtc.ICEConnectionState, startedAt time.Time, logged *atomic.Bool) {
	if pc == nil {
		return
	}
	const attempts = 5
	for attempt := 1; attempt <= attempts; attempt++ {
		if snapshot, ok := selectedICECandidateRouteFromStats(pc.GetStats()); ok {
			if logged != nil && !logged.CompareAndSwap(false, true) {
				return
			}
			attrs := []any{
				"ice_state", state.String(),
				"since_offer_ms", time.Since(startedAt).Milliseconds(),
				"attempt", attempt,
			}
			attrs = append(attrs, snapshot.logAttrs()...)
			s.logInfo("ice_selected_candidate_pair", attrs...)
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	s.logWarn(
		"ice_selected_candidate_pair_unavailable",
		"ice_state", state.String(),
		"since_offer_ms", time.Since(startedAt).Milliseconds(),
		"attempts", attempts,
	)
}

func (s *gatewaySession) addDownlinkRTPTrack(pc *webrtc.PeerConnection) (*webrtc.TrackLocalStaticRTP, error) {
	if !downlinkAudioTransportUsesRTP(s.server.cfg.Audio.DownlinkTransport) {
		return nil, nil
	}
	track, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{
			MimeType: webrtc.MimeTypeOpus,
		},
		downlinkAudioTrackID,
		downlinkAudioStreamID,
	)
	if err != nil {
		return nil, fmt.Errorf("create downlink RTP track: %w", err)
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		return nil, fmt.Errorf("add downlink RTP track: %w", err)
	}
	go s.drainDownlinkRTCP(sender)
	s.logInfo(
		"downlink_rtp_track_added",
		"track_id", downlinkAudioTrackID,
		"stream_id", downlinkAudioStreamID,
		"downlink_transport", s.server.cfg.Audio.DownlinkTransport,
	)
	return track, nil
}

func downlinkAudioTransportUsesRTP(transport string) bool {
	return transport == downlinkAudioTransportWebRTCRTP || transport == downlinkAudioTransportWebRTCMirror
}

func (s *gatewaySession) drainDownlinkRTCP(sender *webrtc.RTPSender) {
	buf := make([]byte, 1500)
	for {
		if _, _, err := sender.Read(buf); err != nil {
			return
		}
	}
}

func (s *gatewaySession) replaceDownlinkRTPTrack(track *webrtc.TrackLocalStaticRTP) {
	s.downBlocked.Store(false)
	s.downGen.Add(1)
	s.downMu.Lock()
	s.downlink = track
	s.downRTP = newDownlinkRTPSendState()
	s.downTraceID = ""
	s.downRoundID = ""
	s.downPlayID = ""
	s.downMu.Unlock()
}

func (s *gatewaySession) startDownlinkPlayback(traceID, roundID, playbackID, reason string) uint16 {
	s.downMu.Lock()
	s.downTraceID = strings.TrimSpace(traceID)
	s.downRoundID = strings.TrimSpace(roundID)
	s.downPlayID = strings.TrimSpace(playbackID)
	rtpStartSequence := s.downRTP.nextSequenceNumber
	generation := s.downGen.Add(1)
	wasBlocked := s.downBlocked.Swap(false)
	s.downMu.Unlock()
	s.logInfo(
		"downlink_rtp_resumed",
		"generation", generation,
		"reason", printable(reason),
		"was_blocked", wasBlocked,
		"trace_id", printable(traceID),
		"round_id", printable(roundID),
		"playback_id", printable(playbackID),
		"rtp_start_sequence", rtpStartSequence,
	)
	return rtpStartSequence
}

func (s *gatewaySession) cancelDownlinkRTP(reason string) (string, string, string) {
	s.downBlocked.Store(true)
	s.downMu.Lock()
	s.downBlocked.Store(true)
	generation := s.downGen.Add(1)
	traceID := s.downTraceID
	roundID := s.downRoundID
	playbackID := s.downPlayID
	s.downTraceID = ""
	s.downRoundID = ""
	s.downPlayID = ""
	s.downMu.Unlock()
	s.logInfo(
		"downlink_rtp_cancel_requested",
		"generation", generation,
		"reason", printable(reason),
		"trace_id", printable(traceID),
		"round_id", printable(roundID),
		"playback_id", printable(playbackID),
	)
	return traceID, roundID, playbackID
}

func (s *gatewaySession) cancelDownlinkRTPForPlayback(reason, roundID, playbackID string) bool {
	roundID = strings.TrimSpace(roundID)
	playbackID = strings.TrimSpace(playbackID)

	s.downMu.Lock()
	currentTraceID := s.downTraceID
	currentRoundID := s.downRoundID
	currentPlaybackID := s.downPlayID
	matches := (roundID != "" || playbackID != "") &&
		(roundID == "" || roundID == currentRoundID) &&
		(playbackID == "" || playbackID == currentPlaybackID)
	if !matches {
		s.downMu.Unlock()
		s.logInfo(
			"downlink_rtp_cancel_ignored_stale_playback",
			"reason", printable(reason),
			"report_round_id", printable(roundID),
			"report_playback_id", printable(playbackID),
			"current_trace_id", printable(currentTraceID),
			"current_round_id", printable(currentRoundID),
			"current_playback_id", printable(currentPlaybackID),
		)
		return false
	}

	s.downBlocked.Store(true)
	generation := s.downGen.Add(1)
	s.downTraceID = ""
	s.downRoundID = ""
	s.downPlayID = ""
	s.downMu.Unlock()
	s.logInfo(
		"downlink_rtp_cancel_requested",
		"generation", generation,
		"reason", printable(reason),
		"trace_id", printable(currentTraceID),
		"round_id", printable(currentRoundID),
		"playback_id", printable(currentPlaybackID),
	)
	return true
}

func (s *gatewaySession) sendPythonGatewayDownlinkRTP(payload []byte) error {
	if s.downBlocked.Load() {
		s.downMu.Lock()
		traceID := s.downTraceID
		roundID := s.downRoundID
		playbackID := s.downPlayID
		s.downMu.Unlock()
		s.logInfo(
			"downlink_rtp_drop_blocked",
			"trace_id", printable(traceID),
			"round_id", printable(roundID),
			"playback_id", printable(playbackID),
			"payload_bytes", len(payload),
			"reason", "cancelled",
		)
		return nil
	}
	startedAt := time.Now()
	frame, err := parsePythonGatewayDownlinkAudioFrame(payload)
	if err != nil {
		return err
	}
	generation := s.downGen.Load()

	s.downMu.Lock()
	defer s.downMu.Unlock()
	frameTraceID := firstNonEmpty(strings.TrimSpace(frame.Header.TraceID), s.downTraceID)
	if s.downBlocked.Load() {
		s.logInfo(
			"downlink_rtp_drop_blocked",
			"trace_id", printable(frameTraceID),
			"round_id", printable(frame.Header.RoundID),
			"playback_id", printable(frame.Header.PlaybackID),
			"payload_bytes", len(frame.Payload),
			"reason", "cancelled_after_parse",
		)
		return nil
	}
	if current := s.downGen.Load(); current != generation {
		s.logInfo(
			"downlink_rtp_drop_stale_frame",
			"generation", generation,
			"current_generation", current,
			"trace_id", printable(frameTraceID),
			"round_id", printable(frame.Header.RoundID),
			"playback_id", printable(frame.Header.PlaybackID),
			"payload_bytes", len(frame.Payload),
		)
		return nil
	}
	if !s.downlinkPlaybackMatchesLocked(frame.Header) {
		s.logInfo(
			"downlink_rtp_drop_stale_playback",
			"trace_id", printable(frameTraceID),
			"frame_round_id", printable(frame.Header.RoundID),
			"frame_playback_id", printable(frame.Header.PlaybackID),
			"current_round_id", printable(s.downRoundID),
			"current_playback_id", printable(s.downPlayID),
			"chunk_seq", frame.Header.ChunkSeq,
			"payload_bytes", len(frame.Payload),
		)
		return nil
	}
	if s.downlink == nil {
		return fmt.Errorf("downlink RTP track is not negotiated")
	}
	packets, err := s.downRTP.packetsFromPythonGatewayDownlink(frame)
	if err != nil {
		return err
	}
	frameDuration, err := downlinkOpusFrameDuration(frame.Header)
	if err != nil {
		return err
	}
	for i := range packets {
		if current := s.downGen.Load(); current != generation || s.downBlocked.Load() {
			s.logInfo(
				"downlink_rtp_stopped",
				"generation", generation,
				"current_generation", current,
				"sent_packets", i,
				"total_packets", len(packets),
				"blocked", s.downBlocked.Load(),
				"trace_id", printable(frameTraceID),
				"round_id", printable(frame.Header.RoundID),
				"playback_id", printable(frame.Header.PlaybackID),
				"reason", "cancelled",
			)
			return nil
		}
		if delay := s.downRTP.nextPacketSendDelay(frameDuration, time.Now()); delay > 0 {
			time.Sleep(delay)
		}
		if current := s.downGen.Load(); current != generation || s.downBlocked.Load() {
			s.logInfo(
				"downlink_rtp_stopped",
				"generation", generation,
				"current_generation", current,
				"sent_packets", i,
				"total_packets", len(packets),
				"blocked", s.downBlocked.Load(),
				"trace_id", printable(frameTraceID),
				"round_id", printable(frame.Header.RoundID),
				"playback_id", printable(frame.Header.PlaybackID),
				"reason", "cancelled_after_pacing",
			)
			return nil
		}
		if err := s.downlink.WriteRTP(&packets[i]); err != nil {
			return fmt.Errorf("write downlink RTP packet %d/%d: %w", i+1, len(packets), err)
		}
		if i == 0 {
			s.logInfo(
				"downlink_rtp_first_packet_sent",
				"packets", len(packets),
				"payload_bytes", len(frame.Payload),
				"trace_id", printable(frameTraceID),
				"utterance_id", printable(frame.Header.UtteranceID),
				"round_id", printable(frame.Header.RoundID),
				"playback_id", printable(frame.Header.PlaybackID),
				"chunk_seq", frame.Header.ChunkSeq,
				"since_chunk_start_ms", time.Since(startedAt).Milliseconds(),
				"rtp_pacing_frame_ms", frameDuration.Milliseconds(),
			)
		}
	}
	s.logInfo(
		"downlink_rtp_sent",
		"packets", len(packets),
		"payload_bytes", len(frame.Payload),
		"trace_id", printable(frameTraceID),
		"utterance_id", printable(frame.Header.UtteranceID),
		"round_id", printable(frame.Header.RoundID),
		"playback_id", printable(frame.Header.PlaybackID),
		"chunk_seq", frame.Header.ChunkSeq,
		"duration_ms", time.Since(startedAt).Milliseconds(),
		"rtp_pacing_frame_ms", frameDuration.Milliseconds(),
	)
	return nil
}

func (s *gatewaySession) startDownlinkQueueWorker() {
	if s == nil || s.downlinkQueue == nil || s.downlinkDone == nil {
		return
	}
	go func() {
		for {
			select {
			case <-s.downlinkDone:
				return
			case queued := <-s.downlinkQueue:
				message := queued.message
				if message.IsJSON && message.Type == "done" {
					if failure := s.takeDownlinkQueueFailure(queued.generation); failure != nil {
						if err := s.sendDownlinkQueueFailure(queued.generation); err != nil {
							s.logWarn(
								"downlink_queue_error_notify_failed",
								"error", err,
								"cause", failure,
							)
						}
						continue
					}
				}
				if err := s.forwardQueuedPythonGatewayDownlink(message); err != nil {
					if !message.IsJSON {
						s.recordDownlinkQueueFailure(queued.generation, err)
					}
					s.logWarn(
						"downlink_queue_forward_failed",
						"type", printable(message.Type),
						"json", message.IsJSON,
						"binary_bytes", len(message.Binary),
						"error", err,
					)
				}
			}
		}
	}()
}

func (s *gatewaySession) queuePythonGatewayDownlink(message pythonGatewayBridgeMessage) error {
	if s == nil || s.downlinkQueue == nil || s.downlinkDone == nil {
		return fmt.Errorf("downlink queue is not available")
	}
	queued := queuedPythonGatewayDownlink{
		message:    message,
		generation: s.downGen.Load(),
	}
	select {
	case <-s.downlinkDone:
		return fmt.Errorf("downlink queue is closed")
	case s.downlinkQueue <- queued:
		return nil
	default:
		s.logWarn(
			"downlink_queue_full",
			"type", printable(message.Type),
			"json", message.IsJSON,
			"binary_bytes", len(message.Binary),
			"capacity", cap(s.downlinkQueue),
		)
		select {
		case <-s.downlinkDone:
			return fmt.Errorf("downlink queue is closed")
		case s.downlinkQueue <- queued:
			return nil
		}
	}
}

func (s *gatewaySession) recordDownlinkQueueFailure(generation uint64, err error) {
	if s == nil || err == nil {
		return
	}
	s.downlinkFailureMu.Lock()
	defer s.downlinkFailureMu.Unlock()
	if s.downlinkFailureErr == nil || generation >= s.downlinkFailureGen {
		s.downlinkFailureGen = generation
		s.downlinkFailureErr = err
	}
}

func (s *gatewaySession) takeDownlinkQueueFailure(generation uint64) error {
	if s == nil {
		return nil
	}
	s.downlinkFailureMu.Lock()
	defer s.downlinkFailureMu.Unlock()
	if s.downlinkFailureErr == nil {
		return nil
	}
	if s.downlinkFailureGen < generation {
		s.downlinkFailureGen = 0
		s.downlinkFailureErr = nil
		return nil
	}
	if s.downlinkFailureGen != generation {
		return nil
	}
	err := s.downlinkFailureErr
	s.downlinkFailureGen = 0
	s.downlinkFailureErr = nil
	return err
}

func (s *gatewaySession) sendDownlinkQueueFailure(generation uint64) error {
	payload := map[string]any{
		"type":    "error",
		"code":    "DOWNLINK_AUDIO_FAILED",
		"message": "TTS audio downlink failed",
	}
	if s.downGen.Load() == generation {
		s.downMu.Lock()
		if s.downTraceID != "" {
			payload["trace_id"] = s.downTraceID
		}
		if s.downRoundID != "" {
			payload["round_id"] = s.downRoundID
		}
		if s.downPlayID != "" {
			payload["playback_id"] = s.downPlayID
		}
		s.downMu.Unlock()
	}
	return s.sendJSON(payload)
}

func (s *gatewaySession) forwardQueuedPythonGatewayDownlink(message pythonGatewayBridgeMessage) error {
	if message.IsJSON {
		if message.JSON == nil {
			return fmt.Errorf("queued JSON downlink payload is empty")
		}
		return s.sendJSON(message.JSON)
	}
	if len(message.Binary) == 0 {
		return fmt.Errorf("queued binary downlink payload is empty")
	}
	return s.sendPythonGatewayDownlinkRTP(message.Binary)
}

func (s *gatewaySession) shouldQueuePythonGatewayDownlink(message pythonGatewayBridgeMessage) bool {
	if s == nil || s.server == nil || s.server.cfg.Audio.DownlinkTransport != downlinkAudioTransportWebRTCRTP {
		return false
	}
	if !message.IsJSON {
		return true
	}
	return message.Type == "done"
}

func (s *gatewaySession) downlinkPlaybackMatchesLocked(header pythonGatewayAudioFrameHeader) bool {
	frameRoundID := strings.TrimSpace(header.RoundID)
	framePlaybackID := strings.TrimSpace(header.PlaybackID)
	currentRoundID := strings.TrimSpace(s.downRoundID)
	currentPlaybackID := strings.TrimSpace(s.downPlayID)
	if frameRoundID == "" && framePlaybackID == "" {
		return currentRoundID == "" && currentPlaybackID == ""
	}
	if currentRoundID != "" && frameRoundID != "" && frameRoundID != currentRoundID {
		return false
	}
	if currentPlaybackID != "" && framePlaybackID != "" && framePlaybackID != currentPlaybackID {
		return false
	}
	return true
}

func (s *gatewaySession) forwardPythonGatewayBinaryDownlink(payload []byte) error {
	switch s.server.cfg.Audio.DownlinkTransport {
	case downlinkAudioTransportWebRTCRTP:
		if s.downBlocked.Load() {
			s.logInfo(
				"downlink_rtp_drop_blocked",
				"payload_bytes", len(payload),
				"downlink_transport", s.server.cfg.Audio.DownlinkTransport,
			)
			return nil
		}
		return s.queuePythonGatewayDownlink(pythonGatewayBridgeMessage{Binary: payload})
	case downlinkAudioTransportWebRTCMirror:
		if s.downBlocked.Load() {
			s.logInfo(
				"downlink_rtp_drop_blocked",
				"payload_bytes", len(payload),
				"downlink_transport", s.server.cfg.Audio.DownlinkTransport,
			)
			return nil
		}
		if err := s.sendPythonGatewayDownlinkRTP(payload); err != nil {
			s.logWarn("downlink_rtp_mirror_failed", "error", err)
		}
		return s.sendBinary(payload)
	default:
		return s.sendBinary(payload)
	}
}

func (s *gatewaySession) handleDataChannelText(dc *webrtc.DataChannel, text string) error {
	text = strings.TrimSpace(text)
	if text == "" {
		return errors.New("empty datachannel control message")
	}
	if text == "ping" {
		return dc.SendText("pong:ping")
	}

	var msg clientMessage
	if err := json.Unmarshal([]byte(text), &msg); err != nil {
		return dc.SendText("pong:" + text)
	}
	if strings.TrimSpace(msg.Type) == "" {
		return errors.New("datachannel control message missing type")
	}
	return s.handleDataChannelClientMessage(dc, msg)
}

func (s *gatewaySession) handleDataChannelClientMessage(dc *webrtc.DataChannel, msg clientMessage) error {
	if msg.SessionID != "" {
		if err := s.ensureMessageSession(msg.SessionID); err != nil {
			return err
		}
	}

	switch msg.Type {
	case "register", "rtc_offer", "rtc_ice_candidate", "transport_fallback_start":
		return fmt.Errorf("message type %s must use websocket signaling", msg.Type)
	case "transport_ready":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo("transport_ready", "transport", canonicalTransportWebRTCControl, "active_transport", printable(msg.ActiveTransport))
		return nil
	case "heartbeat":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		return sendDataChannelJSON(dc, simpleTypeMessage{Type: "heartbeat_ack"})
	case "ping":
		return sendDataChannelJSON(dc, simpleTypeMessage{Type: "pong"})
	case "client_event":
		if !s.shouldProcessClientEvent(msg, canonicalTransportWebRTCControl) {
			return nil
		}
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"client_event",
			"transport", canonicalTransportWebRTCControl,
			"event", printable(msg.Event),
			"source", printable(msg.Source),
			"event_id", printable(msg.EventID),
		)
		s.cancelDownlinkRTP("client_event:" + firstNonEmpty(strings.TrimSpace(msg.Event), "unknown"))
		s.dispatchClientEvent(msg, canonicalTransportWebRTCControl)
		return nil
	case "audio_start":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo("audio_start", "transport", canonicalTransportWebRTCControl, "utterance_id", printable(msg.UtteranceID))
		return nil
	case "audio_end":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo("audio_end", "transport", canonicalTransportWebRTCControl, "utterance_id", printable(msg.UtteranceID))
		return nil
	case "turn_candidate":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"turn_candidate",
			"transport", canonicalTransportWebRTCControl,
			"utterance_id", printable(msg.UtteranceID),
			"candidate_seq", msg.CandidateSeq,
			"speech_epoch", msg.SpeechEpoch,
			"audio_watermark", msg.AudioWatermark,
			"silence_ms", msg.SilenceMS,
			"shadow", msg.Shadow,
		)
		return nil
	case "turn_candidate_cancel":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"turn_candidate_cancel",
			"transport", canonicalTransportWebRTCControl,
			"utterance_id", printable(msg.UtteranceID),
			"candidate_seq", msg.CandidateSeq,
			"speech_epoch", msg.SpeechEpoch,
			"reason", printable(msg.Reason),
		)
		return nil
	case "turn_commit_ack":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"turn_commit_ack",
			"transport", canonicalTransportWebRTCControl,
			"utterance_id", printable(msg.UtteranceID),
			"candidate_seq", msg.CandidateSeq,
			"speech_epoch", msg.SpeechEpoch,
			"accepted", msg.Accepted,
			"reason", printable(msg.Reason),
		)
		return nil
	case "barge_in_start", "barge_in_probe", "barge_in_cancel", "barge_in_commit_ack":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			msg.Type,
			"transport", canonicalTransportWebRTCControl,
			"utterance_id", printable(msg.UtteranceID),
			"round_id", printable(msg.RoundID),
			"playback_id", printable(msg.PlaybackID),
			"candidate_seq", msg.CandidateSeq,
			"speech_epoch", msg.SpeechEpoch,
			"accepted", msg.Accepted,
		)
		return nil
	case "audio_cancel":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"audio_cancel",
			"transport", canonicalTransportWebRTCControl,
			"utterance_id", printable(msg.UtteranceID),
			"reason", printable(msg.Reason),
		)
		s.cancelDownlinkRTP("audio_cancel")
		if s.internalVoiceOwnsUserAudio() {
			s.forwardInternalVoiceInputAudioMetadata(msg, canonicalTransportWebRTCControl)
		} else {
			s.forwardClientControl(msg)
			s.forwardInternalVoiceInputAudioMetadata(msg, canonicalTransportWebRTCControl)
		}
		return nil
	case "interrupt":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo("client_interrupt", "transport", canonicalTransportWebRTCControl)
		_, roundID, playbackID := s.cancelDownlinkRTP("interrupt")
		if roundID != "" && playbackID != "" {
			if err := s.sendJSON(playbackCancelMessage{
				Type: "playback_cancel", RoundID: roundID, PlaybackID: playbackID, Reason: "client_interrupt",
			}); err != nil {
				return err
			}
		}
		if s.internalVoiceOwnsUserAudio() {
			if shouldForwardClientInterruptToPrimary(msg.Reason) {
				s.forwardActiveClientControl(msg)
			}
			s.forwardInternalVoiceInterrupt(msg, canonicalTransportWebRTCControl)
		} else {
			if shouldForwardClientInterruptToPrimary(msg.Reason) {
				s.forwardClientControl(msg)
			}
			s.forwardInternalVoiceInterrupt(msg, canonicalTransportWebRTCControl)
		}
		return nil
	case "playback_complete":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"playback_complete",
			"transport", canonicalTransportWebRTCControl,
			"round_id", printable(msg.RoundID),
			"playback_id", printable(msg.PlaybackID),
		)
		if s.internalVoiceOwnsUserAudio() {
			s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebRTCControl)
		} else {
			s.forwardPlaybackReport(msg)
			s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebRTCControl)
		}
		return nil
	case "playback_interrupted":
		s.recordCanonicalEvent(canonicalEventFromClientMessage(msg, canonicalTransportWebRTCControl))
		s.logInfo(
			"playback_interrupted",
			"transport", canonicalTransportWebRTCControl,
			"round_id", printable(msg.RoundID),
			"playback_id", printable(msg.PlaybackID),
			"reason", printable(msg.Reason),
		)
		if s.internalVoiceOwnsUserAudio() {
			s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebRTCControl)
		} else {
			s.forwardPlaybackReport(msg)
			s.forwardInternalVoicePlaybackReport(msg, canonicalTransportWebRTCControl)
		}
		s.cancelDownlinkRTPForPlayback(
			"playback_interrupted",
			msg.RoundID,
			msg.PlaybackID,
		)
		return nil
	default:
		return fmt.Errorf("unknown datachannel control message type: %s", msg.Type)
	}
}

func (s *gatewaySession) forwardPlaybackReport(msg clientMessage) {
	if s == nil || s.server == nil || s.server.playbackReports == nil {
		return
	}
	msgCopy := msg
	go func() {
		if err := s.server.playbackReports.ProcessPlaybackReport(s.id, msgCopy); err != nil {
			s.logWarn(
				"playback_report_bridge_failed",
				"type", printable(msgCopy.Type),
				"round_id", printable(msgCopy.RoundID),
				"playback_id", printable(msgCopy.PlaybackID),
				"error", err,
			)
		}
	}()
}

func (s *gatewaySession) forwardClientControl(msg clientMessage) {
	if s == nil || s.server == nil || s.server.clientEvents == nil {
		return
	}
	controlProcessor, ok := s.server.clientEvents.(ClientControlProcessor)
	if !ok || controlProcessor == nil {
		return
	}
	msgCopy := msg
	go func() {
		if err := controlProcessor.ProcessClientControl(s.id, msgCopy); err != nil {
			s.logWarn(
				"client_control_bridge_failed",
				"type", printable(msgCopy.Type),
				"utterance_id", printable(msgCopy.UtteranceID),
				"reason", printable(msgCopy.Reason),
				"error", err,
			)
		}
	}()
}

func (s *gatewaySession) forwardActiveClientControl(msg clientMessage) {
	if s == nil || s.server == nil || s.server.clientEvents == nil {
		return
	}
	controlProcessor, ok := s.server.clientEvents.(ActiveClientControlProcessor)
	if !ok || controlProcessor == nil {
		return
	}
	msgCopy := msg
	go func() {
		sent, err := controlProcessor.ProcessClientControlDuringActive(s.id, msgCopy)
		if err != nil {
			s.logWarn(
				"active_client_control_bridge_failed",
				"type", printable(msgCopy.Type),
				"utterance_id", printable(msgCopy.UtteranceID),
				"reason", printable(msgCopy.Reason),
				"error", err,
			)
			return
		}
		if sent {
			s.logInfo(
				"active_client_control_forwarded",
				"type", printable(msgCopy.Type),
				"utterance_id", printable(msgCopy.UtteranceID),
				"reason", printable(msgCopy.Reason),
			)
		}
	}()
}

func sendDataChannelJSON(dc *webrtc.DataChannel, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return dc.SendText(string(payload))
}

func (s *gatewaySession) sendTurnCommitRequest(result TurnCandidateResult) error {
	if s == nil {
		return errors.New("gateway session unavailable")
	}
	s.controlMu.Lock()
	defer s.controlMu.Unlock()
	if s.controlDC == nil || s.controlDC.ReadyState() != webrtc.DataChannelStateOpen {
		return errors.New("turn commit control datachannel unavailable")
	}
	return sendDataChannelJSON(s.controlDC, newTurnCommitRequestMessage(result))
}

func (s *gatewaySession) sendTurnCandidateDecision(result TurnCandidateResult, decision string, reason string) error {
	if s == nil {
		return errors.New("gateway session unavailable")
	}
	s.controlMu.Lock()
	defer s.controlMu.Unlock()
	if s.controlDC == nil || s.controlDC.ReadyState() != webrtc.DataChannelStateOpen {
		return errors.New("turn candidate control datachannel unavailable")
	}
	return sendDataChannelJSON(s.controlDC, newTurnCandidateDecisionMessage(result, decision, reason))
}

func (s *gatewaySession) sendBargeInDecision(result BargeInResult) error {
	if s == nil {
		return errors.New("gateway session unavailable")
	}
	s.controlMu.Lock()
	defer s.controlMu.Unlock()
	if s.controlDC == nil || s.controlDC.ReadyState() != webrtc.DataChannelStateOpen {
		return errors.New("barge-in control datachannel unavailable")
	}
	return sendDataChannelJSON(s.controlDC, newBargeInDecisionMessage(result))
}

func (s *gatewaySession) drainRemoteRTPTrack(track *webrtc.TrackRemote) {
	key := rtpTrackKey{
		kind:     track.Kind().String(),
		id:       track.ID(),
		streamID: track.StreamID(),
	}
	startedAt := time.Now()
	defer func() {
		if snapshot, ok := s.rtpTrackSnapshot(key); ok {
			s.logInfo(
				"rtp_track_closed",
				"kind", snapshot.key.kind,
				"track_id", snapshot.key.id,
				"packets", snapshot.packetCount,
				"payload_bytes", snapshot.payloadBytes,
				"seq_gaps", snapshot.sequenceGaps,
				"timestamp_regressions", snapshot.timestampRegressions,
				"duration_ms", time.Since(startedAt).Milliseconds(),
			)
		}
	}()
	for {
		packet, _, err := track.ReadRTP()
		if err != nil {
			if !errors.Is(err, io.EOF) {
				s.logWarn(
					"rtp_track_read_closed",
					"kind", track.Kind().String(),
					"track_id", track.ID(),
					"error", err,
				)
			}
			return
		}
		snapshot := s.observeRTPPacket(key, packet)
		if snapshot.packetCount == 1 ||
			snapshot.packetCount%100 == 0 ||
			snapshot.sequenceGapDetected ||
			snapshot.timestampRegressionDetected {
			s.logInfo(
				"rtp_packet",
				"kind", snapshot.key.kind,
				"track_id", snapshot.key.id,
				"count", snapshot.packetCount,
				"payload_bytes", snapshot.lastPayloadBytes,
				"total_payload_bytes", snapshot.payloadBytes,
				"seq", snapshot.lastSequenceNumber,
				"rtp_timestamp", snapshot.lastTimestamp,
				"cached", snapshot.cachedPackets,
				"seq_gaps", snapshot.sequenceGaps,
				"timestamp_regressions", snapshot.timestampRegressions,
			)
		}
	}
}

func (s *gatewaySession) observeRTPPacket(key rtpTrackKey, packet *rtp.Packet) rtpTrackSnapshot {
	s.rtpMu.Lock()
	defer s.rtpMu.Unlock()

	if s.rtp == nil {
		s.rtp = make(map[rtpTrackKey]*rtpTrackStats)
	}
	stats := s.rtp[key]
	if stats == nil {
		stats = &rtpTrackStats{key: key}
		s.rtp[key] = stats
	}

	sequenceGapDetected := false
	timestampRegressionDetected := false
	if stats.packetCount == 0 {
		stats.firstSequenceNumber = packet.SequenceNumber
		stats.firstTimestamp = packet.Timestamp
	} else {
		expectedSequence := stats.lastSequenceNumber + 1
		if packet.SequenceNumber != expectedSequence {
			stats.sequenceGaps++
			sequenceGapDetected = true
		}
		if packet.Timestamp < stats.lastTimestamp {
			stats.timestampRegressions++
			timestampRegressionDetected = true
		}
	}

	stats.packetCount++
	stats.payloadBytes += uint64(len(packet.Payload))
	stats.lastSequenceNumber = packet.SequenceNumber
	stats.lastTimestamp = packet.Timestamp
	stats.recent = append(stats.recent, cachedRTPPacket{
		sequenceNumber: packet.SequenceNumber,
		timestamp:      packet.Timestamp,
		marker:         packet.Marker,
		payload:        append([]byte(nil), packet.Payload...),
	})
	if len(stats.recent) > maxCachedRTPPackets {
		stats.recent = stats.recent[len(stats.recent)-maxCachedRTPPackets:]
	}

	snapshot := stats.snapshot()
	snapshot.sequenceGapDetected = sequenceGapDetected
	snapshot.timestampRegressionDetected = timestampRegressionDetected
	s.recordCanonicalEvent(canonicalEventFromRTPPacketSnapshot(snapshot, packet.Payload))
	return snapshot
}

func (s *gatewaySession) rtpTrackSnapshot(key rtpTrackKey) (rtpTrackSnapshot, bool) {
	s.rtpMu.Lock()
	defer s.rtpMu.Unlock()

	stats := s.rtp[key]
	if stats == nil {
		return rtpTrackSnapshot{}, false
	}
	return stats.snapshot(), true
}

func (s *gatewaySession) cachedRTPPackets(key rtpTrackKey) []cachedRTPPacket {
	s.rtpMu.Lock()
	defer s.rtpMu.Unlock()

	stats := s.rtp[key]
	if stats == nil || len(stats.recent) == 0 {
		return nil
	}
	packets := make([]cachedRTPPacket, len(stats.recent))
	for i, packet := range stats.recent {
		packets[i] = packet
		packets[i].payload = append([]byte(nil), packet.payload...)
	}
	return packets
}

func (s *rtpTrackStats) snapshot() rtpTrackSnapshot {
	snapshot := rtpTrackSnapshot{
		key:                  s.key,
		packetCount:          s.packetCount,
		payloadBytes:         s.payloadBytes,
		firstSequenceNumber:  s.firstSequenceNumber,
		lastSequenceNumber:   s.lastSequenceNumber,
		firstTimestamp:       s.firstTimestamp,
		lastTimestamp:        s.lastTimestamp,
		cachedPackets:        len(s.recent),
		sequenceGaps:         s.sequenceGaps,
		timestampRegressions: s.timestampRegressions,
	}
	if len(s.recent) > 0 {
		snapshot.lastPayloadBytes = len(s.recent[len(s.recent)-1].payload)
	}
	return snapshot
}

func (s *gatewaySession) addRemoteCandidate(msg clientMessage) error {
	s.pcMu.Lock()
	pc := s.pc
	s.pcMu.Unlock()
	if pc == nil {
		return errors.New("rtc_ice_candidate received before rtc_offer")
	}
	return pc.AddICECandidate(webrtc.ICECandidateInit{
		Candidate:     msg.Candidate,
		SDPMid:        msg.SDPMid,
		SDPMLineIndex: msg.SDPMLineIndex,
	})
}

func (s *gatewaySession) replacePeerConnection(pc *webrtc.PeerConnection) {
	s.pcMu.Lock()
	old := s.pc
	s.pc = pc
	s.pcMu.Unlock()
	if old != nil {
		_ = old.Close()
	}
}

func (s *gatewaySession) close() {
	s.closeOnce.Do(func() {
		s.recordCanonicalEvent(CanonicalVoiceEvent{
			Type:      "interrupt",
			Transport: canonicalTransportServerCleanup,
			Reason:    "session_close",
		})
		if s.downlinkDone != nil {
			close(s.downlinkDone)
		}
		internalVoiceClosed := s.closeInternalVoiceSession()
		bridgeClosed := false
		if s.server != nil && s.server.clientEvents != nil {
			if closer, ok := s.server.clientEvents.(ClientSessionCloser); ok && closer != nil {
				closer.CloseClientSession(s.id)
				bridgeClosed = true
			}
		}
		if s.server != nil && s.server.voiceEvents != nil {
			if closer, ok := s.server.voiceEvents.(ClientSessionCloser); ok && closer != nil {
				closer.CloseClientSession(s.id)
			}
		}
		s.pcMu.Lock()
		pc := s.pc
		s.pc = nil
		s.pcMu.Unlock()
		s.downBlocked.Store(true)
		generation := s.downGen.Add(1)
		s.downMu.Lock()
		hadDownlink := s.downlink != nil
		traceID := s.downTraceID
		roundID := s.downRoundID
		playbackID := s.downPlayID
		s.downlink = nil
		s.downRTP = newDownlinkRTPSendState()
		s.downTraceID = ""
		s.downRoundID = ""
		s.downPlayID = ""
		s.downMu.Unlock()
		if pc != nil {
			_ = pc.Close()
		}
		s.logInfo(
			"session_cleanup_done",
			"peer_connection", pc != nil,
			"downlink_track", hadDownlink,
			"python_bridge", bridgeClosed,
			"internal_voice", internalVoiceClosed,
			"generation", generation,
			"trace_id", printable(traceID),
			"round_id", printable(roundID),
			"playback_id", printable(playbackID),
		)
	})
}

func (s *gatewaySession) closeInternalVoiceSession() bool {
	if s == nil {
		return false
	}
	s.internalVoiceMu.Lock()
	session := s.internalVoice
	s.internalVoice = nil
	s.internalVoiceMu.Unlock()
	if session == nil {
		return false
	}
	ctx := context.Background()
	if s.server != nil && s.server.cfg.InternalVoice.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.server.cfg.InternalVoice.Timeout)
		defer cancel()
	}
	if err := session.Close(ctx); err != nil {
		s.logWarn("internal_voice_session_close_failed", "error", err)
		return true
	}
	s.logInfo("internal_voice_session_closed")
	return true
}

func (s *gatewaySession) ensureMessageSession(sessionID string) error {
	sessionID = strings.TrimSpace(sessionID)
	if sessionID != "" && sessionID != s.id {
		return fmt.Errorf("session mismatch: expected=%s actual=%s", s.id, sessionID)
	}
	return nil
}

func (s *gatewaySession) sendJSON(value any) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.server.cfg.WriteTimeout > 0 {
		_ = s.conn.SetWriteDeadline(time.Now().Add(s.server.cfg.WriteTimeout))
	}
	return websocket.JSON.Send(s.conn, value)
}

func (s *gatewaySession) sendBinary(payload []byte) error {
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	if s.server.cfg.WriteTimeout > 0 {
		_ = s.conn.SetWriteDeadline(time.Now().Add(s.server.cfg.WriteTimeout))
	}
	return websocket.Message.Send(s.conn, payload)
}

func isOriginAllowed(r *http.Request, allowed []string) bool {
	origin := strings.TrimSpace(r.Header.Get("Origin"))
	if origin == "" {
		return true
	}
	if len(allowed) == 0 {
		parsed, err := url.Parse(origin)
		return err == nil && strings.EqualFold(parsed.Host, r.Host)
	}
	parsed, err := url.Parse(origin)
	originHost := ""
	if err == nil {
		originHost = parsed.Host
	}
	for _, item := range allowed {
		item = strings.TrimSpace(item)
		if item == "*" || strings.EqualFold(item, origin) || strings.EqualFold(item, originHost) {
			return true
		}
	}
	return false
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func newSessionID() string {
	var data [8]byte
	if _, err := rand.Read(data[:]); err != nil {
		return fmt.Sprintf("rtc_%d", time.Now().UnixNano())
	}
	return "rtc_" + hex.EncodeToString(data[:])
}

func printable(value string) string {
	if strings.TrimSpace(value) == "" {
		return "-"
	}
	return value
}
