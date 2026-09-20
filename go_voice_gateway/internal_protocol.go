package main

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const internalVoiceProtocolVersion = 1

const (
	internalVoiceTypeSessionOpen        = "session.open"
	internalVoiceTypeSessionOpened      = "session.opened"
	internalVoiceTypeSessionClose       = "session.close"
	internalVoiceTypeSessionClosed      = "session.closed"
	internalVoiceTypeHeartbeatPing      = "heartbeat.ping"
	internalVoiceTypeHeartbeatPong      = "heartbeat.pong"
	internalVoiceTypeInputAudioStart    = "input_audio.start"
	internalVoiceTypeInputAudioBatch    = "input_audio.batch"
	internalVoiceTypeInputAudioEnd      = "input_audio.end"
	internalVoiceTypeInputAudioCancel   = "input_audio.cancel"
	internalVoiceTypeInputTextCommit    = "input_text.commit"
	internalVoiceTypeClientEvent        = "client_event"
	internalVoiceTypeInterrupt          = "interrupt"
	internalVoiceTypePlaybackReport     = "playback.report"
	internalVoiceTypeOrchestratorStatus = "orchestrator.status"
	internalVoiceTypeProtocolError      = "protocol.error"
	internalVoiceTypeResponseASR        = "response.asr"
	internalVoiceTypeResponseAudio      = "response.audio"
	internalVoiceTypeResponseDone       = "response.done"
	internalVoiceTypeResponseCancelled  = "response.cancelled"
	internalVoiceTypeResponseError      = "response.error"
	internalVoiceTypeTraceEvent         = "trace.event"
)

type internalVoiceEnvelope struct {
	Version     int             `json:"version"`
	Type        string          `json:"type"`
	SessionID   string          `json:"session_id"`
	TraceID     string          `json:"trace_id,omitempty"`
	UtteranceID string          `json:"utterance_id,omitempty"`
	RoundID     string          `json:"round_id,omitempty"`
	PlaybackID  string          `json:"playback_id,omitempty"`
	TimestampMS int64           `json:"timestamp_ms"`
	Payload     json.RawMessage `json:"payload"`
}

type internalVoiceSessionOpenPayload struct {
	RobotID     string `json:"robot_id,omitempty"`
	RobotSecret string `json:"robot_secret,omitempty"`
	ClientType  string `json:"client_type,omitempty"`
	BotID       string `json:"bot_id,omitempty"`
	Source      string `json:"source,omitempty"`
	Transport   string `json:"transport,omitempty"`
	BridgeMode  string `json:"bridge_mode,omitempty"`
}

type internalVoiceInputAudioStartPayload struct {
	Codec        string `json:"codec"`
	PacketFormat string `json:"packet_format"`
	SampleRate   int    `json:"sample_rate"`
	Channels     int    `json:"channels"`
	FrameMS      int    `json:"frame_ms,omitempty"`
	ICERoute     string `json:"ice_route,omitempty"`
}

type internalVoiceInputAudioEndPayload struct {
	PacketCount          int    `json:"packet_count"`
	PayloadBytes         int    `json:"payload_bytes"`
	DurationMS           int64  `json:"duration_ms"`
	Lossy                bool   `json:"lossy,omitempty"`
	DroppedPackets       int    `json:"dropped_packets,omitempty"`
	DuplicatePackets     int    `json:"duplicate_packets,omitempty"`
	SequenceGaps         int    `json:"sequence_gaps,omitempty"`
	TimestampRegressions int    `json:"timestamp_regressions,omitempty"`
	OrchestrationMode    string `json:"orchestration_mode,omitempty"`
}

type internalVoiceInputAudioCancelPayload struct {
	Reason    string `json:"reason,omitempty"`
	Source    string `json:"source,omitempty"`
	Transport string `json:"transport,omitempty"`
	Ownership string `json:"ownership_mode,omitempty"`
}

type internalVoiceInputTextCommitPayload struct {
	Content        string  `json:"content"`
	Source         string  `json:"source,omitempty"`
	BotID          string  `json:"bot_id,omitempty"`
	ASRTimeMS      float64 `json:"asr_time_ms,omitempty"`
	CandidateSeq   uint64  `json:"candidate_seq,omitempty"`
	SpeechEpoch    uint64  `json:"speech_epoch,omitempty"`
	AudioWatermark uint64  `json:"audio_watermark,omitempty"`
}

type internalVoiceInterruptPayload struct {
	Reason      string `json:"reason,omitempty"`
	Source      string `json:"source,omitempty"`
	Transport   string `json:"transport,omitempty"`
	UtteranceID string `json:"utterance_id,omitempty"`
	Ownership   string `json:"ownership_mode,omitempty"`
}

type internalVoiceClientEventPayload struct {
	Event   string `json:"event"`
	EventID string `json:"event_id,omitempty"`
	Source  string `json:"source,omitempty"`
	BotID   string `json:"bot_id,omitempty"`
}

type internalVoicePlaybackReportPayload struct {
	ReportType                  string   `json:"report_type"`
	Reason                      string   `json:"reason,omitempty"`
	FirstAudioToPlaybackStartMS *float64 `json:"first_audio_to_playback_start_ms,omitempty"`
	PlaybackStartToCompleteMS   *float64 `json:"playback_start_to_complete_ms,omitempty"`
	PushedChunks                *uint64  `json:"pushed_chunks,omitempty"`
	PushedSamples               *uint64  `json:"pushed_samples,omitempty"`
	UnderrunCallbacks           *uint64  `json:"underrun_callbacks,omitempty"`
	ZeroFilledSamples           *uint64  `json:"zero_filled_samples,omitempty"`
	MaxBufferedSamples          *uint64  `json:"max_buffered_samples,omitempty"`
	Ownership                   string   `json:"ownership_mode,omitempty"`
}

