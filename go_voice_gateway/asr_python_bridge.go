package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/net/websocket"
)

const (
	pythonGatewayAudioFrameMagic          = "VAF1"
	pythonGatewayAudioFrameVersion        = 1
	pythonGatewayAudioFrameHeaderMaxBytes = 4096
)

type PythonGatewayBridgeConfig struct {
	WSURL              string
	RobotID            string
	RobotSecret        string
	ClientType         string
	BotID              string
	Timeout            time.Duration
	TotalTimeout       time.Duration
	MaxMessageBytes    int
	ConnectionMode     string
	SessionIdleRecycle time.Duration
}

type PythonGatewayBridgeProcessor struct {
	cfg    PythonGatewayBridgeConfig
	logger *log.Logger
	dialer func(context.Context, string) (*websocket.Conn, error)

	downlink           PythonGatewayDownlinkForwarder
	credentialResolver pythonGatewayCredentialResolver
	aliasMu            sync.RWMutex
	pythonSessionAlias map[string]string
	metrics            pythonGatewayBridgeMetrics

	sessionMu      sync.Mutex
	sessionBridges map[string]*pythonGatewaySessionBridge
	closed         bool
}

type pythonGatewayBridgeMetrics struct {
	sessionBridgesCreated     atomic.Int64
	sessionBridgesClosed      atomic.Int64
	connectionOpenAttempts    atomic.Int64
	connectionOpenSuccesses   atomic.Int64
	connectionOpenFailures    atomic.Int64
	connectionReused          atomic.Int64
	connectionClosed          atomic.Int64
	idleRecycles              atomic.Int64
	sendErrors                atomic.Int64
	receiveErrors             atomic.Int64
	totalTimeouts             atomic.Int64
	gatewayErrors             atomic.Int64
	downlinkForwardErrors     atomic.Int64
	fastPathSendAttempts      atomic.Int64
	fastPathSendSuccesses     atomic.Int64
	fastPathSendErrors        atomic.Int64
	fastPathClientEventDrains atomic.Int64
	staleConnectionRetries    atomic.Int64
}

type pythonGatewaySessionBridge struct {
	processor                *PythonGatewayBridgeProcessor
	sessionID                string
	mu                       sync.Mutex
	connMu                   sync.RWMutex
	writeMu                  sync.Mutex
	conn                     *websocket.Conn
	lastUsedAt               time.Time
	fastPathMu               sync.Mutex
	fastPathAccepting        bool
	fastPathClientEventDones int64
}

func (p *PythonGatewayBridgeProcessor) BridgeHealthStatus() map[string]any {
	activeSessionBridges, activeConnections, pendingFastPathDones := p.sessionBridgeStatusCounts()
	return map[string]any{
		"connection_mode":                      printable(p.cfg.ConnectionMode),
		"active_session_bridges":               activeSessionBridges,
		"active_connections":                   activeConnections,
		"pending_fast_path_client_event_dones": pendingFastPathDones,
		"session_bridges_created":              p.metrics.sessionBridgesCreated.Load(),
		"session_bridges_closed":               p.metrics.sessionBridgesClosed.Load(),
		"connection_open_attempts":             p.metrics.connectionOpenAttempts.Load(),
		"connection_open_successes":            p.metrics.connectionOpenSuccesses.Load(),
		"connection_open_failures":             p.metrics.connectionOpenFailures.Load(),
		"connection_reused":                    p.metrics.connectionReused.Load(),
		"connection_closed":                    p.metrics.connectionClosed.Load(),
		"idle_recycles":                        p.metrics.idleRecycles.Load(),
		"send_errors":                          p.metrics.sendErrors.Load(),
		"receive_errors":                       p.metrics.receiveErrors.Load(),
		"total_timeouts":                       p.metrics.totalTimeouts.Load(),
		"gateway_errors":                       p.metrics.gatewayErrors.Load(),
		"downlink_forward_errors":              p.metrics.downlinkForwardErrors.Load(),
		"fast_path_send_attempts":              p.metrics.fastPathSendAttempts.Load(),
		"fast_path_send_successes":             p.metrics.fastPathSendSuccesses.Load(),
		"fast_path_send_errors":                p.metrics.fastPathSendErrors.Load(),
		"fast_path_client_event_drains":        p.metrics.fastPathClientEventDrains.Load(),
		"stale_connection_retries":             p.metrics.staleConnectionRetries.Load(),
	}
}

func (p *PythonGatewayBridgeProcessor) sessionBridgeStatusCounts() (int, int, int64) {
	p.sessionMu.Lock()
	bridges := make([]*pythonGatewaySessionBridge, 0, len(p.sessionBridges))
	for _, bridge := range p.sessionBridges {
		bridges = append(bridges, bridge)
	}
	p.sessionMu.Unlock()

	activeConnections := 0
	var pendingFastPathDones int64
	for _, bridge := range bridges {
		if bridge.currentConn() != nil {
			activeConnections++
		}
		pendingFastPathDones += bridge.pendingFastPathClientEventDones()
	}
	return len(bridges), activeConnections, pendingFastPathDones
}

type pythonGatewayConnectionIO interface {
	SendMessage(conn *websocket.Conn, payload []byte) error
	SendJSON(conn *websocket.Conn, payload any) error
}

type pythonGatewayDirectIO struct{}

func (pythonGatewayDirectIO) SendMessage(conn *websocket.Conn, payload []byte) error {
	return websocket.Message.Send(conn, payload)
}

func (pythonGatewayDirectIO) SendJSON(conn *websocket.Conn, payload any) error {
	return websocket.JSON.Send(conn, payload)
}

type PythonGatewayDownlinkForwarder interface {
	ForwardPythonGatewayDownlink(sessionID string, message pythonGatewayBridgeMessage) error
}

type pythonGatewayCredentialResolver interface {
	PythonGatewayCredentialsForSession(sessionID string) (pythonGatewaySessionCredentials, bool)
}

type pythonGatewaySessionCredentials struct {
	RobotID     string
	RobotSecret string
	ClientType  string
}

type pythonGatewayBridgeMessage struct {
	Type    string
	JSON    map[string]any
	Binary  []byte
	IsJSON  bool
	Payload []byte
}

type pythonGatewayBridgeSummary struct {
	TextMessages      int
	BinaryMessages    int
	BinaryBytes       int
	LastType          string
	ConnectionMode    string
	ConnectionReused  bool
	ConnectionOpenMS  int64
	ActionDurationMS  int64
	SendDurationMS    int64
	ReceiveDurationMS int64
	FirstTextMS       int64
	FirstBinaryMS     int64
	LastMessageMS     int64
	RequestCommitted  bool
	Accepted          bool
	PythonSessionID   string
	TurnCandidate     *TurnCandidateResult
	BargeIn           *BargeInResult
}

type pythonGatewayAudioFrameHeader struct {
	Type             string  `json:"type"`
	EventType        string  `json:"event_type,omitempty"`
	Version          int     `json:"version"`
	Encoding         string  `json:"encoding"`
	Direction        string  `json:"direction"`
	SessionID        string  `json:"session_id,omitempty"`
	ContextSessionID string  `json:"context_session_id,omitempty"`
	BotID            string  `json:"bot_id,omitempty"`
	TraceID          string  `json:"trace_id,omitempty"`
	UtteranceID      string  `json:"utterance_id,omitempty"`
	RoundID          string  `json:"round_id,omitempty"`
	PlaybackID       string  `json:"playback_id,omitempty"`
	SampleRate       uint32  `json:"sample_rate"`
	Channels         uint16  `json:"channels"`
	OpusFrameMS      uint16  `json:"opus_frame_ms"`
	PacketCount      int     `json:"packet_count"`
	PayloadBytes     int     `json:"payload_bytes,omitempty"`
	DurationMS       float64 `json:"duration_ms,omitempty"`
	ChunkSeq         uint64  `json:"chunk_seq,omitempty"`
	Seq              uint64  `json:"seq,omitempty"`
	TimestampMS      int64   `json:"timestamp_ms,omitempty"`
	CandidateSeq     uint64  `json:"candidate_seq,omitempty"`
	SpeechEpoch      uint64  `json:"speech_epoch,omitempty"`
	AudioWatermark   uint64  `json:"audio_watermark,omitempty"`
	SilenceMS        uint64  `json:"silence_ms,omitempty"`
	Shadow           bool    `json:"shadow,omitempty"`
}

func buildInternalVoiceInputAudioFrame(request ASRAudioRequest, defaultBotID string) ([]byte, error) {
	frame, err := buildPythonGatewayAudioFrame(request, defaultBotID)
	if err != nil {
		return nil, err
	}
	header, payload, err := splitPythonGatewayAudioFrame(frame)
	if err != nil {
		return nil, err
	}
	header.EventType = internalVoiceTypeInputAudioBatch
	header.Direction = "uplink"
	header.SessionID = strings.TrimSpace(request.SessionID)
	header.PayloadBytes = len(payload)
	return encodePythonGatewayAudioFrame(header, payload)
}

