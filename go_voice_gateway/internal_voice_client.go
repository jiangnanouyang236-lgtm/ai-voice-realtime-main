package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/net/websocket"
)

type InternalVoiceClient struct {
	cfg    InternalVoiceConfig
	logger *log.Logger
	dialer func(context.Context, string) (*websocket.Conn, error)

	metrics internalVoiceClientMetrics
}

type internalVoiceClientMetrics struct {
	openAttempts         atomic.Int64
	openSuccesses        atomic.Int64
	openFailures         atomic.Int64
	closeAttempts        atomic.Int64
	closeSuccesses       atomic.Int64
	closeFailures        atomic.Int64
	clientEventAttempts  atomic.Int64
	clientEventSuccesses atomic.Int64
	clientEventFailures  atomic.Int64
	inputAudioAttempts   atomic.Int64
	inputAudioSuccesses  atomic.Int64
	inputAudioFailures   atomic.Int64
	inputBatchAttempts   atomic.Int64
	inputBatchSuccesses  atomic.Int64
	inputBatchFailures   atomic.Int64
	interruptAttempts    atomic.Int64
	interruptSuccesses   atomic.Int64
	interruptFailures    atomic.Int64
	playbackAttempts     atomic.Int64
	playbackSuccesses    atomic.Int64
	playbackFailures     atomic.Int64
	heartbeatAttempts    atomic.Int64
	heartbeatSuccesses   atomic.Int64
	heartbeatFailures    atomic.Int64
	heartbeatDead        atomic.Int64
	activeSessions       atomic.Int64
}

type InternalVoiceSessionOpenRequest struct {
	SessionID   string
	RobotID     string
	RobotSecret string
	ClientType  string
	BotID       string
	Source      string
	Transport   string
	BridgeMode  string
	RequestedAt time.Time
}

type InternalVoiceSession struct {
	client        *InternalVoiceClient
	sessionID     string
	conn          *websocket.Conn
	writeMu       sync.Mutex
	pendingMu     sync.Mutex
	pending       map[uint64]*internalVoiceWaiter
	nextWaiterID  atomic.Uint64
	readerErrMu   sync.Mutex
	readerErr     error
	readerDone    chan struct{}
	lastReceive   atomic.Int64
	heartbeatStop chan struct{}
	heartbeatDone chan struct{}
	heartbeatOnce sync.Once
	closeOnce     sync.Once
}

func NewInternalVoiceClient(cfg InternalVoiceConfig, logger *log.Logger) (*InternalVoiceClient, error) {
	normalized, err := normalizeInternalVoiceConfig(cfg)
	if err != nil {
		return nil, err
	}
	return &InternalVoiceClient{cfg: normalized, logger: logger}, nil
}

func (c *InternalVoiceClient) OpenSession(ctx context.Context, request InternalVoiceSessionOpenRequest) (*InternalVoiceSession, error) {
	if c == nil {
		return nil, fmt.Errorf("internal voice client is nil")
	}
	sessionID := strings.TrimSpace(request.SessionID)
	if sessionID == "" {
		return nil, fmt.Errorf("internal voice session_id is required")
	}
	c.metrics.openAttempts.Add(1)
	ctx, cancel := c.withTimeout(ctx)
	defer cancel()

	conn, err := c.dial(ctx)
	if err != nil {
		c.metrics.openFailures.Add(1)
		return nil, err
	}
	success := false
	defer func() {
		if !success {
			_ = conn.Close()
		}
	}()
	conn.MaxPayloadBytes = c.cfg.MaxMessageBytes
	c.setDeadline(conn)

	envelope, err := newInternalVoiceEnvelope(
		internalVoiceTypeSessionOpen,
		sessionID,
		internalVoiceSessionOpenPayload{
			RobotID:     strings.TrimSpace(request.RobotID),
			RobotSecret: strings.TrimSpace(request.RobotSecret),
			ClientType:  firstNonEmpty(strings.TrimSpace(request.ClientType), "go_voice_gateway"),
			BotID:       strings.TrimSpace(request.BotID),
			Source:      firstNonEmpty(strings.TrimSpace(request.Source), "go_voice_gateway"),
			Transport:   strings.TrimSpace(request.Transport),
			BridgeMode:  strings.TrimSpace(request.BridgeMode),
		},
	)
	if err != nil {
		c.metrics.openFailures.Add(1)
		return nil, err
	}
	if !request.RequestedAt.IsZero() {
		envelope.TimestampMS = request.RequestedAt.UnixNano() / int64(time.Millisecond)
	}

	c.setWriteDeadline(conn)
	if err := websocket.JSON.Send(conn, envelope); err != nil {
		c.metrics.openFailures.Add(1)
		return nil, fmt.Errorf("send internal voice session.open: %w", err)
	}
	if err := c.receiveExpected(conn, internalVoiceTypeSessionOpened, sessionID); err != nil {
		c.metrics.openFailures.Add(1)
		return nil, err
	}

	success = true
	c.metrics.openSuccesses.Add(1)
	c.metrics.activeSessions.Add(1)
	if c.logger != nil {
		c.logger.Printf(
			"internal_voice_session_opened session=%s url=%s robot_id=%s client_type=%s bot_id=%s",
			printable(sessionID),
			printable(c.cfg.WSURL),
			printable(request.RobotID),
			printable(request.ClientType),
			printable(request.BotID),
		)
	}
	_ = conn.SetDeadline(time.Time{})
	session := &InternalVoiceSession{
		client:        c,
		sessionID:     sessionID,
		conn:          conn,
		pending:       make(map[uint64]*internalVoiceWaiter),
		readerDone:    make(chan struct{}),
		heartbeatStop: make(chan struct{}),
		heartbeatDone: make(chan struct{}),
	}
	session.lastReceive.Store(time.Now().UnixMilli())
	session.startReader()
	session.startHeartbeat()
	return session, nil
}