type internalVoiceResponseAudioPayload struct {
	ChunkSeq     int    `json:"chunk_seq"`
	Codec        string `json:"codec"`
	SampleRate   int    `json:"sample_rate"`
	Channels     int    `json:"channels"`
	DurationMS   int64  `json:"duration_ms,omitempty"`
	IsFinal      bool   `json:"is_final,omitempty"`
	PayloadBytes int    `json:"payload_bytes,omitempty"`
}

type internalVoiceHeartbeatPayload struct {
	Nonce string `json:"nonce"`
}

type internalVoiceResponseDonePayload struct {
	Exit        bool   `json:"exit"`
	Reason      string `json:"reason,omitempty"`
	AudioChunks int    `json:"audio_chunks,omitempty"`
	AudioBytes  int    `json:"audio_bytes,omitempty"`
}

type internalVoiceResponseCancelledPayload struct {
	Reason         string `json:"reason"`
	CancelledStage string `json:"cancelled_stage,omitempty"`
	Expected       bool   `json:"expected"`
}

type internalVoiceResponseErrorPayload struct {
	Origin    string `json:"origin"`
	Stage     string `json:"stage"`
	Code      string `json:"code"`
	Message   string `json:"message"`
	CauseCode string `json:"cause_code,omitempty"`
	Retryable bool   `json:"retryable"`
	Fatal     bool   `json:"fatal"`
}

func newInternalVoiceEnvelope(eventType string, sessionID string, payload any) (internalVoiceEnvelope, error) {
	rawPayload, err := marshalInternalVoicePayload(payload)
	if err != nil {
		return internalVoiceEnvelope{}, err
	}
	envelope := internalVoiceEnvelope{
		Version:     internalVoiceProtocolVersion,
		Type:        strings.TrimSpace(eventType),
		SessionID:   strings.TrimSpace(sessionID),
		TimestampMS: internalVoiceNowMillis(),
		Payload:     rawPayload,
	}
	if err := envelope.Validate(); err != nil {
		return internalVoiceEnvelope{}, err
	}
	return envelope, nil
}

func newInternalVoiceInputAudioEnvelope(eventType string, sessionID string, utteranceID string, payload any) (internalVoiceEnvelope, error) {
	rawPayload, err := marshalInternalVoicePayload(payload)
	if err != nil {
		return internalVoiceEnvelope{}, err
	}
	envelope := internalVoiceEnvelope{
		Version:     internalVoiceProtocolVersion,
		Type:        strings.TrimSpace(eventType),
		SessionID:   strings.TrimSpace(sessionID),
		UtteranceID: strings.TrimSpace(utteranceID),
		TimestampMS: internalVoiceNowMillis(),
		Payload:     rawPayload,
	}
	if err := envelope.Validate(); err != nil {
		return internalVoiceEnvelope{}, err
	}
	return envelope, nil
}

func marshalInternalVoicePayload(payload any) (json.RawMessage, error) {
	if payload == nil {
		return json.RawMessage(`{}`), nil
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal internal voice payload: %w", err)
	}
	if len(raw) == 0 || string(raw) == "null" {
		return json.RawMessage(`{}`), nil
	}
	return json.RawMessage(raw), nil
}

func (e internalVoiceEnvelope) Validate() error {
	if e.Version != internalVoiceProtocolVersion {
		return fmt.Errorf("unsupported internal voice protocol version: %d", e.Version)
	}
	eventType := strings.TrimSpace(e.Type)
	if eventType == "" {
		return fmt.Errorf("internal voice event type is required")
	}
	if !strings.Contains(eventType, ".") && eventType != internalVoiceTypeClientEvent && eventType != internalVoiceTypeInterrupt {
		return fmt.Errorf("internal voice event type must be dotted: %s", eventType)
	}
	if strings.TrimSpace(e.SessionID) == "" {
		return fmt.Errorf("internal voice session_id is required")
	}
	if (strings.HasPrefix(eventType, "input_audio.") || eventType == internalVoiceTypeInputTextCommit) && strings.TrimSpace(e.UtteranceID) == "" {
		return fmt.Errorf("internal voice utterance_id is required for %s", eventType)
	}
	if (eventType == internalVoiceTypeResponseASR ||
		eventType == internalVoiceTypeResponseAudio ||
		eventType == internalVoiceTypeResponseDone) &&
		(strings.TrimSpace(e.RoundID) == "" || strings.TrimSpace(e.PlaybackID) == "") {
		return fmt.Errorf("internal voice round_id and playback_id are required for %s", eventType)
	}
	if len(e.Payload) == 0 {
		return fmt.Errorf("internal voice payload is required")
	}
	return nil
}

func (e internalVoiceEnvelope) PayloadInto(target any) error {
	if target == nil {
		return fmt.Errorf("internal voice payload target is nil")
	}
	if len(e.Payload) == 0 {
		return fmt.Errorf("internal voice payload is empty")
	}
	if err := json.Unmarshal(e.Payload, target); err != nil {
		return fmt.Errorf("decode internal voice payload: %w", err)
	}
	return nil
}

func internalVoiceNowMillis() int64 {
	return time.Now().UnixNano() / int64(time.Millisecond)
}