type pythonGatewayAudioSendError struct {
	err error
}

func (e pythonGatewayAudioSendError) Error() string {
	return fmt.Sprintf("send audio frame to Python Gateway: %v", e.err)
}

func (e pythonGatewayAudioSendError) Unwrap() error {
	return e.err
}

func NewPythonGatewayBridgeProcessor(cfg PythonGatewayBridgeConfig, logger *log.Logger) (*PythonGatewayBridgeProcessor, error) {
	cfg.WSURL = strings.TrimSpace(cfg.WSURL)
	if cfg.WSURL == "" {
		cfg.WSURL = defaultPythonGatewayWSURL
	}
	if _, err := parsePythonGatewayWSURL(cfg.WSURL); err != nil {
		return nil, err
	}
	cfg.ClientType = strings.TrimSpace(cfg.ClientType)
	if cfg.ClientType == "" {
		cfg.ClientType = "go_voice_gateway"
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 30 * time.Second
	}
	if cfg.TotalTimeout < 0 {
		cfg.TotalTimeout = time.Duration(defaultPythonGatewayTotalTimeoutMS) * time.Millisecond
	}
	if cfg.MaxMessageBytes <= 0 {
		cfg.MaxMessageBytes = defaultMessageMaxByte
	}
	if cfg.SessionIdleRecycle < 0 {
		cfg.SessionIdleRecycle = 0
	}
	connectionMode, err := parsePythonGatewayConnectionMode(cfg.ConnectionMode)
	if err != nil {
		return nil, err
	}
	cfg.ConnectionMode = connectionMode
	return &PythonGatewayBridgeProcessor{cfg: cfg, logger: logger}, nil
}

func NewPythonGatewayBridgeProcessorFromASRConfig(cfg ASRConfig, defaultBotID string, logger *log.Logger) (*PythonGatewayBridgeProcessor, error) {
	return NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:              cfg.PythonGatewayWSURL,
		RobotID:            cfg.PythonGatewayRobotID,
		RobotSecret:        cfg.PythonGatewayRobotSecret,
		ClientType:         cfg.PythonGatewayClientType,
		BotID:              firstNonEmpty(cfg.PythonGatewayBotID, defaultBotID),
		Timeout:            time.Duration(maxInt(cfg.PythonGatewayTimeoutMS, 1)) * time.Millisecond,
		TotalTimeout:       time.Duration(maxInt(cfg.PythonGatewayTotalTimeoutMS, 0)) * time.Millisecond,
		MaxMessageBytes:    maxInt(cfg.PythonGatewayMaxMessageKB, 1) * 1024,
		ConnectionMode:     cfg.PythonGatewayConnectionMode,
		SessionIdleRecycle: time.Duration(maxInt(cfg.PythonGatewaySessionIdleRecycleMS, 0)) * time.Millisecond,
	}, logger)
}

func (p *PythonGatewayBridgeProcessor) ProcessASRAudioRequest(request ASRAudioRequest) error {
	if request.Provisional {
		return fmt.Errorf("provisional turn candidate cannot use the committed ASR bridge")
	}
	startedAt := time.Now()
	frame, err := buildPythonGatewayAudioFrame(request, p.cfg.BotID)
	if err != nil {
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_bridge_start session=%s trace_id=%s utterance_id=%s packets=%d payload_bytes=%d frame_bytes=%d lossy=%t",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			request.PacketCount,
			request.RetainedPacketPayloadBytes,
			len(frame),
			request.Lossy,
		)
	}

	var summary pythonGatewayBridgeSummary
	for attempt := 0; ; attempt++ {
		summary, err = p.withPythonGatewayConnection(request.SessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
			var summary pythonGatewayBridgeSummary
			sessionBridge := pythonGatewaySessionBridgeFromIO(io)
			sessionBridge.beginFastPathReceiveWindow()
			sendStartedAt := time.Now()
			p.setPythonGatewayWriteDeadline(conn)
			if err := io.SendMessage(conn, frame); err != nil {
				sessionBridge.closeFastPathReceiveWindow()
				summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
				p.metrics.sendErrors.Add(1)
				return summary, pythonGatewayAudioSendError{err: err}
			}
			summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
			receiveStartedAt := time.Now()
			received, err := p.receiveUntil(conn, "done", request.SessionID, sessionBridge)
			received.SendDurationMS = summary.SendDurationMS
			received.ReceiveDurationMS = time.Since(receiveStartedAt).Milliseconds()
			return received, err
		})
		if err == nil {
			break
		}
		if attempt == 0 && summary.ConnectionReused && isRetryablePythonGatewayStaleWrite(err) {
			p.metrics.staleConnectionRetries.Add(1)
			if p.logger != nil {
				p.logger.Printf(
					"python_gateway_stale_connection_retry session=%s trace_id=%s utterance_id=%s error=%v",
					printable(request.SessionID),
					printable(request.TraceID),
					printable(request.UtteranceID),
					err,
				)
			}
			continue
		}
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_bridge_done session=%s trace_id=%s utterance_id=%s text_messages=%d binary_messages=%d binary_bytes=%d duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d receive_ms=%d action_ms=%d first_text_ms=%d first_binary_ms=%d last_message_ms=%d",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			summary.TextMessages,
			summary.BinaryMessages,
			summary.BinaryBytes,
			time.Since(startedAt).Milliseconds(),
			printable(summary.ConnectionMode),
			summary.ConnectionReused,
			summary.ConnectionOpenMS,
			summary.SendDurationMS,
			summary.ReceiveDurationMS,
			summary.ActionDurationMS,
			summary.FirstTextMS,
			summary.FirstBinaryMS,
			summary.LastMessageMS,
		)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) ProcessTurnCandidateAudioRequest(request ASRAudioRequest) error {
	_, err := p.ProcessTurnCandidateAudioRequestWithResult(request)
	return err
}

func (p *PythonGatewayBridgeProcessor) ProcessTurnCandidateAudioRequestWithResult(request ASRAudioRequest) (TurnCandidateResult, error) {
	if !request.Provisional || request.CandidateSeq == 0 {
		return TurnCandidateResult{}, fmt.Errorf("turn candidate request metadata is invalid")
	}
	frame, err := buildPythonGatewayTurnCandidateFrame(request, p.cfg.BotID)
	if err != nil {
		return TurnCandidateResult{}, err
	}
	startedAt := time.Now()
	summary, err := p.withPythonGatewayConnection(request.SessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		var summary pythonGatewayBridgeSummary
		sendStartedAt := time.Now()
		p.setPythonGatewayWriteDeadline(conn)
		if err := io.SendMessage(conn, frame); err != nil {
			p.metrics.sendErrors.Add(1)
			return summary, pythonGatewayAudioSendError{err: err}
		}
		summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
		received, err := p.receiveUntil(conn, "done", "", nil)
		received.SendDurationMS = summary.SendDurationMS
		return received, err
	})
	if p.logger != nil {
		p.logger.Printf(
			"turn_candidate_python_done session=%s trace_id=%s utterance_id=%s candidate_seq=%d speech_epoch=%d audio_watermark=%d silence_ms=%d packets=%d duration_ms=%d error=%v",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			request.CandidateSeq,
			request.SpeechEpoch,
			request.AudioWatermark,
			request.SilenceMS,
			request.PacketCount,
			time.Since(startedAt).Milliseconds(),
			err,
		)
	}
	if err != nil {
		return TurnCandidateResult{}, err
	}
	if summary.TurnCandidate == nil {
		return TurnCandidateResult{}, fmt.Errorf("Python Gateway turn candidate result missing")
	}
	return *summary.TurnCandidate, nil
}

func (p *PythonGatewayBridgeProcessor) ProcessBargeInAudioRequestWithResult(request ASRAudioRequest) (BargeInResult, error) {
	if !request.Provisional || request.CandidateKind != "barge_in" || request.CandidateSeq == 0 {
		return BargeInResult{}, fmt.Errorf("barge-in request metadata is invalid")
	}
	frame, err := buildPythonGatewayBargeInFrame(request, p.cfg.BotID)
	if err != nil {
		return BargeInResult{}, err
	}
	summary, err := p.withPythonGatewayConnection(request.SessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		var summary pythonGatewayBridgeSummary
		sendStartedAt := time.Now()
		p.setPythonGatewayWriteDeadline(conn)
		if err := io.SendMessage(conn, frame); err != nil {
			p.metrics.sendErrors.Add(1)
			return summary, pythonGatewayAudioSendError{err: err}
		}
		summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
		received, err := p.receiveUntil(conn, "done", "", nil)
		received.SendDurationMS = summary.SendDurationMS
		return received, err
	})
	if err != nil {
		return BargeInResult{}, err
	}
	if summary.BargeIn == nil {
		return BargeInResult{}, fmt.Errorf("Python Gateway barge-in result missing")
	}
	return *summary.BargeIn, nil
}