func (s *InternalVoiceSession) Close(ctx context.Context) error {
	if s == nil {
		return nil
	}
	var closeErr error
	s.closeOnce.Do(func() {
		s.stopHeartbeat()
		if s.client != nil {
			s.client.metrics.closeAttempts.Add(1)
		}
		defer func() {
			if s.conn != nil {
				_ = s.conn.Close()
			}
			if s.client != nil {
				s.client.metrics.activeSessions.Add(-1)
			}
		}()
		if s.conn == nil || s.client == nil {
			return
		}
		ctx, cancel := s.client.withTimeout(ctx)
		defer cancel()
		done := make(chan error, 1)
		go func() {
			done <- s.closeOnConn()
		}()
		select {
		case err := <-done:
			closeErr = err
		case <-ctx.Done():
			closeErr = fmt.Errorf("internal voice session.close timeout: %w", ctx.Err())
		}
		if closeErr != nil {
			s.client.metrics.closeFailures.Add(1)
			return
		}
		s.client.metrics.closeSuccesses.Add(1)
		if s.client.logger != nil {
			s.client.logger.Printf(
				"internal_voice_session_closed session=%s url=%s",
				printable(s.sessionID),
				printable(s.client.cfg.WSURL),
			)
		}
	})
	return closeErr
}

func (s *InternalVoiceSession) closeOnConn() error {
	envelope, err := newInternalVoiceEnvelope(internalVoiceTypeSessionClose, s.sessionID, nil)
	if err != nil {
		return err
	}
	waiter, err := s.registerWaiter(
		internalVoiceWaiterType,
		internalVoiceTypeSessionClosed,
		envelope,
	)
	if err != nil {
		return err
	}
	defer s.unregisterWaiter(waiter)
	if err := s.sendEnvelope(envelope, "session.close"); err != nil {
		return err
	}
	ctx, cancel := s.client.withTimeout(context.Background())
	defer cancel()
	if _, err := waitForInternalVoiceMessage(ctx, waiter); err != nil {
		return fmt.Errorf("receive internal voice session.closed: %w", err)
	}
	return nil
}

func (s *InternalVoiceSession) SendClientEvent(ctx context.Context, message clientMessage, transport string) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	event := strings.TrimSpace(message.Event)
	if event == "" {
		return fmt.Errorf("internal voice client_event is missing event")
	}
	s.client.metrics.clientEventAttempts.Add(1)

	payload := internalVoiceClientEventPayload{
		Event:   event,
		EventID: strings.TrimSpace(message.EventID),
		Source:  firstNonEmpty(strings.TrimSpace(message.Source), strings.TrimSpace(transport), "go_voice_gateway"),
		BotID:   strings.TrimSpace(message.BotID),
	}
	envelope, err := newInternalVoiceEnvelope(internalVoiceTypeClientEvent, s.sessionID, payload)
	if err != nil {
		s.client.metrics.clientEventFailures.Add(1)
		return err
	}
	envelope.TraceID = traceIDForVoiceTurn(
		message.TraceID,
		s.sessionID,
		firstNonEmpty(message.EventID, message.RoundID, message.PlaybackID, event),
	)

	if err := s.sendEnvelopeExpectingStatus(ctx, envelope, "client_event"); err != nil {
		s.client.metrics.clientEventFailures.Add(1)
		return err
	}
	s.client.metrics.clientEventSuccesses.Add(1)
	if s.client.logger != nil {
		s.client.logger.Printf(
			"internal_voice_client_event_ack session=%s event=%s event_id=%s transport=%s",
			printable(s.sessionID),
			printable(event),
			printable(message.EventID),
			printable(transport),
		)
	}
	return nil
}

func (s *InternalVoiceSession) SendClientEventActive(
	ctx context.Context,
	message clientMessage,
	transport string,
	downlink PythonGatewayDownlinkForwarder,
) (pythonGatewayBridgeSummary, error) {
	var empty pythonGatewayBridgeSummary
	if s == nil || s.client == nil || s.conn == nil {
		return empty, fmt.Errorf("internal voice session is not open")
	}
	event := strings.TrimSpace(message.Event)
	if event == "" {
		return empty, fmt.Errorf("internal voice client_event is missing event")
	}
	s.client.metrics.clientEventAttempts.Add(1)

	payload := internalVoiceClientEventPayload{
		Event:   event,
		EventID: strings.TrimSpace(message.EventID),
		Source:  firstNonEmpty(strings.TrimSpace(message.Source), strings.TrimSpace(transport), "go_voice_gateway"),
		BotID:   strings.TrimSpace(message.BotID),
	}
	envelope, err := newInternalVoiceEnvelope(internalVoiceTypeClientEvent, s.sessionID, payload)
	if err != nil {
		s.client.metrics.clientEventFailures.Add(1)
		return empty, err
	}
	envelope.TraceID = traceIDForVoiceTurn(
		message.TraceID,
		s.sessionID,
		firstNonEmpty(message.EventID, message.RoundID, message.PlaybackID, event),
	)

	summary, err := s.sendClientEventActive(ctx, envelope, downlink)
	if err != nil {
		s.client.metrics.clientEventFailures.Add(1)
		return summary, err
	}
	s.client.metrics.clientEventSuccesses.Add(1)
	if s.client.logger != nil {
		s.client.logger.Printf(
			"internal_voice_client_event_active_done session=%s event=%s event_id=%s transport=%s text_messages=%d binary_messages=%d binary_bytes=%d first_text_ms=%d first_binary_ms=%d last_message_ms=%d",
			printable(s.sessionID),
			printable(event),
			printable(message.EventID),
			printable(transport),
			summary.TextMessages,
			summary.BinaryMessages,
			summary.BinaryBytes,
			summary.FirstTextMS,
			summary.FirstBinaryMS,
			summary.LastMessageMS,
		)
	}
	return summary, nil
}

