package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"
)

type voiceEventStatusProvider interface {
	VoiceEventStatus() map[string]any
}

type bridgeHealthStatusProvider interface {
	BridgeHealthStatus() map[string]any
}

type ClientEventProcessor interface {
	ProcessClientEvent(sessionID string, message clientMessage) error
}

type ClientTextProcessor interface {
	ProcessClientText(sessionID string, message clientMessage) error
}

type ClientPlaybackReportProcessor interface {
	ProcessPlaybackReport(sessionID string, message clientMessage) error
}

type ClientControlProcessor interface {
	ProcessClientControl(sessionID string, message clientMessage) error
}

type BargeInCommitProcessor interface {
	ClientControlProcessor
	ClientTextProcessor
}

type ActiveClientControlProcessor interface {
	ProcessClientControlDuringActive(sessionID string, message clientMessage) (bool, error)
}

type ClientSessionCloser interface {
	CloseClientSession(sessionID string)
}

type configuredASRVoiceEventSink struct {
	prototype       *ASRPrototypeSink
	handoffQueue    *QueuedASREncodedAudioHandoffSink
	candidateQueue  *QueuedASREncodedAudioHandoffSink
	candidateBridge *PythonGatewayBridgeProcessor
	bargeInQueue    *QueuedASREncodedAudioHandoffSink
	bargeInBridge   *PythonGatewayBridgeProcessor
	bargeIn         *BargeInRuntimeController
	turnGate        *TurnGateRuntimeController
	primaryBridge   BargeInCommitProcessor
	logger          *log.Logger
	processor       string
}

func shouldSendTurnContinueDecision(result TurnCandidateResult) bool {
	return !result.Shadow && result.Status == "observed" && result.PolicyPreview == "continue"
}

func newConfiguredClientEventProcessor(cfg Config, logger *log.Logger, downlink PythonGatewayDownlinkForwarder, sharedBridge *PythonGatewayBridgeProcessor) (ClientEventProcessor, error) {
	if cfg.ASR.Processor != asrProcessorPythonGateway {
		return nil, nil
	}
	bridge := sharedBridge
	if bridge == nil {
		var err error
		bridge, err = NewPythonGatewayBridgeProcessorFromASRConfig(cfg.ASR, cfg.BotID, logger)
		if err != nil {
			return nil, err
		}
		bridge.downlink = downlink
	}
	return bridge, nil
}