func isRetryablePythonGatewayStaleWrite(err error) bool {
	var sendErr pythonGatewayAudioSendError
	if !errors.As(err, &sendErr) {
		return false
	}
	return errors.Is(sendErr.err, syscall.EPIPE) ||
		errors.Is(sendErr.err, syscall.ECONNRESET) ||
		errors.Is(sendErr.err, io.ErrClosedPipe)
}

func (p *PythonGatewayBridgeProcessor) ProcessClientEvent(sessionID string, message clientMessage) error {
	startedAt := time.Now()
	event := strings.TrimSpace(message.Event)
	if event == "" {
		return fmt.Errorf("client_event is missing event")
	}
	payload := map[string]any{
		"type":     "client_event",
		"event":    event,
		"source":   firstNonEmpty(strings.TrimSpace(message.Source), "go_voice_gateway"),
		"trace_id": traceIDForVoiceTurn(message.TraceID, sessionID, firstNonEmpty(message.EventID, message.RoundID, message.PlaybackID)),
	}
	if eventID := strings.TrimSpace(message.EventID); eventID != "" {
		payload["event_id"] = eventID
	}
	if botID := firstNonEmpty(strings.TrimSpace(message.BotID), strings.TrimSpace(p.cfg.BotID)); botID != "" {
		payload["bot_id"] = botID
	}

	if summary, sent, err := p.trySendSessionJSONDuringActive(sessionID, payload); sent || err != nil {
		if err != nil {
			return err
		}
		if p.logger != nil {
			p.logger.Printf(
				"python_gateway_client_event_bridge_done session=%s event=%s event_id=%s text_messages=%d binary_messages=%d binary_bytes=%d duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d receive_ms=%d action_ms=%d first_text_ms=%d first_binary_ms=%d last_message_ms=%d fast_path=%t",
				printable(sessionID),
				printable(event),
				printable(message.EventID),
				summary.TextMessages,
				summary.BinaryMessages,
				summary.BinaryBytes,
				time.Since(startedAt).Milliseconds(),
				printable(summary.ConnectionMode),
				summary.ConnectionReused,
				summary.ConnectionOpenMS,
				summary.SendDurationMS,
				summary.ReceiveDurationMS,
				summary.ActionDurationMS,
				summary.FirstTextMS,
				summary.FirstBinaryMS,
				summary.LastMessageMS,
				true,
			)
		}
		return nil
	}

	summary, err := p.withPythonGatewayConnection(sessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		var summary pythonGatewayBridgeSummary
		sessionBridge := pythonGatewaySessionBridgeFromIO(io)
		sessionBridge.beginFastPathReceiveWindow()
		sendStartedAt := time.Now()
		p.setPythonGatewayWriteDeadline(conn)
		if err := io.SendJSON(conn, payload); err != nil {
			sessionBridge.closeFastPathReceiveWindow()
			summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
			p.metrics.sendErrors.Add(1)
			return summary, fmt.Errorf("send client_event to Python Gateway: %w", err)
		}
		summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
		receiveStartedAt := time.Now()
		received, err := p.receiveUntil(conn, "done", sessionID, sessionBridge)
		received.SendDurationMS = summary.SendDurationMS
		received.ReceiveDurationMS = time.Since(receiveStartedAt).Milliseconds()
		return received, err
	})
	if err != nil {
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_client_event_bridge_done session=%s event=%s event_id=%s text_messages=%d binary_messages=%d binary_bytes=%d duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d receive_ms=%d action_ms=%d first_text_ms=%d first_binary_ms=%d last_message_ms=%d",
			printable(sessionID),
			printable(event),
			printable(message.EventID),
			summary.TextMessages,
			summary.BinaryMessages,
			summary.BinaryBytes,
			time.Since(startedAt).Milliseconds(),
			printable(summary.ConnectionMode),
			summary.ConnectionReused,
			summary.ConnectionOpenMS,
			summary.SendDurationMS,
			summary.ReceiveDurationMS,
			summary.ActionDurationMS,
			summary.FirstTextMS,
			summary.FirstBinaryMS,
			summary.LastMessageMS,
		)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) ProcessClientText(sessionID string, message clientMessage) error {
	startedAt := time.Now()
	content := strings.TrimSpace(message.Content)
	if content == "" {
		return fmt.Errorf("text is missing content")
	}
	payload := map[string]any{
		"type":     "text",
		"content":  content,
		"source":   firstNonEmpty(strings.TrimSpace(message.Source), "go_voice_gateway"),
		"trace_id": traceIDForVoiceTurn(message.TraceID, sessionID, ""),
	}
	if utteranceID := strings.TrimSpace(message.UtteranceID); utteranceID != "" {
		payload["utterance_id"] = utteranceID
	}
	if botID := firstNonEmpty(strings.TrimSpace(message.BotID), strings.TrimSpace(p.cfg.BotID)); botID != "" {
		payload["bot_id"] = botID
	}

	summary, err := p.withPythonGatewayConnection(sessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		var summary pythonGatewayBridgeSummary
		sessionBridge := pythonGatewaySessionBridgeFromIO(io)
		sessionBridge.beginFastPathReceiveWindow()
		sendStartedAt := time.Now()
		p.setPythonGatewayWriteDeadline(conn)
		if err := io.SendJSON(conn, payload); err != nil {
			sessionBridge.closeFastPathReceiveWindow()
			summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
			p.metrics.sendErrors.Add(1)
			return summary, fmt.Errorf("send text to Python Gateway: %w", err)
		}
		summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
		receiveStartedAt := time.Now()
		received, err := p.receiveUntil(conn, "done", sessionID, sessionBridge)
		received.SendDurationMS = summary.SendDurationMS
		received.ReceiveDurationMS = time.Since(receiveStartedAt).Milliseconds()
		return received, err
	})
	if err != nil {
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_text_bridge_done session=%s source=%s content_chars=%d text_messages=%d binary_messages=%d binary_bytes=%d duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d receive_ms=%d action_ms=%d first_text_ms=%d first_binary_ms=%d last_message_ms=%d",
			printable(sessionID),
			printable(firstNonEmpty(strings.TrimSpace(message.Source), "go_voice_gateway")),
			len([]rune(content)),
			summary.TextMessages,
			summary.BinaryMessages,
			summary.BinaryBytes,
			time.Since(startedAt).Milliseconds(),
			printable(summary.ConnectionMode),
			summary.ConnectionReused,
			summary.ConnectionOpenMS,
			summary.SendDurationMS,
			summary.ReceiveDurationMS,
			summary.ActionDurationMS,
			summary.FirstTextMS,
			summary.FirstBinaryMS,
			summary.LastMessageMS,
		)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) ProcessPlaybackReport(sessionID string, message clientMessage) error {
	startedAt := time.Now()
	if message.Type != "playback_complete" && message.Type != "playback_interrupted" {
		return fmt.Errorf("unsupported playback report type: %s", message.Type)
	}
	if strings.TrimSpace(message.RoundID) == "" {
		return fmt.Errorf("%s missing round_id", message.Type)
	}
	payload := pythonGatewayPlaybackReportPayload(sessionID, message)
	if summary, sent, err := p.trySendSessionJSONDuringActive(sessionID, payload); sent || err != nil {
		if err != nil {
			return err
		}
		if p.logger != nil {
			p.logger.Printf(
				"python_gateway_playback_report_forwarded session=%s type=%s round_id=%s playback_id=%s duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d action_ms=%d fast_path=%t",
				printable(sessionID),
				printable(message.Type),
				printable(message.RoundID),
				printable(message.PlaybackID),
				time.Since(startedAt).Milliseconds(),
				printable(summary.ConnectionMode),
				summary.ConnectionReused,
				summary.ConnectionOpenMS,
				summary.SendDurationMS,
				summary.ActionDurationMS,
				true,
			)
		}
		return nil
	}
	summary, err := p.withPythonGatewayConnection(sessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		return p.sendPythonGatewayJSONOnly(conn, io, payload)
	})
	if err != nil {
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_playback_report_forwarded session=%s type=%s round_id=%s playback_id=%s duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d action_ms=%d",
			printable(sessionID),
			printable(message.Type),
			printable(message.RoundID),
			printable(message.PlaybackID),
			time.Since(startedAt).Milliseconds(),
			printable(summary.ConnectionMode),
			summary.ConnectionReused,
			summary.ConnectionOpenMS,
			summary.SendDurationMS,
			summary.ActionDurationMS,
		)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) ProcessClientControl(sessionID string, message clientMessage) error {
	startedAt := time.Now()
	if message.Type != "interrupt" && message.Type != "audio_cancel" {
		return fmt.Errorf("unsupported client control type: %s", message.Type)
	}
	payload := pythonGatewayClientControlPayload(sessionID, message)
	if summary, sent, err := p.trySendSessionJSONDuringActive(sessionID, payload); sent || err != nil {
		if err != nil {
			return err
		}
		if p.logger != nil {
			p.logger.Printf(
				"python_gateway_client_control_forwarded session=%s type=%s utterance_id=%s reason=%s duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d action_ms=%d fast_path=%t",
				printable(sessionID),
				printable(message.Type),
				printable(message.UtteranceID),
				printable(message.Reason),
				time.Since(startedAt).Milliseconds(),
				printable(summary.ConnectionMode),
				summary.ConnectionReused,
				summary.ConnectionOpenMS,
				summary.SendDurationMS,
				summary.ActionDurationMS,
				true,
			)
		}
		return nil
	}
	summary, err := p.withPythonGatewayConnection(sessionID, func(conn *websocket.Conn, io pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		return p.sendPythonGatewayJSONOnly(conn, io, payload)
	})
	if err != nil {
		return err
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_client_control_forwarded session=%s type=%s utterance_id=%s reason=%s duration_ms=%d connection_mode=%s connection_reused=%t connection_open_ms=%d send_ms=%d action_ms=%d",
			printable(sessionID),
			printable(message.Type),
			printable(message.UtteranceID),
			printable(message.Reason),
			time.Since(startedAt).Milliseconds(),
			printable(summary.ConnectionMode),
			summary.ConnectionReused,
			summary.ConnectionOpenMS,
			summary.SendDurationMS,
			summary.ActionDurationMS,
		)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) ProcessClientControlDuringActive(sessionID string, message clientMessage) (bool, error) {
	if message.Type != "interrupt" && message.Type != "audio_cancel" {
		return false, fmt.Errorf("unsupported client control type: %s", message.Type)
	}
	if p.cfg.ConnectionMode != pythonGatewayConnectionModeSession {
		return false, nil
	}
	key := strings.TrimSpace(sessionID)
	if key == "" {
		key = "default"
	}
	p.sessionMu.Lock()
	if p.closed {
		p.sessionMu.Unlock()
		return false, fmt.Errorf("python gateway bridge processor is closed")
	}
	bridge := p.sessionBridges[key]
	p.sessionMu.Unlock()
	if bridge == nil {
		return false, nil
	}
	_, sent, err := bridge.trySendJSONDuringActive(pythonGatewayClientControlPayload(sessionID, message))
	return sent, err
}