func (s *InternalVoiceSession) SendInputAudioMetadata(ctx context.Context, message clientMessage, transport string, audio AudioConfig) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	utteranceID := strings.TrimSpace(message.UtteranceID)
	if utteranceID == "" {
		return fmt.Errorf("internal voice input_audio is missing utterance_id")
	}

	var (
		eventType string
		payload   any
	)
	switch message.Type {
	case "audio_start":
		eventType = internalVoiceTypeInputAudioStart
		payload = internalVoiceInputAudioStartPayload{
			Codec:        firstNonEmpty(strings.TrimSpace(audio.Codec), "opus"),
			PacketFormat: "rtp_opus",
			SampleRate:   int(audio.SampleRate),
			Channels:     int(audio.Channels),
			FrameMS:      int(audio.PtimeMS),
		}
	case "audio_end":
		eventType = internalVoiceTypeInputAudioEnd
		payload = internalVoiceInputAudioEndPayload{}
	case "audio_cancel":
		eventType = internalVoiceTypeInputAudioCancel
		payload = internalVoiceInputAudioCancelPayload{
			Reason:    strings.TrimSpace(message.Reason),
			Source:    strings.TrimSpace(message.Source),
			Transport: strings.TrimSpace(transport),
			Ownership: s.client.cfg.InputAudioMode,
		}
	default:
		return fmt.Errorf("unsupported internal voice input_audio message type: %s", message.Type)
	}

	s.client.metrics.inputAudioAttempts.Add(1)
	envelope, err := newInternalVoiceInputAudioEnvelope(eventType, s.sessionID, utteranceID, payload)
	if err != nil {
		s.client.metrics.inputAudioFailures.Add(1)
		return err
	}
	envelope.TraceID = traceIDForVoiceTurn(message.TraceID, s.sessionID, utteranceID)

	if err := s.sendEnvelopeExpectingStatus(ctx, envelope, eventType); err != nil {
		s.client.metrics.inputAudioFailures.Add(1)
		return err
	}
	s.client.metrics.inputAudioSuccesses.Add(1)
	if s.client.logger != nil {
		s.client.logger.Printf(
			"internal_voice_input_audio_ack session=%s type=%s utterance_id=%s transport=%s",
			printable(s.sessionID),
			printable(eventType),
			printable(utteranceID),
			printable(transport),
		)
	}
	return nil
}

func (s *InternalVoiceSession) SendInputAudioBatch(ctx context.Context, request ASRAudioRequest) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	if request.SessionID != "" && strings.TrimSpace(request.SessionID) != s.sessionID {
		return fmt.Errorf(
			"internal voice input audio session mismatch: expected=%s actual=%s",
			s.sessionID,
			strings.TrimSpace(request.SessionID),
		)
	}
	s.client.metrics.inputBatchAttempts.Add(1)
	frame, err := buildInternalVoiceInputAudioFrame(request, "")
	if err != nil {
		s.client.metrics.inputBatchFailures.Add(1)
		return err
	}
	envelope, err := newInternalVoiceInputAudioEnvelope(
		internalVoiceTypeInputAudioBatch,
		s.sessionID,
		request.UtteranceID,
		struct{}{},
	)
	if err != nil {
		s.client.metrics.inputBatchFailures.Add(1)
		return err
	}
	envelope.TraceID = traceIDForVoiceTurn(
		request.TraceID,
		s.sessionID,
		request.UtteranceID,
	)
	ctx, cancel := s.client.withTimeout(ctx)
	defer cancel()
	waiter, err := s.registerWaiter(
		internalVoiceWaiterStatus,
		internalVoiceTypeOrchestratorStatus,
		envelope,
	)
	if err != nil {
		s.client.metrics.inputBatchFailures.Add(1)
		return err
	}
	defer s.unregisterWaiter(waiter)
	if err := s.sendBinary(frame, internalVoiceTypeInputAudioBatch); err != nil {
		s.client.metrics.inputBatchFailures.Add(1)
		return err
	}
	if _, err := waitForInternalVoiceMessage(ctx, waiter); err != nil {
		s.client.metrics.inputBatchFailures.Add(1)
		return fmt.Errorf("internal voice input_audio.batch timeout or receive failure: %w", err)
	}
	s.client.metrics.inputBatchSuccesses.Add(1)
	return nil
}

func (s *InternalVoiceSession) SendInputAudioBegin(ctx context.Context, request ASRAudioRequest) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	payload := internalVoiceInputAudioStartPayload{
		Codec:        request.AudioEncoding,
		PacketFormat: request.PacketStreamFormat,
		SampleRate:   int(request.SampleRate),
		Channels:     int(request.Channels),
		FrameMS:      int(request.OpusFrameMS),
	}
	envelope, err := newInternalVoiceInputAudioEnvelope(
		internalVoiceTypeInputAudioStart,
		s.sessionID,
		request.UtteranceID,
		payload,
	)
	if err != nil {
		return err
	}
	envelope.TraceID = traceIDForVoiceTurn(
		request.TraceID,
		s.sessionID,
		request.UtteranceID,
	)
	return s.sendEnvelopeExpectingStatus(ctx, envelope, internalVoiceTypeInputAudioStart)
}