func newConfiguredVoiceEventSink(cfg Config, logger *log.Logger, downlink PythonGatewayDownlinkForwarder, sharedBridge *PythonGatewayBridgeProcessor) (VoiceEventSink, func(), error) {
	processorName := cfg.ASR.Processor
	if processorName == "" && cfg.ASR.HandoffDryRunEnabled {
		processorName = asrProcessorDryRun
	}
	if processorName == "" {
		return nil, nil, nil
	}

	var processor ASRAudioRequestProcessor
	var primaryPythonBridge *PythonGatewayBridgeProcessor
	var commitProcessor BargeInCommitProcessor
	internalVoiceActive := false
	switch processorName {
	case asrProcessorDryRun:
		processor = dryRunASRAudioRequestProcessor{logger: logger}
	case asrProcessorPythonGateway:
		bridge := sharedBridge
		if bridge == nil {
			var err error
			bridge, err = NewPythonGatewayBridgeProcessorFromASRConfig(cfg.ASR, cfg.BotID, logger)
			if err != nil {
				return nil, nil, err
			}
			bridge.downlink = downlink
		}
		primaryPythonBridge = bridge
		commitProcessor = bridge
		processor = bridge
		if cfg.InternalVoice.Enabled && cfg.InternalVoice.InputAudioEnabled {
			if sessions, ok := downlink.(*gatewaySessionRegistry); ok {
				processor = internalVoiceAudioProcessor{
					m0:            processor,
					sessions:      sessions,
					downlink:      downlink,
					mode:          cfg.InternalVoice.InputAudioMode,
					timeout:       cfg.InternalVoice.Timeout,
					activeTimeout: time.Duration(maxInt(cfg.ASR.PythonGatewayTotalTimeoutMS, 0)) * time.Millisecond,
					logger:        logger,
				}
				if cfg.InternalVoice.InputAudioMode == "active" {
					internalVoiceActive = true
					commitProcessor = internalVoiceCommitProcessor{
						sessions: sessions,
						downlink: downlink,
						timeout:  time.Duration(maxInt(cfg.ASR.PythonGatewayTotalTimeoutMS, 0)) * time.Millisecond,
					}
				}
			}
		}
	default:
		return nil, nil, errUnsupportedASRProcessor(processorName)
	}

	handoffQueue := NewQueuedASREncodedAudioHandoffSink(
		cfg.ASR.HandoffQueueSize,
		NewASRAudioRequestProcessorAdapter(newASRBridgeErrorReportingProcessor(processor, downlink, logger)),
	)
	handoffQueue.SetLogger(logger)
	prototype := NewASRPrototypeSinkWithOptions(
		defaultASRPrototypeQueueSize,
		handoffQueue,
		time.Duration(maxInt(cfg.ASR.HandoffEndGraceMS, 0))*time.Millisecond,
	)
	prototype.SetLogger(logger)
	prototype.SetTurnCandidateSnapshotGrace(
		time.Duration(cfg.ASR.TurnGateShadowSnapshotGraceMS) * time.Millisecond,
	)
	var turnGate *TurnGateRuntimeController
	if cfg.ASR.TurnGateActiveEnabled {
		turnGate = NewTurnGateRuntimeController(time.Duration(cfg.ASR.TurnGateActiveDeadlineMS) * time.Millisecond)
	}
	var candidateQueue *QueuedASREncodedAudioHandoffSink
	var candidateBridge *PythonGatewayBridgeProcessor
	if cfg.ASR.TurnGateShadowEnabled && processorName == asrProcessorPythonGateway {
		candidateCfg := cfg.ASR
		if cfg.ASR.TurnGateActiveEnabled {
			candidateCfg.PythonGatewayConnectionMode = pythonGatewayConnectionModeSession
		} else {
			candidateCfg.PythonGatewayConnectionMode = pythonGatewayConnectionModePerTurn
		}
		candidateCfg.PythonGatewayTimeoutMS = cfg.ASR.TurnGateShadowTimeoutMS
		candidateCfg.PythonGatewayTotalTimeoutMS = cfg.ASR.TurnGateShadowTimeoutMS
		var err error
		candidateBridge, err = NewPythonGatewayBridgeProcessorFromASRConfig(candidateCfg, cfg.BotID, logger)
		if err != nil {
			return nil, nil, err
		}
		if sessions, ok := downlink.(*gatewaySessionRegistry); ok {
			candidateBridge.credentialResolver = sessions
		}
		candidateProcessor := ASRAudioRequestProcessorFunc(func(request ASRAudioRequest) error {
			if internalVoiceActive {
				request.ContextSessionID = request.SessionID
			} else if primaryPythonBridge != nil {
				request.ContextSessionID = primaryPythonBridge.pythonSessionIDForClientSession(request.SessionID)
			}
			result, err := candidateBridge.ProcessTurnCandidateAudioRequestWithResult(request)
			if err != nil {
				return err
			}
			if turnGate == nil || request.Shadow {
				return nil
			}
			sessions, ok := downlink.(*gatewaySessionRegistry)
			if !ok || sessions == nil || primaryPythonBridge == nil {
				return nil
			}
			session := sessions.Lookup(request.SessionID)
			if session == nil {
				return nil
			}
			requested, reason, commitErr := turnGate.ProposeIfCurrent(
				request.SessionID,
				result,
				time.Now(),
				func() error { return session.sendTurnCommitRequest(result) },
			)
			if logger != nil {
				logger.Printf(
					"turn_gate_active_proposal session=%s trace_id=%s utterance_id=%s candidate_seq=%d speech_epoch=%d audio_watermark=%d requested=%t reason=%s error=%v",
					printable(request.SessionID),
					printable(result.TraceID),
					printable(result.UtteranceID),
					result.CandidateSeq,
					result.SpeechEpoch,
					result.AudioWatermark,
					requested,
					reason,
					commitErr,
				)
			}
			if commitErr == nil && !requested && shouldSendTurnContinueDecision(result) {
				decisionErr := session.sendTurnCandidateDecision(result, "continue", "models_continue")
				if logger != nil {
					logger.Printf(
						"turn_gate_active_decision session=%s trace_id=%s utterance_id=%s candidate_seq=%d speech_epoch=%d decision=continue error=%v",
						printable(request.SessionID),
						printable(result.TraceID),
						printable(result.UtteranceID),
						result.CandidateSeq,
						result.SpeechEpoch,
						decisionErr,
					)
				}
				return decisionErr
			}
			if commitErr != nil || !requested {
				return commitErr
			}
			return nil
		})
		candidateQueue = NewQueuedASREncodedAudioHandoffSink(
			cfg.ASR.HandoffQueueSize,
			NewASRAudioRequestProcessorAdapter(candidateProcessor),
		)
		candidateQueue.SetLogger(logger)
		prototype.SetTurnCandidateHandoffSink(candidateQueue)
	}
	var bargeInQueue *QueuedASREncodedAudioHandoffSink
	var bargeInBridge *PythonGatewayBridgeProcessor
	var bargeIn *BargeInRuntimeController
	if cfg.ASR.BargeInEnabled && processorName == asrProcessorPythonGateway {
		bargeCfg := cfg.ASR
		bargeCfg.PythonGatewayConnectionMode = pythonGatewayConnectionModeSession
		bargeCfg.PythonGatewayTimeoutMS = 1000
		bargeCfg.PythonGatewayTotalTimeoutMS = 1000
		var err error
		bargeInBridge, err = NewPythonGatewayBridgeProcessorFromASRConfig(bargeCfg, cfg.BotID, logger)
		if err != nil {
			return nil, nil, err
		}
		if sessions, ok := downlink.(*gatewaySessionRegistry); ok {
			bargeInBridge.credentialResolver = sessions
		}
		bargeIn = NewBargeInRuntimeController()
		bargeProcessor := ASRAudioRequestProcessorFunc(func(request ASRAudioRequest) error {
			result, err := bargeInBridge.ProcessBargeInAudioRequestWithResult(request)
			if err != nil {
				return err
			}
			sessions, ok := downlink.(*gatewaySessionRegistry)
			if !ok || sessions == nil {
				return nil
			}
			session := sessions.Lookup(request.SessionID)
			if session == nil {
				return nil
			}
			if result.Decision != "ignore" && !bargeIn.Remember(request.SessionID, result) {
				return nil
			}
			if err := session.sendBargeInDecision(result); err != nil {
				if result.Decision != "ignore" {
					bargeIn.Clear(request.SessionID)
				}
				return err
			}
			return nil
		})
		bargeInQueue = NewConcurrentASREncodedAudioHandoffSink(
			cfg.ASR.HandoffQueueSize,
			4,
			NewASRAudioRequestProcessorAdapter(bargeProcessor),
		)
		bargeInQueue.SetLogger(logger)
		prototype.SetBargeInHandoffSink(bargeInQueue)
	}
	sink := &configuredASRVoiceEventSink{
		prototype:       prototype,
		handoffQueue:    handoffQueue,
		candidateQueue:  candidateQueue,
		candidateBridge: candidateBridge,
		bargeInQueue:    bargeInQueue,
		bargeInBridge:   bargeInBridge,
		bargeIn:         bargeIn,
		turnGate:        turnGate,
		primaryBridge:   commitProcessor,
		logger:          logger,
		processor:       processorName,
	}
	return sink, sink.Close, nil
}