func (p *PythonGatewayBridgeProcessor) AuthenticateRegister(ctx context.Context, message clientMessage) (deviceRegistration, error) {
	robotID := strings.TrimSpace(message.RobotID)
	if robotID == "" {
		return deviceRegistration{}, fmt.Errorf("register missing robot_id")
	}
	robotSecret := ""
	if message.RobotSecret != nil {
		robotSecret = strings.TrimSpace(*message.RobotSecret)
	}
	if robotSecret == "" {
		return deviceRegistration{}, fmt.Errorf("register missing robot_secret")
	}
	clientType := firstNonEmpty(strings.TrimSpace(message.ClientType), p.cfg.ClientType)

	ctx, cancel := context.WithTimeout(ctx, p.cfg.Timeout)
	defer cancel()
	p.metrics.connectionOpenAttempts.Add(1)
	conn, err := p.dial(ctx)
	if err != nil {
		p.metrics.connectionOpenFailures.Add(1)
		return deviceRegistration{}, err
	}
	success := false
	defer func() {
		_ = conn.Close()
		p.metrics.connectionClosed.Add(1)
		if !success {
			p.metrics.connectionOpenFailures.Add(1)
		}
	}()
	conn.MaxPayloadBytes = p.cfg.MaxMessageBytes
	_ = conn.SetDeadline(time.Now().Add(p.cfg.Timeout))

	if _, err := p.receiveUntil(conn, "connected", "", nil); err != nil {
		return deviceRegistration{}, err
	}
	p.setPythonGatewayWriteDeadline(conn)
	if err := sendPythonGatewayRegister(conn, robotID, robotSecret, clientType); err != nil {
		p.metrics.sendErrors.Add(1)
		return deviceRegistration{}, err
	}
	registered, err := p.receiveRegistered(conn)
	if err != nil {
		return deviceRegistration{}, err
	}
	success = true
	p.metrics.connectionOpenSuccesses.Add(1)
	registration := deviceRegistration{
		RobotID:    firstNonEmpty(strings.TrimSpace(registered.RobotID), robotID),
		BotID:      firstNonEmpty(strings.TrimSpace(registered.BotID), strings.TrimSpace(p.cfg.BotID)),
		BotName:    strings.TrimSpace(registered.BotName),
		ClientType: firstNonEmpty(strings.TrimSpace(registered.ClientType), clientType),
		IsNewRobot: registered.IsNewRobot,
	}
	if registration.BotName == "" {
		registration.BotName = registration.BotID
	}
	if p.logger != nil {
		p.logger.Printf(
			"python_gateway_register_auth_ok robot_id=%s client_type=%s bot_id=%s",
			printable(registration.RobotID),
			printable(registration.ClientType),
			printable(registration.BotID),
		)
	}
	return registration, nil
}

func (p *PythonGatewayBridgeProcessor) Close() {
	p.sessionMu.Lock()
	p.closed = true
	bridges := make([]*pythonGatewaySessionBridge, 0, len(p.sessionBridges))
	for _, bridge := range p.sessionBridges {
		bridges = append(bridges, bridge)
	}
	p.metrics.sessionBridgesClosed.Add(int64(len(bridges)))
	p.sessionBridges = nil
	p.sessionMu.Unlock()
	p.aliasMu.Lock()
	p.pythonSessionAlias = nil
	p.aliasMu.Unlock()

	for _, bridge := range bridges {
		bridge.close()
	}
}

func (p *PythonGatewayBridgeProcessor) CloseClientSession(sessionID string) {
	key := strings.TrimSpace(sessionID)
	if key == "" {
		key = "default"
	}

	p.sessionMu.Lock()
	bridge := p.sessionBridges[key]
	if bridge != nil {
		delete(p.sessionBridges, key)
	}
	p.sessionMu.Unlock()
	p.unbindPythonSessionAliases(key)

	if bridge != nil {
		p.metrics.sessionBridgesClosed.Add(1)
		bridge.close()
	}
}

func (p *PythonGatewayBridgeProcessor) WarmSession(sessionID string) error {
	if p == nil || p.cfg.ConnectionMode != pythonGatewayConnectionModeSession {
		return nil
	}
	_, err := p.withPythonGatewayConnection(sessionID, func(_ *websocket.Conn, _ pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error) {
		return pythonGatewayBridgeSummary{}, nil
	})
	return err
}

func (p *PythonGatewayBridgeProcessor) withPythonGatewayConnection(
	sessionID string,
	action func(*websocket.Conn, pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error),
) (pythonGatewayBridgeSummary, error) {
	ctx, cancel := context.WithTimeout(context.Background(), p.cfg.Timeout)
	defer cancel()
	if p.cfg.ConnectionMode != pythonGatewayConnectionModeSession {
		openStartedAt := time.Now()
		conn, pythonSessionID, err := p.openRegisteredConnection(ctx, p.credentialsForSession(sessionID))
		if err != nil {
			return pythonGatewayBridgeSummary{}, err
		}
		p.bindPythonSessionAlias(sessionID, pythonSessionID)
		connectionOpenMS := time.Since(openStartedAt).Milliseconds()
		defer func() {
			p.unbindPythonSessionAliases(sessionID)
			_ = conn.Close()
			p.metrics.connectionClosed.Add(1)
		}()
		actionStartedAt := time.Now()
		summary, err := action(conn, pythonGatewayDirectIO{})
		summary.ConnectionMode = p.cfg.ConnectionMode
		summary.ConnectionReused = false
		summary.ConnectionOpenMS = connectionOpenMS
		summary.ActionDurationMS = time.Since(actionStartedAt).Milliseconds()
		return summary, err
	}
	bridge, err := p.sessionBridge(sessionID)
	if err != nil {
		return pythonGatewayBridgeSummary{}, err
	}
	return bridge.withConnection(ctx, action)
}

func (p *PythonGatewayBridgeProcessor) sessionBridge(sessionID string) (*pythonGatewaySessionBridge, error) {
	key := strings.TrimSpace(sessionID)
	if key == "" {
		key = "default"
	}

	p.sessionMu.Lock()
	defer p.sessionMu.Unlock()
	if p.closed {
		return nil, fmt.Errorf("python gateway bridge processor is closed")
	}
	if p.sessionBridges == nil {
		p.sessionBridges = make(map[string]*pythonGatewaySessionBridge)
	}
	bridge := p.sessionBridges[key]
	if bridge == nil {
		bridge = &pythonGatewaySessionBridge{
			processor: p,
			sessionID: key,
		}
		p.sessionBridges[key] = bridge
		p.metrics.sessionBridgesCreated.Add(1)
	}
	return bridge, nil
}