func (s *InternalVoiceSession) SendInputAudioCommit(ctx context.Context, request ASRAudioRequest) error {
	envelope, err := s.newInputAudioCommitEnvelope(request, "shadow")
	if err != nil {
		return err
	}
	return s.sendEnvelopeExpectingStatus(ctx, envelope, internalVoiceTypeInputAudioEnd)
}

func (s *InternalVoiceSession) SendInputAudioActive(
	ctx context.Context,
	request ASRAudioRequest,
	downlink PythonGatewayDownlinkForwarder,
) (pythonGatewayBridgeSummary, error) {
	var summary pythonGatewayBridgeSummary
	if err := s.SendInputAudioBegin(ctx, request); err != nil {
		return summary, err
	}
	if err := s.SendInputAudioBatch(ctx, request); err != nil {
		return summary, err
	}
	envelope, err := s.newInputAudioCommitEnvelope(request, "active")
	if err != nil {
		return summary, err
	}
	turnWaiter, err := s.registerWaiter(
		internalVoiceWaiterTurn,
		internalVoiceTypeResponseDone,
		envelope,
	)
	if err != nil {
		return summary, err
	}
	defer s.unregisterWaiter(turnWaiter)
	statusWaiter, err := s.registerWaiter(
		internalVoiceWaiterStatus,
		internalVoiceTypeOrchestratorStatus,
		envelope,
	)
	if err != nil {
		return summary, err
	}
	defer s.unregisterWaiter(statusWaiter)
	if err := s.currentReaderError(); err != nil {
		return summary, err
	}
	summary.RequestCommitted = true
	if err := s.sendEnvelope(envelope, "active input_audio.end"); err != nil {
		return summary, err
	}
	acceptCtx, cancel := s.client.withTimeout(ctx)
	_, err = waitForInternalVoiceMessage(acceptCtx, statusWaiter)
	cancel()
	if err != nil {
		return summary, fmt.Errorf(
			"internal voice active input_audio.end acceptance unknown: %w",
			err,
		)
	}
	summary.Accepted = true
	activeSummary, err := s.receiveActiveTurn(ctx, turnWaiter, downlink)
	activeSummary.RequestCommitted = true
	activeSummary.Accepted = true
	return activeSummary, err
}

func (s *InternalVoiceSession) SendInputTextActive(
	ctx context.Context,
	message clientMessage,
	downlink PythonGatewayDownlinkForwarder,
) (pythonGatewayBridgeSummary, error) {
	var summary pythonGatewayBridgeSummary
	if s == nil || s.client == nil || s.conn == nil {
		return summary, fmt.Errorf("internal voice session is not open")
	}
	content := strings.TrimSpace(message.Content)
	if content == "" {
		return summary, fmt.Errorf("internal voice input_text.commit content is required")
	}
	utteranceID := strings.TrimSpace(message.UtteranceID)
	if utteranceID == "" {
		return summary, fmt.Errorf("internal voice input_text.commit utterance_id is required")
	}
	payload := internalVoiceInputTextCommitPayload{
		Content:        content,
		Source:         firstNonEmpty(strings.TrimSpace(message.Source), "go_voice_gateway"),
		BotID:          strings.TrimSpace(message.BotID),
		ASRTimeMS:      message.ASRTimeMS,
		CandidateSeq:   message.CandidateSeq,
		SpeechEpoch:    message.SpeechEpoch,
		AudioWatermark: message.AudioWatermark,
	}
	envelope, err := newInternalVoiceInputAudioEnvelope(
		internalVoiceTypeInputTextCommit,
		s.sessionID,
		utteranceID,
		payload,
	)
	if err != nil {
		return summary, err
	}
	envelope.TraceID = traceIDForVoiceTurn(message.TraceID, s.sessionID, utteranceID)

	waiter, err := s.registerWaiter(
		internalVoiceWaiterTurn,
		internalVoiceTypeResponseDone,
		envelope,
	)
	if err != nil {
		return summary, err
	}
	defer s.unregisterWaiter(waiter)
	statusWaiter, err := s.registerWaiter(
		internalVoiceWaiterStatus,
		internalVoiceTypeOrchestratorStatus,
		envelope,
	)
	if err != nil {
		return summary, err
	}
	defer s.unregisterWaiter(statusWaiter)
	if err := s.currentReaderError(); err != nil {
		return summary, err
	}
	summary.RequestCommitted = true
	if err := s.sendEnvelope(envelope, internalVoiceTypeInputTextCommit); err != nil {
		return summary, err
	}
	acceptCtx, cancel := s.client.withTimeout(ctx)
	_, err = waitForInternalVoiceMessage(acceptCtx, statusWaiter)
	cancel()
	if err != nil {
		return summary, fmt.Errorf("internal voice input_text.commit acceptance unknown: %w", err)
	}
	summary.Accepted = true
	activeSummary, err := s.receiveActiveTurn(ctx, waiter, downlink)
	activeSummary.RequestCommitted = true
	activeSummary.Accepted = true
	return activeSummary, err
}