type internalVoiceAudioProcessor struct {
	m0            ASRAudioRequestProcessor
	sessions      *gatewaySessionRegistry
	downlink      PythonGatewayDownlinkForwarder
	mode          string
	timeout       time.Duration
	activeTimeout time.Duration
	logger        *log.Logger
}

type internalVoiceCommitProcessor struct {
	sessions *gatewaySessionRegistry
	downlink PythonGatewayDownlinkForwarder
	timeout  time.Duration
}

func (p internalVoiceCommitProcessor) activeSession(sessionID string) (*InternalVoiceSession, error) {
	if p.sessions == nil {
		return nil, fmt.Errorf("internal voice session registry is unavailable")
	}
	gatewaySession := p.sessions.Lookup(sessionID)
	if gatewaySession == nil {
		return nil, fmt.Errorf("gateway session not found: %s", printable(sessionID))
	}
	openCtx, cancel := context.WithTimeout(context.Background(), boundedInternalVoiceLazyOpenTimeout(p.timeout))
	session := gatewaySession.ensureInternalVoiceSession(openCtx, "input_text_active")
	cancel()
	if session == nil {
		return nil, fmt.Errorf("internal voice session open failed: %s", printable(sessionID))
	}
	return session, nil
}

func (p internalVoiceCommitProcessor) ProcessClientText(sessionID string, message clientMessage) error {
	session, err := p.activeSession(sessionID)
	if err != nil {
		return err
	}
	ctx := context.Background()
	cancel := func() {}
	if p.timeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, p.timeout)
	}
	defer cancel()
	_, err = session.SendInputTextActive(ctx, message, p.downlink)
	return err
}