func (p *PythonGatewayBridgeProcessor) openRegisteredConnection(ctx context.Context, credentials pythonGatewaySessionCredentials) (*websocket.Conn, string, error) {
	p.metrics.connectionOpenAttempts.Add(1)
	conn, err := p.dial(ctx)
	if err != nil {
		p.metrics.connectionOpenFailures.Add(1)
		return nil, "", err
	}
	success := false
	defer func() {
		if !success {
			_ = conn.Close()
			p.metrics.connectionClosed.Add(1)
			p.metrics.connectionOpenFailures.Add(1)
		}
	}()
	conn.MaxPayloadBytes = p.cfg.MaxMessageBytes
	_ = conn.SetDeadline(time.Now().Add(p.cfg.Timeout))

	connected, err := p.receiveUntil(conn, "connected", "", nil)
	if err != nil {
		return nil, "", err
	}
	pythonSessionID := connected.PythonSessionID
	if strings.TrimSpace(credentials.RobotID) != "" {
		p.setPythonGatewayWriteDeadline(conn)
		if err := sendPythonGatewayRegister(conn, credentials.RobotID, credentials.RobotSecret, credentials.ClientType); err != nil {
			p.metrics.sendErrors.Add(1)
			return nil, "", err
		}
		registered, err := p.receiveUntil(conn, "registered", "", nil)
		if err != nil {
			return nil, "", err
		}
		if registered.PythonSessionID != "" {
			pythonSessionID = registered.PythonSessionID
		}
	}
	success = true
	p.metrics.connectionOpenSuccesses.Add(1)
	return conn, pythonSessionID, nil
}

func (b *pythonGatewaySessionBridge) withConnection(
	ctx context.Context,
	action func(*websocket.Conn, pythonGatewayConnectionIO) (pythonGatewayBridgeSummary, error),
) (pythonGatewayBridgeSummary, error) {
	b.mu.Lock()
	defer b.mu.Unlock()

	b.closeIdleConnectionLocked(time.Now())
	conn, reused, openDuration, err := b.ensureConnection(ctx)
	if err != nil {
		b.closeLocked()
		return pythonGatewayBridgeSummary{}, err
	}
	_ = conn.SetDeadline(time.Now().Add(b.processor.cfg.Timeout))
	actionStartedAt := time.Now()
	summary, err := action(conn, b)
	summary.ConnectionMode = b.processor.cfg.ConnectionMode
	summary.ConnectionReused = reused
	summary.ConnectionOpenMS = openDuration.Milliseconds()
	summary.ActionDurationMS = time.Since(actionStartedAt).Milliseconds()
	b.lastUsedAt = time.Now()
	if err != nil {
		b.closeLocked()
		return summary, err
	}
	return summary, nil
}

func (p *PythonGatewayBridgeProcessor) trySendSessionJSONDuringActive(sessionID string, payload map[string]any) (pythonGatewayBridgeSummary, bool, error) {
	if p.cfg.ConnectionMode != pythonGatewayConnectionModeSession {
		return pythonGatewayBridgeSummary{}, false, nil
	}
	bridge, err := p.sessionBridge(sessionID)
	if err != nil {
		return pythonGatewayBridgeSummary{}, false, err
	}
	return bridge.trySendJSONDuringActive(payload)
}

func (b *pythonGatewaySessionBridge) trySendJSONDuringActive(payload map[string]any) (pythonGatewayBridgeSummary, bool, error) {
	if b.mu.TryLock() {
		b.mu.Unlock()
		return pythonGatewayBridgeSummary{}, false, nil
	}
	conn := b.currentConn()
	if conn == nil {
		return pythonGatewayBridgeSummary{}, false, nil
	}

	expectDone := stringValue(payload["type"]) == "client_event"
	if !b.reserveFastPathSend(expectDone) {
		return pythonGatewayBridgeSummary{}, false, nil
	}
	b.processor.metrics.fastPathSendAttempts.Add(1)
	summary := pythonGatewayBridgeSummary{
		LastType:         "fast_path_send_only",
		ConnectionMode:   b.processor.cfg.ConnectionMode,
		ConnectionReused: true,
	}
	sendStartedAt := time.Now()
	b.processor.setPythonGatewayWriteDeadline(conn)
	err := b.SendJSON(conn, payload)
	summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
	summary.ActionDurationMS = summary.SendDurationMS
	if err != nil {
		b.processor.metrics.sendErrors.Add(1)
		b.processor.metrics.fastPathSendErrors.Add(1)
		if expectDone {
			b.cancelReservedFastPathDone()
		}
		b.closeConnIfCurrent(conn)
		return summary, true, fmt.Errorf("send %s to active Python Gateway session: %w", stringValue(payload["type"]), err)
	}
	b.processor.metrics.fastPathSendSuccesses.Add(1)
	return summary, true, nil
}

func pythonGatewaySessionBridgeFromIO(io pythonGatewayConnectionIO) *pythonGatewaySessionBridge {
	bridge, _ := io.(*pythonGatewaySessionBridge)
	return bridge
}

func (b *pythonGatewaySessionBridge) beginFastPathReceiveWindow() {
	if b == nil {
		return
	}
	b.fastPathMu.Lock()
	b.fastPathAccepting = true
	b.fastPathMu.Unlock()
}

func (b *pythonGatewaySessionBridge) closeFastPathReceiveWindow() int64 {
	if b == nil {
		return 0
	}
	b.fastPathMu.Lock()
	defer b.fastPathMu.Unlock()
	b.fastPathAccepting = false
	pending := b.fastPathClientEventDones
	if pending < 0 {
		return 0
	}
	return pending
}

func (b *pythonGatewaySessionBridge) completeFastPathClientEventDone() int64 {
	if b == nil {
		return 0
	}
	b.fastPathMu.Lock()
	defer b.fastPathMu.Unlock()
	b.fastPathClientEventDones--
	remaining := b.fastPathClientEventDones
	if remaining < 0 {
		b.fastPathClientEventDones = 0
		return 0
	}
	return remaining
}

func (b *pythonGatewaySessionBridge) reserveFastPathSend(expectDone bool) bool {
	if b == nil {
		return false
	}
	b.fastPathMu.Lock()
	defer b.fastPathMu.Unlock()
	if !b.fastPathAccepting {
		return false
	}
	if expectDone {
		b.fastPathClientEventDones++
	}
	return true
}

func (b *pythonGatewaySessionBridge) cancelReservedFastPathDone() {
	if b == nil {
		return
	}
	b.fastPathMu.Lock()
	defer b.fastPathMu.Unlock()
	b.fastPathClientEventDones--
	if b.fastPathClientEventDones < 0 {
		b.fastPathClientEventDones = 0
	}
}

func (b *pythonGatewaySessionBridge) SendMessage(conn *websocket.Conn, payload []byte) error {
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	return websocket.Message.Send(conn, payload)
}

func (b *pythonGatewaySessionBridge) SendJSON(conn *websocket.Conn, payload any) error {
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	return websocket.JSON.Send(conn, payload)
}

func (b *pythonGatewaySessionBridge) ensureConnection(ctx context.Context) (*websocket.Conn, bool, time.Duration, error) {
	if conn := b.currentConn(); conn != nil {
		b.processor.metrics.connectionReused.Add(1)
		return conn, true, 0, nil
	}
	credentials := b.processor.credentialsForSession(b.sessionID)
	openStartedAt := time.Now()
	conn, pythonSessionID, err := b.processor.openRegisteredConnection(ctx, credentials)
	if err != nil {
		return nil, false, 0, err
	}
	openDuration := time.Since(openStartedAt)
	b.connMu.Lock()
	b.conn = conn
	b.connMu.Unlock()
	b.processor.bindPythonSessionAlias(b.sessionID, pythonSessionID)
	if b.processor.logger != nil {
		b.processor.logger.Printf(
			"python_gateway_session_bridge_connected session=%s mode=%s robot_id=%s connection_open_ms=%d",
			printable(b.sessionID),
			b.processor.cfg.ConnectionMode,
			printable(credentials.RobotID),
			openDuration.Milliseconds(),
		)
	}
	return conn, false, openDuration, nil
}

func (b *pythonGatewaySessionBridge) closeIdleConnectionLocked(now time.Time) {
	idleRecycle := b.processor.cfg.SessionIdleRecycle
	if idleRecycle <= 0 || b.lastUsedAt.IsZero() {
		return
	}
	conn := b.currentConn()
	if conn == nil {
		return
	}
	idleFor := now.Sub(b.lastUsedAt)
	if idleFor < idleRecycle {
		return
	}
	if b.processor.logger != nil {
		b.processor.logger.Printf(
			"python_gateway_session_bridge_idle_recycle session=%s idle_ms=%d threshold_ms=%d",
			printable(b.sessionID),
			idleFor.Milliseconds(),
			idleRecycle.Milliseconds(),
		)
	}
	b.processor.metrics.idleRecycles.Add(1)
	b.closeLocked()
}

func (b *pythonGatewaySessionBridge) close() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.closeLocked()
}

func (b *pythonGatewaySessionBridge) closeLocked() {
	b.fastPathMu.Lock()
	b.fastPathAccepting = false
	b.fastPathClientEventDones = 0
	b.fastPathMu.Unlock()
	b.processor.unbindPythonSessionAliases(b.sessionID)
	b.closeCurrentConn()
	b.lastUsedAt = time.Time{}
}