func (s *InternalVoiceSession) newInputAudioCommitEnvelope(
	request ASRAudioRequest,
	orchestrationMode string,
) (internalVoiceEnvelope, error) {
	if s == nil || s.client == nil || s.conn == nil {
		return internalVoiceEnvelope{}, fmt.Errorf("internal voice session is not open")
	}
	payload := internalVoiceInputAudioEndPayload{
		PacketCount:          request.PacketCount,
		PayloadBytes:         len(request.AudioBytes),
		DurationMS:           int64(request.PacketCount) * int64(request.OpusFrameMS),
		Lossy:                request.Lossy,
		DroppedPackets:       int(request.DroppedEncodedPackets),
		DuplicatePackets:     int(request.DuplicatePackets),
		SequenceGaps:         int(request.SequenceGaps),
		TimestampRegressions: int(request.TimestampRegressions),
		OrchestrationMode:    strings.TrimSpace(orchestrationMode),
	}
	envelope, err := newInternalVoiceInputAudioEnvelope(
		internalVoiceTypeInputAudioEnd,
		s.sessionID,
		request.UtteranceID,
		payload,
	)
	if err != nil {
		return internalVoiceEnvelope{}, err
	}
	envelope.TraceID = traceIDForVoiceTurn(
		request.TraceID,
		s.sessionID,
		request.UtteranceID,
	)
	return envelope, nil
}

func (s *InternalVoiceSession) SendInterrupt(ctx context.Context, message clientMessage, transport string) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	s.client.metrics.interruptAttempts.Add(1)
	payload := internalVoiceInterruptPayload{
		Reason:      strings.TrimSpace(message.Reason),
		Source:      strings.TrimSpace(message.Source),
		Transport:   strings.TrimSpace(transport),
		UtteranceID: strings.TrimSpace(message.UtteranceID),
		Ownership:   s.client.cfg.InputAudioMode,
	}
	envelope, err := newInternalVoiceEnvelope(internalVoiceTypeInterrupt, s.sessionID, payload)
	if err != nil {
		s.client.metrics.interruptFailures.Add(1)
		return err
	}
	envelope.UtteranceID = strings.TrimSpace(message.UtteranceID)
	envelope.TraceID = traceIDForVoiceTurn(
		message.TraceID,
		s.sessionID,
		firstNonEmpty(message.UtteranceID, message.RoundID, message.PlaybackID, "interrupt"),
	)

	if err := s.sendEnvelopeExpectingStatus(ctx, envelope, internalVoiceTypeInterrupt); err != nil {
		s.client.metrics.interruptFailures.Add(1)
		return err
	}
	s.client.metrics.interruptSuccesses.Add(1)
	if s.client.logger != nil {
		s.client.logger.Printf(
			"internal_voice_interrupt_ack session=%s reason=%s utterance_id=%s transport=%s",
			printable(s.sessionID),
			printable(message.Reason),
			printable(message.UtteranceID),
			printable(transport),
		)
	}
	return nil
}

func (s *InternalVoiceSession) SendPlaybackReport(ctx context.Context, message clientMessage, transport string) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	reportType := strings.TrimSpace(message.Type)
	if reportType == "" {
		return fmt.Errorf("internal voice playback.report is missing report type")
	}
	s.client.metrics.playbackAttempts.Add(1)
	payload := internalVoicePlaybackReportPayload{
		ReportType:                  reportType,
		Reason:                      strings.TrimSpace(message.Reason),
		FirstAudioToPlaybackStartMS: message.FirstAudioToPlaybackStartMS,
		PlaybackStartToCompleteMS:   message.PlaybackStartToCompleteMS,
		PushedChunks:                message.PushedChunks,
		PushedSamples:               message.PushedSamples,
		UnderrunCallbacks:           message.UnderrunCallbacks,
		ZeroFilledSamples:           message.ZeroFilledSamples,
		MaxBufferedSamples:          message.MaxBufferedSamples,
		Ownership:                   s.client.cfg.InputAudioMode,
	}
	envelope, err := newInternalVoiceEnvelope(internalVoiceTypePlaybackReport, s.sessionID, payload)
	if err != nil {
		s.client.metrics.playbackFailures.Add(1)
		return err
	}
	envelope.RoundID = strings.TrimSpace(message.RoundID)
	envelope.PlaybackID = strings.TrimSpace(message.PlaybackID)
	envelope.TraceID = traceIDForVoiceTurn(
		message.TraceID,
		s.sessionID,
		firstNonEmpty(message.RoundID, message.PlaybackID, reportType),
	)

	if err := s.sendEnvelopeExpectingStatus(ctx, envelope, internalVoiceTypePlaybackReport); err != nil {
		s.client.metrics.playbackFailures.Add(1)
		return err
	}
	s.client.metrics.playbackSuccesses.Add(1)
	if s.client.logger != nil {
		s.client.logger.Printf(
			"internal_voice_playback_report_ack session=%s report_type=%s round_id=%s playback_id=%s transport=%s",
			printable(s.sessionID),
			printable(reportType),
			printable(message.RoundID),
			printable(message.PlaybackID),
			printable(transport),
		)
	}
	return nil
}

func (s *InternalVoiceSession) sendEnvelopeExpectingStatus(ctx context.Context, envelope internalVoiceEnvelope, action string) error {
	ctx, cancel := s.client.withTimeout(ctx)
	defer cancel()
	waiter, err := s.registerWaiter(
		internalVoiceWaiterStatus,
		internalVoiceTypeOrchestratorStatus,
		envelope,
	)
	if err != nil {
		return err
	}
	defer s.unregisterWaiter(waiter)
	if err := s.sendEnvelope(envelope, action); err != nil {
		return err
	}
	if _, err := waitForInternalVoiceMessage(ctx, waiter); err != nil {
		return fmt.Errorf("internal voice %s timeout or receive failure: %w", action, err)
	}
	return nil
}