func (p internalVoiceCommitProcessor) ProcessClientControl(sessionID string, message clientMessage) error {
	session, err := p.activeSession(sessionID)
	if err != nil {
		return err
	}
	ctx := context.Background()
	cancel := func() {}
	if p.timeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, p.timeout)
	}
	defer cancel()
	return session.SendInterrupt(ctx, message, "turn_candidate")
}

func (p internalVoiceAudioProcessor) ProcessASRAudioRequest(request ASRAudioRequest) error {
	if p.mode == "active" {
		return p.processActive(request)
	}
	if p.sessions != nil {
		shadowRequest := request
		shadowRequest.AudioBytes = append([]byte(nil), request.AudioBytes...)
		go p.sendShadow(shadowRequest)
	}
	if p.m0 == nil {
		return nil
	}
	return p.m0.ProcessASRAudioRequest(request)
}

func (p internalVoiceAudioProcessor) processActive(request ASRAudioRequest) error {
	if p.sessions == nil {
		return p.processM0(request)
	}
	gatewaySession := p.sessions.Lookup(request.SessionID)
	if gatewaySession == nil {
		return p.processM0(request)
	}
	openTimeout := boundedInternalVoiceLazyOpenTimeout(p.timeout)
	openCtx, openCancel := context.WithTimeout(context.Background(), openTimeout)
	session := gatewaySession.ensureInternalVoiceSession(openCtx, "input_audio_active")
	openCancel()
	if session == nil {
		return p.processM0(request)
	}
	ctx := context.Background()
	cancel := func() {}
	if p.activeTimeout > 0 {
		ctx, cancel = context.WithTimeout(ctx, p.activeTimeout)
	}
	summary, err := session.SendInputAudioActive(ctx, request, p.downlink)
	cancel()
	if err == nil {
		return nil
	}
	if p.logger != nil {
		p.logger.Printf(
			"internal_voice_input_audio_active_failed session=%s trace_id=%s utterance_id=%s committed=%t accepted=%t text_messages=%d binary_messages=%d error=%v",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			summary.RequestCommitted,
			summary.Accepted,
			summary.TextMessages,
			summary.BinaryMessages,
			err,
		)
	}
	if summary.RequestCommitted {
		return err
	}
	gatewaySession.resetInternalVoiceSession("input_audio_active_precommit_failed")
	if p.logger != nil {
		p.logger.Printf(
			"internal_voice_input_audio_active_fallback_to_m0 session=%s trace_id=%s utterance_id=%s",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
		)
	}
	return p.processM0(request)
}

func (p internalVoiceAudioProcessor) processM0(request ASRAudioRequest) error {
	if p.m0 == nil {
		return nil
	}
	return p.m0.ProcessASRAudioRequest(request)
}