func (b *pythonGatewaySessionBridge) currentConn() *websocket.Conn {
	b.connMu.RLock()
	defer b.connMu.RUnlock()
	return b.conn
}

func (b *pythonGatewaySessionBridge) pendingFastPathClientEventDones() int64 {
	if b == nil {
		return 0
	}
	b.fastPathMu.Lock()
	defer b.fastPathMu.Unlock()
	if b.fastPathClientEventDones < 0 {
		return 0
	}
	return b.fastPathClientEventDones
}

func (b *pythonGatewaySessionBridge) closeCurrentConn() {
	b.connMu.Lock()
	conn := b.conn
	b.conn = nil
	b.connMu.Unlock()
	if conn != nil {
		_ = conn.Close()
		b.processor.metrics.connectionClosed.Add(1)
	}
}

func (b *pythonGatewaySessionBridge) closeConnIfCurrent(conn *websocket.Conn) {
	if conn == nil {
		return
	}
	b.connMu.Lock()
	shouldClose := b.conn == conn
	if shouldClose {
		b.conn = nil
	}
	b.connMu.Unlock()
	if shouldClose {
		_ = conn.Close()
		b.processor.metrics.connectionClosed.Add(1)
	}
}

func (p *PythonGatewayBridgeProcessor) dial(ctx context.Context) (*websocket.Conn, error) {
	if p.dialer != nil {
		return p.dialer(ctx, p.cfg.WSURL)
	}
	location, err := parsePythonGatewayWSURL(p.cfg.WSURL)
	if err != nil {
		return nil, err
	}
	origin := pythonGatewayOrigin(location)
	cfg, err := websocket.NewConfig(location.String(), origin)
	if err != nil {
		return nil, fmt.Errorf("create Python Gateway websocket config: %w", err)
	}
	return cfg.DialContext(ctx)
}

func (p *PythonGatewayBridgeProcessor) credentialsForSession(sessionID string) pythonGatewaySessionCredentials {
	credentials := pythonGatewaySessionCredentials{
		RobotID:     strings.TrimSpace(p.cfg.RobotID),
		RobotSecret: strings.TrimSpace(p.cfg.RobotSecret),
		ClientType:  strings.TrimSpace(p.cfg.ClientType),
	}
	if p.credentialResolver != nil {
		if resolved, ok := p.credentialResolver.PythonGatewayCredentialsForSession(sessionID); ok {
			credentials.RobotID = strings.TrimSpace(resolved.RobotID)
			credentials.RobotSecret = strings.TrimSpace(resolved.RobotSecret)
			if strings.TrimSpace(resolved.ClientType) != "" {
				credentials.ClientType = strings.TrimSpace(resolved.ClientType)
			}
		}
	}
	credentials.RobotID = strings.TrimSpace(credentials.RobotID)
	credentials.RobotSecret = strings.TrimSpace(credentials.RobotSecret)
	credentials.ClientType = firstNonEmpty(strings.TrimSpace(credentials.ClientType), strings.TrimSpace(p.cfg.ClientType), "go_voice_gateway")
	return credentials
}

func sendPythonGatewayRegister(conn *websocket.Conn, robotID, robotSecret, clientType string) error {
	payload := map[string]any{
		"type":        "register",
		"robot_id":    strings.TrimSpace(robotID),
		"client_type": strings.TrimSpace(clientType),
	}
	if strings.TrimSpace(robotSecret) != "" {
		payload["robot_secret"] = strings.TrimSpace(robotSecret)
	}
	if err := websocket.JSON.Send(conn, payload); err != nil {
		return fmt.Errorf("send register to Python Gateway: %w", err)
	}
	return nil
}

func (p *PythonGatewayBridgeProcessor) receiveUntil(
	conn *websocket.Conn,
	targetType string,
	forwardSessionID string,
	sessionBridge *pythonGatewaySessionBridge,
) (pythonGatewayBridgeSummary, error) {
	var summary pythonGatewayBridgeSummary
	startedAt := time.Now()
	totalDeadline := p.pythonGatewayTotalDeadline(startedAt)
	totalExpired, stopTotalTimeout := p.startPythonGatewayTotalTimeout(conn)
	defer stopTotalTimeout()
	defer sessionBridge.closeFastPathReceiveWindow()
	targetSeen := false
	fastPathDoneRemaining := int64(0)
	for {
		if pythonGatewayTotalTimeoutExpired(totalExpired) || (!totalDeadline.IsZero() && !time.Now().Before(totalDeadline)) {
			p.metrics.totalTimeouts.Add(1)
			p.metrics.receiveErrors.Add(1)
			return summary, fmt.Errorf(
				"receive Python Gateway message before %s: total timeout after %s (text_messages=%d binary_messages=%d last_type=%s last_message_ms=%d)",
				targetType,
				p.cfg.TotalTimeout,
				summary.TextMessages,
				summary.BinaryMessages,
				printable(summary.LastType),
				summary.LastMessageMS,
			)
		}
		p.setPythonGatewayReadDeadlineUntil(conn, totalDeadline)
		message, err := receivePythonGatewayBridgeMessage(conn)
		if err != nil {
			if pythonGatewayTotalTimeoutExpired(totalExpired) || (!totalDeadline.IsZero() && !time.Now().Before(totalDeadline)) {
				p.metrics.totalTimeouts.Add(1)
				p.metrics.receiveErrors.Add(1)
				return summary, fmt.Errorf(
					"receive Python Gateway message before %s: total timeout after %s (text_messages=%d binary_messages=%d last_type=%s last_message_ms=%d): %w",
					targetType,
					p.cfg.TotalTimeout,
					summary.TextMessages,
					summary.BinaryMessages,
					printable(summary.LastType),
					summary.LastMessageMS,
					err,
				)
			}
			p.metrics.receiveErrors.Add(1)
			return summary, fmt.Errorf("receive Python Gateway message before %s: %w", targetType, err)
		}
		elapsedMS := time.Since(startedAt).Milliseconds()
		summary.LastMessageMS = elapsedMS
		if forwardSessionID != "" && p.downlink != nil {
			if err := p.downlink.ForwardPythonGatewayDownlink(forwardSessionID, message); err != nil {
				p.metrics.downlinkForwardErrors.Add(1)
				return summary, fmt.Errorf(
					"forward Python Gateway downlink failed session=%s type=%s json=%t binary=%t payload_bytes=%d elapsed_ms=%d: %w",
					printable(forwardSessionID),
					printable(message.Type),
					message.IsJSON,
					len(message.Binary) > 0,
					len(message.Payload),
					elapsedMS,
					err,
				)
			}
		}
		if !message.IsJSON {
			if summary.BinaryMessages == 0 {
				summary.FirstBinaryMS = elapsedMS
			}
			summary.BinaryMessages++
			summary.BinaryBytes += len(message.Binary)
			continue
		}
		if summary.TextMessages == 0 {
			summary.FirstTextMS = elapsedMS
		}
		summary.TextMessages++
		summary.LastType = message.Type
		if summary.PythonSessionID == "" {
			summary.PythonSessionID = strings.TrimSpace(stringValue(message.JSON["session_id"]))
		}
		switch message.Type {
		case "error":
			p.metrics.gatewayErrors.Add(1)
			return summary, pythonGatewayError(message.JSON)
		case "turn_candidate_result":
			result, err := turnCandidateResultFromPythonMessage(message.JSON)
			if err != nil {
				return summary, err
			}
			summary.TurnCandidate = &result
			if targetType == message.Type {
				return summary, nil
			}
			continue
		case "barge_in_probe_result":
			result, err := bargeInResultFromPythonMessage(message.JSON)
			if err != nil {
				return summary, err
			}
			summary.BargeIn = &result
			if targetType == message.Type {
				return summary, nil
			}
			continue
		case targetType:
			if !targetSeen {
				targetSeen = true
				fastPathDoneRemaining = sessionBridge.closeFastPathReceiveWindow()
				if fastPathDoneRemaining > 0 {
					continue
				}
				return summary, nil
			}
			if fastPathDoneRemaining > 0 {
				p.metrics.fastPathClientEventDrains.Add(1)
				fastPathDoneRemaining = sessionBridge.completeFastPathClientEventDone()
				if fastPathDoneRemaining > 0 {
					continue
				}
			}
			return summary, nil
		default:
			continue
		}
	}
}

func turnCandidateResultFromPythonMessage(data map[string]any) (TurnCandidateResult, error) {
	result := TurnCandidateResult{
		TraceID:        strings.TrimSpace(stringValue(data["trace_id"])),
		UtteranceID:    strings.TrimSpace(stringValue(data["utterance_id"])),
		CandidateSeq:   uint64Value(data["candidate_seq"]),
		SpeechEpoch:    uint64Value(data["speech_epoch"]),
		AudioWatermark: uint64Value(data["audio_watermark"]),
		Status:         strings.TrimSpace(stringValue(data["status"])),
		ASRText:        strings.TrimSpace(stringValue(data["asr_text"])),
		ASRTimeMS:      float64Value(data["asr_time_ms"]),
		PolicyPreview:  strings.TrimSpace(stringValue(data["policy_preview"])),
		Shadow:         boolValue(data["shadow"]),
	}
	if result.UtteranceID == "" || result.CandidateSeq == 0 || result.AudioWatermark == 0 {
		return TurnCandidateResult{}, fmt.Errorf("Python Gateway turn candidate result metadata is invalid")
	}
	return result, nil
}