func (s *InternalVoiceSession) sendClientEventActive(
	ctx context.Context,
	envelope internalVoiceEnvelope,
	downlink PythonGatewayDownlinkForwarder,
) (pythonGatewayBridgeSummary, error) {
	if s.client.cfg.ClientEventTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, s.client.cfg.ClientEventTimeout)
		defer cancel()
	}

	waiter, err := s.registerWaiter(
		internalVoiceWaiterTurn,
		internalVoiceTypeResponseDone,
		envelope,
	)
	if err != nil {
		return pythonGatewayBridgeSummary{}, err
	}
	defer s.unregisterWaiter(waiter)
	statusWaiter, err := s.registerWaiter(
		internalVoiceWaiterStatus,
		internalVoiceTypeOrchestratorStatus,
		envelope,
	)
	if err != nil {
		return pythonGatewayBridgeSummary{}, err
	}
	defer s.unregisterWaiter(statusWaiter)
	var summary pythonGatewayBridgeSummary
	if err := s.currentReaderError(); err != nil {
		return summary, err
	}
	summary.RequestCommitted = true
	if err := s.sendEnvelope(envelope, "active client_event"); err != nil {
		return summary, err
	}
	acceptCtx, acceptCancel := s.client.withTimeout(ctx)
	_, err = waitForInternalVoiceMessage(acceptCtx, statusWaiter)
	acceptCancel()
	if err != nil {
		return summary, fmt.Errorf(
			"internal voice active client_event acceptance unknown: %w",
			err,
		)
	}
	summary.Accepted = true
	activeSummary, err := s.receiveActiveTurn(ctx, waiter, downlink)
	activeSummary.RequestCommitted = true
	activeSummary.Accepted = true
	return activeSummary, err
}