func (p internalVoiceAudioProcessor) sendShadow(request ASRAudioRequest) {
	gatewaySession := p.sessions.Lookup(request.SessionID)
	if gatewaySession == nil {
		return
	}
	openTimeout := boundedInternalVoiceLazyOpenTimeout(p.timeout)
	ctx, cancel := context.WithTimeout(context.Background(), openTimeout)
	session := gatewaySession.ensureInternalVoiceSession(ctx, "input_audio_shadow")
	cancel()
	if session == nil {
		return
	}
	beginCtx := context.Background()
	beginCancel := func() {}
	if p.timeout > 0 {
		beginCtx, beginCancel = context.WithTimeout(beginCtx, p.timeout)
	}
	err := session.SendInputAudioBegin(beginCtx, request)
	beginCancel()
	if err != nil {
		if p.logger != nil {
			p.logger.Printf(
				"internal_voice_input_audio_shadow_failed session=%s trace_id=%s utterance_id=%s stage=begin error=%v",
				printable(request.SessionID),
				printable(request.TraceID),
				printable(request.UtteranceID),
				err,
			)
		}
		return
	}
	batchCtx := context.Background()
	batchCancel := func() {}
	if p.timeout > 0 {
		batchCtx, batchCancel = context.WithTimeout(batchCtx, p.timeout)
	}
	err = session.SendInputAudioBatch(batchCtx, request)
	batchCancel()
	if err != nil {
		if p.logger != nil {
			p.logger.Printf(
				"internal_voice_input_audio_shadow_failed session=%s trace_id=%s utterance_id=%s stage=batch error=%v",
				printable(request.SessionID),
				printable(request.TraceID),
				printable(request.UtteranceID),
				err,
			)
		}
		return
	}
	commitCtx := context.Background()
	commitCancel := func() {}
	if p.timeout > 0 {
		commitCtx, commitCancel = context.WithTimeout(commitCtx, p.timeout)
	}
	err = session.SendInputAudioCommit(commitCtx, request)
	commitCancel()
	if err != nil && p.logger != nil {
		p.logger.Printf(
			"internal_voice_input_audio_shadow_failed session=%s trace_id=%s utterance_id=%s stage=commit error=%v",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			err,
		)
	}
}

func (s *configuredASRVoiceEventSink) HandleVoiceEvent(event CanonicalVoiceEvent) {
	if s.bargeIn != nil && s.bargeInBridge != nil && event.Type == "barge_in_start" {
		if !s.bargeIn.Start(event.SessionID, event) {
			if s.logger != nil {
				s.logger.Printf("barge_in_start_rejected session=%s utterance_id=%s playback_id=%s", printable(event.SessionID), printable(event.UtteranceID), printable(event.PlaybackID))
			}
			return
		}
		go func(sessionID string) {
			if err := s.bargeInBridge.WarmSession(sessionID); err != nil && s.logger != nil {
				s.logger.Printf("barge_in_candidate_session_warm_failed session=%s error=%v", printable(sessionID), err)
			}
		}(event.SessionID)
	}
	if s.bargeIn != nil && event.Type == "barge_in_commit_ack" {
		result, reason := s.bargeIn.Accept(event.SessionID, event)
		if s.logger != nil {
			s.logger.Printf(
				"barge_in_commit_ack session=%s utterance_id=%s playback_id=%s candidate_seq=%d accepted=%t reason=%s",
				printable(event.SessionID), printable(event.UtteranceID), printable(event.PlaybackID), event.CandidateSeq, event.Accepted, reason,
			)
		}
		if result != nil {
			s.commitBargeIn(event.SessionID, *result)
		}
		return
	}
	if s.bargeIn != nil && event.Type == "barge_in_cancel" {
		s.bargeIn.Clear(event.SessionID)
	}
	if s.turnGate != nil && s.candidateBridge != nil && event.Type == "audio_start" {
		go func(sessionID string) {
			if err := s.candidateBridge.WarmSession(sessionID); err != nil && s.logger != nil {
				s.logger.Printf(
					"turn_gate_active_candidate_session_warm_failed session=%s error=%v",
					printable(sessionID),
					err,
				)
			}
		}(event.SessionID)
	}
	if s.turnGate != nil && event.Type == "turn_commit_ack" {
		result, reason := s.turnGate.AcceptAck(event)
		if s.logger != nil {
			s.logger.Printf(
				"turn_gate_active_ack session=%s trace_id=%s utterance_id=%s candidate_seq=%d speech_epoch=%d audio_watermark=%d accepted=%t reason=%s",
				printable(event.SessionID),
				printable(event.TraceID),
				printable(event.UtteranceID),
				event.CandidateSeq,
				event.SpeechEpoch,
				event.AudioWatermark,
				event.Accepted,
				reason,
			)
		}
		if result != nil {
			s.prototype.HandleVoiceEvent(CanonicalVoiceEvent{
				Type:           "turn_commit",
				SessionID:      event.SessionID,
				TraceID:        result.TraceID,
				UtteranceID:    result.UtteranceID,
				CandidateSeq:   result.CandidateSeq,
				SpeechEpoch:    result.SpeechEpoch,
				AudioWatermark: result.AudioWatermark,
				Reason:         "turn_gate_active_commit",
				At:             time.Now().UTC(),
			})
			s.promoteTurnCandidate(event.SessionID, *result)
		}
		return
	}
	if s.turnGate != nil && s.turnGate.Observe(event) {
		return
	}
	s.prototype.HandleVoiceEvent(event)
}