func bargeInResultFromPythonMessage(data map[string]any) (BargeInResult, error) {
	result := BargeInResult{
		TraceID:        strings.TrimSpace(stringValue(data["trace_id"])),
		UtteranceID:    strings.TrimSpace(stringValue(data["utterance_id"])),
		RoundID:        strings.TrimSpace(stringValue(data["round_id"])),
		PlaybackID:     strings.TrimSpace(stringValue(data["playback_id"])),
		CandidateSeq:   uint64Value(data["candidate_seq"]),
		SpeechEpoch:    uint64Value(data["speech_epoch"]),
		AudioWatermark: uint64Value(data["audio_watermark"]),
		Status:         strings.TrimSpace(stringValue(data["status"])),
		Decision:       strings.TrimSpace(stringValue(data["decision"])),
		Reason:         strings.TrimSpace(stringValue(data["reason"])),
		ASRText:        strings.TrimSpace(stringValue(data["asr_text"])),
		ASRTimeMS:      float64Value(data["asr_time_ms"]),
	}
	if result.UtteranceID == "" || result.RoundID == "" || result.PlaybackID == "" || result.CandidateSeq == 0 || result.SpeechEpoch == 0 || result.AudioWatermark == 0 {
		return BargeInResult{}, fmt.Errorf("Python Gateway barge-in result metadata is invalid")
	}
	if result.Decision != "ignore" && result.Decision != "interrupt" && result.Decision != "new_intent" {
		return BargeInResult{}, fmt.Errorf("Python Gateway barge-in decision is invalid: %s", result.Decision)
	}
	return result, nil
}

func uint64Value(value any) uint64 {
	switch typed := value.(type) {
	case uint64:
		return typed
	case int:
		if typed > 0 {
			return uint64(typed)
		}
	case float64:
		if typed > 0 {
			return uint64(typed)
		}
	case json.Number:
		parsed, _ := typed.Int64()
		if parsed > 0 {
			return uint64(parsed)
		}
	}
	return 0
}

func float64Value(value any) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case json.Number:
		parsed, _ := typed.Float64()
		return parsed
	}
	return 0
}

func boolValue(value any) bool {
	typed, _ := value.(bool)
	return typed
}

func (p *PythonGatewayBridgeProcessor) bindPythonSessionAlias(clientSessionID, pythonSessionID string) {
	clientSessionID = strings.TrimSpace(clientSessionID)
	pythonSessionID = strings.TrimSpace(pythonSessionID)
	if clientSessionID == "" || pythonSessionID == "" {
		return
	}
	p.aliasMu.Lock()
	if p.pythonSessionAlias == nil {
		p.pythonSessionAlias = make(map[string]string)
	}
	p.pythonSessionAlias[pythonSessionID] = clientSessionID
	p.aliasMu.Unlock()
}

func (p *PythonGatewayBridgeProcessor) resolveClientSessionAlias(pythonSessionID string) string {
	p.aliasMu.RLock()
	defer p.aliasMu.RUnlock()
	return p.pythonSessionAlias[strings.TrimSpace(pythonSessionID)]
}

func (p *PythonGatewayBridgeProcessor) pythonSessionIDForClientSession(clientSessionID string) string {
	clientSessionID = strings.TrimSpace(clientSessionID)
	if clientSessionID == "" {
		return ""
	}
	p.aliasMu.RLock()
	defer p.aliasMu.RUnlock()
	for pythonSessionID, mappedClientSessionID := range p.pythonSessionAlias {
		if mappedClientSessionID == clientSessionID {
			return pythonSessionID
		}
	}
	return ""
}

func (p *PythonGatewayBridgeProcessor) unbindPythonSessionAliases(clientSessionID string) {
	clientSessionID = strings.TrimSpace(clientSessionID)
	p.aliasMu.Lock()
	defer p.aliasMu.Unlock()
	for pythonSessionID, mappedClientSessionID := range p.pythonSessionAlias {
		if mappedClientSessionID == clientSessionID {
			delete(p.pythonSessionAlias, pythonSessionID)
		}
	}
}

func (p *PythonGatewayBridgeProcessor) sendPythonGatewayJSONOnly(conn *websocket.Conn, io pythonGatewayConnectionIO, payload map[string]any) (pythonGatewayBridgeSummary, error) {
	var summary pythonGatewayBridgeSummary
	sendStartedAt := time.Now()
	p.setPythonGatewayWriteDeadline(conn)
	if err := io.SendJSON(conn, payload); err != nil {
		summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
		p.metrics.sendErrors.Add(1)
		return summary, fmt.Errorf("send %s to Python Gateway: %w", stringValue(payload["type"]), err)
	}
	summary.SendDurationMS = time.Since(sendStartedAt).Milliseconds()
	return summary, nil
}

func pythonGatewayPlaybackReportPayload(sessionID string, message clientMessage) map[string]any {
	payload := map[string]any{
		"type":     strings.TrimSpace(message.Type),
		"trace_id": traceIDForVoiceTurn(message.TraceID, sessionID, message.RoundID),
		"round_id": strings.TrimSpace(message.RoundID),
	}
	if playbackID := strings.TrimSpace(message.PlaybackID); playbackID != "" {
		payload["playback_id"] = playbackID
	}
	if reason := strings.TrimSpace(message.Reason); reason != "" {
		payload["reason"] = reason
	}
	if message.FirstAudioToPlaybackStartMS != nil {
		payload["first_audio_to_playback_start_ms"] = *message.FirstAudioToPlaybackStartMS
	}
	if message.PlaybackStartToCompleteMS != nil {
		payload["playback_start_to_complete_ms"] = *message.PlaybackStartToCompleteMS
	}
	if message.PushedChunks != nil {
		payload["pushed_chunks"] = *message.PushedChunks
	}
	if message.PushedSamples != nil {
		payload["pushed_samples"] = *message.PushedSamples
	}
	if message.UnderrunCallbacks != nil {
		payload["underrun_callbacks"] = *message.UnderrunCallbacks
	}
	if message.ZeroFilledSamples != nil {
		payload["zero_filled_samples"] = *message.ZeroFilledSamples
	}
	if message.MaxBufferedSamples != nil {
		payload["max_buffered_samples"] = *message.MaxBufferedSamples
	}
	return payload
}

func pythonGatewayClientControlPayload(sessionID string, message clientMessage) map[string]any {
	payload := map[string]any{
		"type":     strings.TrimSpace(message.Type),
		"trace_id": traceIDForVoiceTurn(message.TraceID, sessionID, firstNonEmpty(message.UtteranceID, message.RoundID, message.PlaybackID)),
	}
	if utteranceID := strings.TrimSpace(message.UtteranceID); utteranceID != "" {
		payload["utterance_id"] = utteranceID
	}
	if reason := strings.TrimSpace(message.Reason); reason != "" {
		payload["reason"] = reason
	}
	if source := strings.TrimSpace(message.Source); source != "" {
		payload["source"] = source
	}
	if botID := strings.TrimSpace(message.BotID); botID != "" {
		payload["bot_id"] = botID
	}
	return payload
}

func (p *PythonGatewayBridgeProcessor) receiveRegistered(conn *websocket.Conn) (registeredMessage, error) {
	for {
		p.setPythonGatewayReadDeadline(conn)
		message, err := receivePythonGatewayBridgeMessage(conn)
		if err != nil {
			p.metrics.receiveErrors.Add(1)
			return registeredMessage{}, fmt.Errorf("receive Python Gateway register response: %w", err)
		}
		if !message.IsJSON {
			continue
		}
		switch message.Type {
		case "error":
			p.metrics.gatewayErrors.Add(1)
			return registeredMessage{}, pythonGatewayError(message.JSON)
		case "registered":
			payload, err := json.Marshal(message.JSON)
			if err != nil {
				return registeredMessage{}, err
			}
			var registered registeredMessage
			if err := json.Unmarshal(payload, &registered); err != nil {
				return registeredMessage{}, fmt.Errorf("parse Python Gateway registered response: %w", err)
			}
			return registered, nil
		default:
			continue
		}
	}
}

func (p *PythonGatewayBridgeProcessor) setPythonGatewayReadDeadline(conn *websocket.Conn) {
	p.setPythonGatewayReadDeadlineUntil(conn, time.Time{})
}

func (p *PythonGatewayBridgeProcessor) setPythonGatewayReadDeadlineUntil(conn *websocket.Conn, totalDeadline time.Time) {
	if conn == nil || p.cfg.Timeout <= 0 {
		return
	}
	deadline := time.Now().Add(p.cfg.Timeout)
	if !totalDeadline.IsZero() && totalDeadline.Before(deadline) {
		deadline = totalDeadline
	}
	_ = conn.SetDeadline(deadline)
}