func (s *InternalVoiceSession) receiveActiveTurn(
	ctx context.Context,
	waiter *internalVoiceWaiter,
	downlink PythonGatewayDownlinkForwarder,
) (pythonGatewayBridgeSummary, error) {
	var summary pythonGatewayBridgeSummary
	startedAt := time.Now()
	for {
		message, err := waitForInternalVoiceMessage(ctx, waiter)
		if err != nil {
			return summary, fmt.Errorf("receive internal voice active turn: %w", err)
		}
		elapsedMS := time.Since(startedAt).Milliseconds()
		summary.LastMessageMS = elapsedMS

		if message.IsJSON && isInternalVoiceControlMessageType(message.Type) {
			envelope, err := internalVoiceEnvelopeFromBridgeMessage(message)
			if err != nil {
				return summary, err
			}
			switch envelope.Type {
			case internalVoiceTypeProtocolError:
				return summary, internalVoiceProtocolError(envelope)
			case internalVoiceTypeResponseASR:
				if downlink != nil {
					if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, message); err != nil {
						return summary, fmt.Errorf(
							"forward internal voice response.asr failed session=%s: %w",
							printable(s.sessionID),
							err,
						)
					}
				}
				if summary.TextMessages == 0 {
					summary.FirstTextMS = elapsedMS
				}
				summary.TextMessages++
				summary.LastType = envelope.Type
				continue
			case internalVoiceTypeResponseDone:
				return summary, nil
			case internalVoiceTypeResponseCancelled:
				if downlink != nil {
					cancelMessage, err := internalVoiceCancellationDownlink(envelope)
					if err != nil {
						return summary, err
					}
					if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, cancelMessage); err != nil {
						return summary, fmt.Errorf(
							"forward internal voice response.cancelled failed session=%s: %w",
							printable(s.sessionID),
							err,
						)
					}
					if summary.TextMessages == 0 {
						summary.FirstTextMS = elapsedMS
					}
					summary.TextMessages++
					summary.LastType = "playback_cancel"
				}
				return summary, nil
			case internalVoiceTypeResponseError:
				if downlink != nil {
					if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, message); err != nil {
						return summary, fmt.Errorf(
							"forward internal voice response.error failed session=%s: %w",
							printable(s.sessionID),
							err,
						)
					}
					cancelMessage, convertErr := internalVoiceErrorDownlink(envelope)
					if convertErr != nil {
						return summary, convertErr
					}
					if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, cancelMessage); err != nil {
						return summary, fmt.Errorf(
							"forward internal voice response.error terminal cancel failed session=%s: %w",
							printable(s.sessionID),
							err,
						)
					}
					if summary.TextMessages == 0 {
						summary.FirstTextMS = elapsedMS
					}
					summary.TextMessages++
					summary.LastType = "playback_cancel"
				}
				return summary, internalVoiceResponseError(envelope)
			default:
				continue
			}
		}

		if message.IsJSON {
			if summary.TextMessages == 0 {
				summary.FirstTextMS = elapsedMS
			}
			summary.TextMessages++
			summary.LastType = message.Type
			if message.Type == "error" {
				if downlink != nil {
					if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, message); err != nil {
						return summary, fmt.Errorf(
							"forward internal voice active error downlink failed session=%s payload_bytes=%d elapsed_ms=%d: %w",
							printable(s.sessionID),
							len(message.Payload),
							elapsedMS,
							err,
						)
					}
				}
				continue
			}
		}

		if downlink != nil {
			if err := downlink.ForwardPythonGatewayDownlink(s.sessionID, message); err != nil {
				return summary, fmt.Errorf(
					"forward internal voice active downlink failed session=%s type=%s json=%t binary=%t payload_bytes=%d elapsed_ms=%d: %w",
					printable(s.sessionID),
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
	}
}

func internalVoiceCancellationDownlink(envelope internalVoiceEnvelope) (pythonGatewayBridgeMessage, error) {
	var payload internalVoiceResponseCancelledPayload
	if err := envelope.PayloadInto(&payload); err != nil {
		return pythonGatewayBridgeMessage{}, fmt.Errorf("decode internal voice response.cancelled payload: %w", err)
	}
	message := map[string]any{
		"type":        "playback_cancel",
		"trace_id":    strings.TrimSpace(envelope.TraceID),
		"round_id":    strings.TrimSpace(envelope.RoundID),
		"playback_id": strings.TrimSpace(envelope.PlaybackID),
		"reason":      firstNonEmpty(strings.TrimSpace(payload.Reason), "response_cancelled"),
	}
	raw, err := json.Marshal(message)
	if err != nil {
		return pythonGatewayBridgeMessage{}, fmt.Errorf("encode internal voice playback_cancel downlink: %w", err)
	}
	return pythonGatewayBridgeMessage{
		Type:    "playback_cancel",
		JSON:    message,
		IsJSON:  true,
		Payload: raw,
	}, nil
}

func internalVoiceErrorDownlink(envelope internalVoiceEnvelope) (pythonGatewayBridgeMessage, error) {
	var payload internalVoiceResponseErrorPayload
	if err := envelope.PayloadInto(&payload); err != nil {
		return pythonGatewayBridgeMessage{}, fmt.Errorf("decode internal voice response.error payload: %w", err)
	}
	reason := "response_error"
	if code := strings.TrimSpace(payload.Code); code != "" {
		reason += ":" + code
	}
	message := map[string]any{
		"type":        "playback_cancel",
		"trace_id":    strings.TrimSpace(envelope.TraceID),
		"round_id":    strings.TrimSpace(envelope.RoundID),
		"playback_id": strings.TrimSpace(envelope.PlaybackID),
		"reason":      reason,
	}
	raw, err := json.Marshal(message)
	if err != nil {
		return pythonGatewayBridgeMessage{}, fmt.Errorf("encode internal voice response.error downlink: %w", err)
	}
	return pythonGatewayBridgeMessage{
		Type:    "playback_cancel",
		JSON:    message,
		IsJSON:  true,
		Payload: raw,
	}, nil
}

func (c *InternalVoiceClient) Status() map[string]any {
	if c == nil {
		return map[string]any{"enabled": false}
	}
	return map[string]any{
		"enabled":                 true,
		"client_event_mode":       c.cfg.ClientEventMode,
		"input_audio_mode":        c.cfg.InputAudioMode,
		"client_event_timeout_ms": c.cfg.ClientEventTimeout.Milliseconds(),
		"ws_url":                  printable(c.cfg.WSURL),
		"timeout_ms":              c.cfg.Timeout.Milliseconds(),
		"max_message_kb":          c.cfg.MaxMessageKB,
		"active_sessions":         c.metrics.activeSessions.Load(),
		"open_attempts":           c.metrics.openAttempts.Load(),
		"open_successes":          c.metrics.openSuccesses.Load(),
		"open_failures":           c.metrics.openFailures.Load(),
		"close_attempts":          c.metrics.closeAttempts.Load(),
		"close_successes":         c.metrics.closeSuccesses.Load(),
		"close_failures":          c.metrics.closeFailures.Load(),
		"client_event_attempts":   c.metrics.clientEventAttempts.Load(),
		"client_event_successes":  c.metrics.clientEventSuccesses.Load(),
		"client_event_failures":   c.metrics.clientEventFailures.Load(),
		"input_audio_attempts":    c.metrics.inputAudioAttempts.Load(),
		"input_audio_successes":   c.metrics.inputAudioSuccesses.Load(),
		"input_audio_failures":    c.metrics.inputAudioFailures.Load(),
		"input_batch_attempts":    c.metrics.inputBatchAttempts.Load(),
		"input_batch_successes":   c.metrics.inputBatchSuccesses.Load(),
		"input_batch_failures":    c.metrics.inputBatchFailures.Load(),
		"interrupt_attempts":      c.metrics.interruptAttempts.Load(),
		"interrupt_successes":     c.metrics.interruptSuccesses.Load(),
		"interrupt_failures":      c.metrics.interruptFailures.Load(),
		"playback_attempts":       c.metrics.playbackAttempts.Load(),
		"playback_successes":      c.metrics.playbackSuccesses.Load(),
		"playback_failures":       c.metrics.playbackFailures.Load(),
		"heartbeat_attempts":      c.metrics.heartbeatAttempts.Load(),
		"heartbeat_successes":     c.metrics.heartbeatSuccesses.Load(),
		"heartbeat_failures":      c.metrics.heartbeatFailures.Load(),
		"heartbeat_dead":          c.metrics.heartbeatDead.Load(),
	}
}

func (c *InternalVoiceClient) receiveExpected(conn *websocket.Conn, expectedType string, sessionID string) error {
	for {
		c.setDeadline(conn)
		var envelope internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &envelope); err != nil {
			return fmt.Errorf("receive internal voice %s: %w", expectedType, err)
		}
		if err := envelope.Validate(); err != nil {
			return fmt.Errorf("receive internal voice %s: %w", expectedType, err)
		}
		if envelope.Type == internalVoiceTypeProtocolError {
			return internalVoiceProtocolError(envelope)
		}
		if envelope.Type != expectedType {
			continue
		}
		if strings.TrimSpace(envelope.SessionID) != strings.TrimSpace(sessionID) {
			return fmt.Errorf("internal voice session mismatch: expected=%s actual=%s", sessionID, envelope.SessionID)
		}
		return nil
	}
}