func (s *configuredASRVoiceEventSink) CloseClientSession(sessionID string) {
	if s == nil {
		return
	}
	if s.candidateBridge != nil {
		s.candidateBridge.CloseClientSession(sessionID)
	}
	if s.bargeInBridge != nil {
		s.bargeInBridge.CloseClientSession(sessionID)
	}
	if s.bargeIn != nil {
		s.bargeIn.Clear(sessionID)
	}
}

func (s *configuredASRVoiceEventSink) promoteTurnCandidate(sessionID string, result TurnCandidateResult) {
	if s == nil || s.primaryBridge == nil {
		return
	}
	promoted := clientMessage{
		Type:           "text",
		Content:        result.ASRText,
		Source:         "turn_gate_candidate_asr",
		TraceID:        result.TraceID,
		UtteranceID:    result.UtteranceID,
		CandidateSeq:   result.CandidateSeq,
		SpeechEpoch:    result.SpeechEpoch,
		AudioWatermark: result.AudioWatermark,
		ASRTimeMS:      result.ASRTimeMS,
	}
	go func() {
		if err := s.primaryBridge.ProcessClientText(sessionID, promoted); err != nil && s.logger != nil {
			s.logger.Printf(
				"turn_gate_active_promotion_failed session=%s trace_id=%s utterance_id=%s error=%v",
				printable(sessionID),
				printable(result.TraceID),
				printable(result.UtteranceID),
				err,
			)
		}
	}()
}

func (s *configuredASRVoiceEventSink) commitBargeIn(sessionID string, result BargeInResult) {
	if s == nil || s.primaryBridge == nil {
		return
	}
	go func() {
		interrupt := clientMessage{
			Type: "interrupt", Reason: "natural_barge_in", TraceID: result.TraceID,
			UtteranceID: result.UtteranceID, RoundID: result.RoundID, PlaybackID: result.PlaybackID,
		}
		if err := s.primaryBridge.ProcessClientControl(sessionID, interrupt); err != nil {
			if s.logger != nil {
				s.logger.Printf(
					"barge_in_interrupt_bridge_failed session=%s trace_id=%s utterance_id=%s error=%v",
					printable(sessionID), printable(result.TraceID), printable(result.UtteranceID), err,
				)
			}
			return
		}
		if result.Decision != "new_intent" || strings.TrimSpace(result.ASRText) == "" {
			return
		}
		promoted := clientMessage{
			Type:           "text",
			Content:        result.ASRText,
			Source:         "barge_in_candidate_asr",
			TraceID:        result.TraceID,
			UtteranceID:    result.UtteranceID,
			CandidateSeq:   result.CandidateSeq,
			SpeechEpoch:    result.SpeechEpoch,
			AudioWatermark: result.AudioWatermark,
			ASRTimeMS:      result.ASRTimeMS,
		}
		if err := s.primaryBridge.ProcessClientText(sessionID, promoted); err != nil && s.logger != nil {
			s.logger.Printf(
				"barge_in_promotion_failed session=%s trace_id=%s utterance_id=%s error=%v",
				printable(sessionID), printable(result.TraceID), printable(result.UtteranceID), err,
			)
		}
	}()
}