func (p *PythonGatewayBridgeProcessor) pythonGatewayTotalDeadline(startedAt time.Time) time.Time {
	if p.cfg.TotalTimeout <= 0 {
		return time.Time{}
	}
	return startedAt.Add(p.cfg.TotalTimeout)
}

func (p *PythonGatewayBridgeProcessor) startPythonGatewayTotalTimeout(conn *websocket.Conn) (<-chan struct{}, func()) {
	if p.cfg.TotalTimeout <= 0 {
		return nil, func() {}
	}
	expired := make(chan struct{})
	var finished atomic.Bool
	timer := time.AfterFunc(p.cfg.TotalTimeout, func() {
		if !finished.CompareAndSwap(false, true) {
			return
		}
		close(expired)
		if conn != nil {
			_ = conn.Close()
		}
	})
	stop := func() {
		if finished.CompareAndSwap(false, true) {
			timer.Stop()
		}
	}
	return expired, stop
}

func pythonGatewayTotalTimeoutExpired(expired <-chan struct{}) bool {
	if expired == nil {
		return false
	}
	select {
	case <-expired:
		return true
	default:
		return false
	}
}

func (p *PythonGatewayBridgeProcessor) setPythonGatewayWriteDeadline(conn *websocket.Conn) {
	if conn == nil || p.cfg.Timeout <= 0 {
		return
	}
	_ = conn.SetWriteDeadline(time.Now().Add(p.cfg.Timeout))
}

func receivePythonGatewayBridgeMessage(conn *websocket.Conn) (pythonGatewayBridgeMessage, error) {
	var payload []byte
	if err := websocket.Message.Receive(conn, &payload); err != nil {
		return pythonGatewayBridgeMessage{}, err
	}
	message := pythonGatewayBridgeMessage{Payload: payload}
	if looksLikePythonGatewayJSONPayload(payload) {
		var decoded map[string]any
		if err := json.Unmarshal(payload, &decoded); err == nil {
			if messageType, ok := decoded["type"].(string); ok && messageType != "" {
				message.Type = messageType
				message.JSON = decoded
				message.IsJSON = true
				return message, nil
			}
		}
	}
	message.Binary = payload
	return message, nil
}

func looksLikePythonGatewayJSONPayload(payload []byte) bool {
	for _, b := range payload {
		switch b {
		case ' ', '\n', '\r', '\t':
			continue
		case '{':
			return true
		default:
			return false
		}
	}
	return false
}

func pythonGatewayError(payload map[string]any) error {
	code := stringValue(payload["code"])
	message := stringValue(payload["message"])
	if message == "" {
		if raw, err := json.Marshal(payload); err == nil {
			message = string(raw)
		}
	}
	if code == "" {
		code = "PYTHON_GATEWAY_ERROR"
	}
	return fmt.Errorf("python gateway error: code=%s message=%s", code, message)
}

func buildPythonGatewayAudioFrame(request ASRAudioRequest, defaultBotID string) ([]byte, error) {
	if request.Type != "" && request.Type != asrAudioRequestType {
		return nil, fmt.Errorf("unsupported ASR request type for Python Gateway: %s", request.Type)
	}
	if request.AudioEncoding != asrAudioRequestEncodingOpus {
		return nil, fmt.Errorf("unsupported ASR request encoding for Python Gateway: %s", request.AudioEncoding)
	}
	if request.PacketStreamFormat != asrAudioRequestPacketStreamOpus {
		return nil, fmt.Errorf("unsupported ASR packet stream for Python Gateway: %s", request.PacketStreamFormat)
	}
	if len(request.AudioBytes) == 0 {
		return nil, fmt.Errorf("ASR request audio bytes are empty")
	}
	if request.PacketCount <= 0 {
		return nil, fmt.Errorf("ASR request packet count must be positive")
	}

	botID := strings.TrimSpace(request.BotID)
	if botID == "" {
		botID = strings.TrimSpace(defaultBotID)
	}
	header := pythonGatewayAudioFrameHeader{
		Type:        "audio_frame",
		Version:     pythonGatewayAudioFrameVersion,
		Encoding:    asrAudioRequestEncodingOpus,
		Direction:   "client_input",
		BotID:       botID,
		TraceID:     traceIDForVoiceTurn(request.TraceID, request.SessionID, request.UtteranceID),
		UtteranceID: request.UtteranceID,
		SampleRate:  request.SampleRate,
		Channels:    request.Channels,
		OpusFrameMS: request.OpusFrameMS,
		PacketCount: request.PacketCount,
	}
	return encodePythonGatewayAudioFrame(header, request.AudioBytes)
}

func buildPythonGatewayTurnCandidateFrame(request ASRAudioRequest, defaultBotID string) ([]byte, error) {
	frame, err := buildPythonGatewayAudioFrame(request, defaultBotID)
	if err != nil {
		return nil, err
	}
	header, payload, err := splitPythonGatewayAudioFrame(frame)
	if err != nil {
		return nil, err
	}
	header.EventType = "turn_candidate"
	header.ContextSessionID = request.ContextSessionID
	header.CandidateSeq = request.CandidateSeq
	header.SpeechEpoch = request.SpeechEpoch
	header.AudioWatermark = request.AudioWatermark
	header.SilenceMS = request.SilenceMS
	header.Shadow = request.Shadow
	return encodePythonGatewayAudioFrame(header, payload)
}

func buildPythonGatewayBargeInFrame(request ASRAudioRequest, defaultBotID string) ([]byte, error) {
	frame, err := buildPythonGatewayAudioFrame(request, defaultBotID)
	if err != nil {
		return nil, err
	}
	header, payload, err := splitPythonGatewayAudioFrame(frame)
	if err != nil {
		return nil, err
	}
	header.EventType = "barge_in_probe"
	header.RoundID = request.RoundID
	header.PlaybackID = request.PlaybackID
	header.CandidateSeq = request.CandidateSeq
	header.SpeechEpoch = request.SpeechEpoch
	header.AudioWatermark = request.AudioWatermark
	return encodePythonGatewayAudioFrame(header, payload)
}

func encodePythonGatewayAudioFrame(header pythonGatewayAudioFrameHeader, payload []byte) ([]byte, error) {
	headerJSON, err := json.Marshal(header)
	if err != nil {
		return nil, fmt.Errorf("marshal Python Gateway audio frame header: %w", err)
	}
	if len(headerJSON) > pythonGatewayAudioFrameHeaderMaxBytes {
		return nil, fmt.Errorf("Python Gateway audio frame header too large: %d", len(headerJSON))
	}
	frame := make([]byte, 0, len(pythonGatewayAudioFrameMagic)+4+len(headerJSON)+len(payload))
	frame = append(frame, pythonGatewayAudioFrameMagic...)
	var headerLen [4]byte
	binary.BigEndian.PutUint32(headerLen[:], uint32(len(headerJSON)))
	frame = append(frame, headerLen[:]...)
	frame = append(frame, headerJSON...)
	frame = append(frame, payload...)
	return frame, nil
}

func splitPythonGatewayAudioFrame(frame []byte) (pythonGatewayAudioFrameHeader, []byte, error) {
	var header pythonGatewayAudioFrameHeader
	prefixBytes := len(pythonGatewayAudioFrameMagic) + 4
	if len(frame) < prefixBytes || string(frame[:len(pythonGatewayAudioFrameMagic)]) != pythonGatewayAudioFrameMagic {
		return header, nil, fmt.Errorf("Python Gateway audio frame prefix is invalid")
	}
	headerBytes := int(binary.BigEndian.Uint32(frame[len(pythonGatewayAudioFrameMagic):prefixBytes]))
	if headerBytes <= 0 || headerBytes > pythonGatewayAudioFrameHeaderMaxBytes || len(frame) < prefixBytes+headerBytes {
		return header, nil, fmt.Errorf("Python Gateway audio frame header length is invalid: %d", headerBytes)
	}
	if err := json.Unmarshal(frame[prefixBytes:prefixBytes+headerBytes], &header); err != nil {
		return header, nil, fmt.Errorf("decode Python Gateway audio frame header: %w", err)
	}
	return header, frame[prefixBytes+headerBytes:], nil
}

func parsePythonGatewayWSURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return nil, fmt.Errorf("parse Python Gateway websocket URL: %w", err)
	}
	if parsed.Scheme != "ws" && parsed.Scheme != "wss" {
		return nil, fmt.Errorf("Python Gateway websocket URL must use ws or wss: %s", raw)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("Python Gateway websocket URL host is empty")
	}
	return parsed, nil
}

func pythonGatewayOrigin(location *url.URL) string {
	scheme := "http"
	if location.Scheme == "wss" {
		scheme = "https"
	}
	return (&url.URL{Scheme: scheme, Host: location.Host}).String()
}