func internalVoiceEnvelopeFromBridgeMessage(message pythonGatewayBridgeMessage) (internalVoiceEnvelope, error) {
	var envelope internalVoiceEnvelope
	if !message.IsJSON {
		return envelope, fmt.Errorf("internal voice control message is not JSON")
	}
	if err := json.Unmarshal(message.Payload, &envelope); err != nil {
		return envelope, fmt.Errorf("decode internal voice control message: %w", err)
	}
	if err := envelope.Validate(); err != nil {
		return envelope, fmt.Errorf("receive internal voice control message: %w", err)
	}
	return envelope, nil
}

func isInternalVoiceControlMessageType(messageType string) bool {
	switch strings.TrimSpace(messageType) {
	case internalVoiceTypeSessionOpened,
		internalVoiceTypeSessionClosed,
		internalVoiceTypeHeartbeatPong,
		internalVoiceTypeOrchestratorStatus,
		internalVoiceTypeProtocolError,
		internalVoiceTypeResponseASR,
		internalVoiceTypeResponseAudio,
		internalVoiceTypeResponseDone,
		internalVoiceTypeResponseCancelled,
		internalVoiceTypeResponseError,
		internalVoiceTypeTraceEvent:
		return true
	default:
		return false
	}
}

type internalVoiceBusinessError struct {
	payload internalVoiceResponseErrorPayload
}

func (e internalVoiceBusinessError) Error() string {
	return fmt.Sprintf(
		"internal voice response error: origin=%s stage=%s code=%s retryable=%t fatal=%t message=%s",
		printable(e.payload.Origin),
		printable(e.payload.Stage),
		printable(e.payload.Code),
		e.payload.Retryable,
		e.payload.Fatal,
		printable(e.payload.Message),
	)
}

func internalVoiceResponseError(envelope internalVoiceEnvelope) error {
	var payload internalVoiceResponseErrorPayload
	if err := envelope.PayloadInto(&payload); err != nil {
		return err
	}
	code := strings.TrimSpace(payload.Code)
	if code == "" {
		code = "M1_RESPONSE_ERROR"
	}
	message := strings.TrimSpace(payload.Message)
	if message == "" {
		message = "M1 response failed"
	}
	payload.Code = code
	payload.Message = message
	return internalVoiceBusinessError{payload: payload}
}

func internalVoiceProtocolError(envelope internalVoiceEnvelope) error {
	var payload map[string]any
	_ = json.Unmarshal(envelope.Payload, &payload)
	code := stringValue(payload["code"])
	message := stringValue(payload["message"])
	if code == "" {
		code = "PROTOCOL_ERROR"
	}
	if message == "" {
		message = string(envelope.Payload)
	}
	return fmt.Errorf("internal voice protocol error: code=%s message=%s", code, message)
}

func (c *InternalVoiceClient) dial(ctx context.Context) (*websocket.Conn, error) {
	if c.dialer != nil {
		return c.dialer(ctx, c.cfg.WSURL)
	}
	location, err := parseInternalVoiceWSURL(c.cfg.WSURL)
	if err != nil {
		return nil, err
	}
	cfg, err := websocket.NewConfig(location.String(), internalVoiceOrigin(location))
	if err != nil {
		return nil, fmt.Errorf("create internal voice websocket config: %w", err)
	}
	return cfg.DialContext(ctx)
}

func (c *InternalVoiceClient) withTimeout(ctx context.Context) (context.Context, context.CancelFunc) {
	if ctx == nil {
		ctx = context.Background()
	}
	if c.cfg.Timeout <= 0 {
		return context.WithCancel(ctx)
	}
	return context.WithTimeout(ctx, c.cfg.Timeout)
}

func (c *InternalVoiceClient) setDeadline(conn *websocket.Conn) {
	if conn != nil && c.cfg.Timeout > 0 {
		_ = conn.SetDeadline(time.Now().Add(c.cfg.Timeout))
	}
}

func (c *InternalVoiceClient) setWriteDeadline(conn *websocket.Conn) {
	if conn != nil && c.cfg.Timeout > 0 {
		_ = conn.SetWriteDeadline(time.Now().Add(c.cfg.Timeout))
	}
}

func (c *InternalVoiceClient) clearWriteDeadline(conn *websocket.Conn) {
	if conn != nil && c.cfg.Timeout > 0 {
		_ = conn.SetWriteDeadline(time.Time{})
	}
}

func (c *InternalVoiceClient) setClientEventDeadline(conn *websocket.Conn) {
	if conn != nil && c.cfg.ClientEventTimeout > 0 {
		_ = conn.SetDeadline(time.Now().Add(c.cfg.ClientEventTimeout))
	}
}

func parseInternalVoiceWSURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return nil, fmt.Errorf("parse internal voice websocket URL: %w", err)
	}
	if parsed.Scheme != "ws" && parsed.Scheme != "wss" {
		return nil, fmt.Errorf("internal voice websocket URL must use ws or wss: %s", raw)
	}
	if parsed.Host == "" {
		return nil, fmt.Errorf("internal voice websocket URL host is empty")
	}
	return parsed, nil
}

func internalVoiceOrigin(location *url.URL) string {
	scheme := "http"
	if location.Scheme == "wss" {
		scheme = "https"
	}
	return (&url.URL{Scheme: scheme, Host: location.Host}).String()
}