func (s *configuredASRVoiceEventSink) Close() {
	s.prototype.Close()
	if s.candidateQueue != nil {
		s.candidateQueue.Close()
	}
	if s.candidateBridge != nil {
		s.candidateBridge.Close()
	}
	if s.bargeInQueue != nil {
		s.bargeInQueue.Close()
	}
	if s.bargeInBridge != nil {
		s.bargeInBridge.Close()
	}
	s.handoffQueue.Close()
}

func (s *configuredASRVoiceEventSink) VoiceEventStatus() map[string]any {
	status := map[string]any{
		"type":          "asr_prototype",
		"processor":     s.processor,
		"prototype":     s.prototype.Snapshot(),
		"handoff_queue": s.handoffQueue.Snapshot(),
	}
	if s.candidateQueue != nil {
		status["turn_candidate_queue"] = s.candidateQueue.Snapshot()
	}
	if s.bargeInQueue != nil {
		status["barge_in_queue"] = s.bargeInQueue.Snapshot()
	}
	return status
}

type dryRunASRAudioRequestProcessor struct {
	logger *log.Logger
}

func (p dryRunASRAudioRequestProcessor) ProcessASRAudioRequest(request ASRAudioRequest) error {
	if p.logger != nil {
		p.logger.Printf(
			"asr_audio_request_dry_run session=%s utterance_id=%s transport=%s encoding=%s sample_rate=%d channels=%d packets=%d payload_bytes=%d stream_bytes=%d lossy=%t dropped=%d duplicates=%d seq_gaps=%d missing=%d timestamp_regressions=%d",
			printable(request.SessionID),
			printable(request.UtteranceID),
			request.AudioTransport,
			request.AudioEncoding,
			request.SampleRate,
			request.Channels,
			request.PacketCount,
			request.RetainedPacketPayloadBytes,
			request.AudioByteCount,
			request.Lossy,
			request.DroppedEncodedPackets,
			request.DuplicatePackets,
			request.SequenceGaps,
			request.MissingPackets,
			request.TimestampRegressions,
		)
	}
	return nil
}

func errUnsupportedASRProcessor(processor string) error {
	return fmt.Errorf("unsupported ASR processor: %s", processor)
}

type asrBridgeErrorReportingProcessor struct {
	processor ASRAudioRequestProcessor
	downlink  PythonGatewayDownlinkForwarder
	logger    *log.Logger
}

func newASRBridgeErrorReportingProcessor(
	processor ASRAudioRequestProcessor,
	downlink PythonGatewayDownlinkForwarder,
	logger *log.Logger,
) ASRAudioRequestProcessor {
	if processor == nil || downlink == nil {
		return processor
	}
	return asrBridgeErrorReportingProcessor{
		processor: processor,
		downlink:  downlink,
		logger:    logger,
	}
}

func (p asrBridgeErrorReportingProcessor) ProcessASRAudioRequest(request ASRAudioRequest) error {
	err := p.processor.ProcessASRAudioRequest(request)
	if err == nil {
		return nil
	}
	var businessErr internalVoiceBusinessError
	if errors.As(err, &businessErr) {
		return err
	}
	if request.SessionID == "" {
		return err
	}

	message := fmt.Sprintf(
		"ASR bridge failed: trace_id=%s utterance_id=%s error=%v",
		printable(request.TraceID),
		printable(request.UtteranceID),
		err,
	)
	notifyErr := p.downlink.ForwardPythonGatewayDownlink(request.SessionID, pythonGatewayBridgeMessage{
		Type:   "error",
		IsJSON: true,
		JSON: map[string]any{
			"type":         "error",
			"code":         "ASR_BRIDGE_FAILED",
			"message":      message,
			"trace_id":     request.TraceID,
			"utterance_id": request.UtteranceID,
		},
	})
	if notifyErr != nil && p.logger != nil {
		p.logger.Printf(
			"asr_bridge_error_notify_failed session=%s trace_id=%s utterance_id=%s error=%v notify_error=%v",
			printable(request.SessionID),
			printable(request.TraceID),
			printable(request.UtteranceID),
			err,
			notifyErr,
		)
	}
	return err
}
