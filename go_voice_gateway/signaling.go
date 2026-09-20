package main

type clientMessage struct {
	Type            string  `json:"type"`
	SessionID       string  `json:"session_id,omitempty"`
	RobotID         string  `json:"robot_id,omitempty"`
	RobotSecret     *string `json:"robot_secret,omitempty"`
	ClientType      string  `json:"client_type,omitempty"`
	SDP             string  `json:"sdp,omitempty"`
	Candidate       string  `json:"candidate,omitempty"`
	SDPMid          *string `json:"sdp_mid,omitempty"`
	SDPMLineIndex   *uint16 `json:"sdp_mline_index,omitempty"`
	ActiveTransport string  `json:"active_transport,omitempty"`
	FromTransport   string  `json:"from_transport,omitempty"`
	ToTransport     string  `json:"to_transport,omitempty"`
	Reason          string  `json:"reason,omitempty"`
	Boundary        *string `json:"boundary,omitempty"`
	BotID           string  `json:"bot_id,omitempty"`
	Source          string  `json:"source,omitempty"`
	Event           string  `json:"event,omitempty"`
	EventID         string  `json:"event_id,omitempty"`
	Content         string  `json:"content,omitempty"`
	TraceID         string  `json:"trace_id,omitempty"`
	UtteranceID     string  `json:"utterance_id,omitempty"`
	RoundID         string  `json:"round_id,omitempty"`
	PlaybackID      string  `json:"playback_id,omitempty"`
	CandidateSeq    uint64  `json:"candidate_seq,omitempty"`
	SpeechEpoch     uint64  `json:"speech_epoch,omitempty"`
	AudioWatermark  uint64  `json:"audio_watermark,omitempty"`
	SilenceMS       uint64  `json:"silence_ms,omitempty"`
	Shadow          bool    `json:"shadow,omitempty"`
	Accepted        bool    `json:"accepted,omitempty"`
	Decision        string  `json:"decision,omitempty"`
	ASRTimeMS       float64 `json:"asr_time_ms,omitempty"`

	FirstAudioToPlaybackStartMS *float64 `json:"first_audio_to_playback_start_ms,omitempty"`
	PlaybackStartToCompleteMS   *float64 `json:"playback_start_to_complete_ms,omitempty"`
	PushedChunks                *uint64  `json:"pushed_chunks,omitempty"`
	PushedSamples               *uint64  `json:"pushed_samples,omitempty"`
	UnderrunCallbacks           *uint64  `json:"underrun_callbacks,omitempty"`
	ZeroFilledSamples           *uint64  `json:"zero_filled_samples,omitempty"`
	MaxBufferedSamples          *uint64  `json:"max_buffered_samples,omitempty"`
}

type connectedMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
}

type registeredMessage struct {
	Type       string `json:"type"`
	SessionID  string `json:"session_id"`
	RobotID    string `json:"robot_id"`
	BotID      string `json:"bot_id"`
	BotName    string `json:"bot_name"`
	ClientType string `json:"client_type"`
	IsNewRobot bool   `json:"is_new_robot"`
}

type rtcConfigMessage struct {
	Type       string            `json:"type"`
	SessionID  string            `json:"session_id"`
	ICEServers []ICEServerConfig `json:"ice_servers"`
	Media      rtcMediaConfig    `json:"media"`
}

type rtcMediaConfig struct {
	Audio AudioConfig `json:"audio"`
}

type rtcAnswerMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

type rtcIceCandidateMessage struct {
	Type          string  `json:"type"`
	SessionID     string  `json:"session_id"`
	Candidate     string  `json:"candidate"`
	SDPMid        *string `json:"sdp_mid,omitempty"`
	SDPMLineIndex *uint16 `json:"sdp_mline_index,omitempty"`
}

type transportFallbackAckMessage struct {
	Type            string  `json:"type"`
	SessionID       string  `json:"session_id"`
	ActiveTransport string  `json:"active_transport"`
	Reason          *string `json:"reason,omitempty"`
}

type errorMessage struct {
	Type    string `json:"type"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

type simpleTypeMessage struct {
	Type string `json:"type"`
}

type playbackCancelMessage struct {
	Type       string `json:"type"`
	RoundID    string `json:"round_id"`
	PlaybackID string `json:"playback_id"`
	Reason     string `json:"reason,omitempty"`
}

type turnCommitRequestMessage struct {
	Type           string  `json:"type"`
	TraceID        string  `json:"trace_id,omitempty"`
	UtteranceID    string  `json:"utterance_id"`
	CandidateSeq   uint64  `json:"candidate_seq"`
	SpeechEpoch    uint64  `json:"speech_epoch"`
	AudioWatermark uint64  `json:"audio_watermark"`
	ASRText        string  `json:"asr_text,omitempty"`
	ASRTimeMS      float64 `json:"asr_time_ms,omitempty"`
}

type turnCandidateDecisionMessage struct {
	Type           string `json:"type"`
	TraceID        string `json:"trace_id,omitempty"`
	UtteranceID    string `json:"utterance_id"`
	CandidateSeq   uint64 `json:"candidate_seq"`
	SpeechEpoch    uint64 `json:"speech_epoch"`
	AudioWatermark uint64 `json:"audio_watermark"`
	Decision       string `json:"decision"`
	Reason         string `json:"reason,omitempty"`
}

type bargeInDecisionMessage struct {
	Type           string  `json:"type"`
	TraceID        string  `json:"trace_id"`
	UtteranceID    string  `json:"utterance_id"`
	RoundID        string  `json:"round_id"`
	PlaybackID     string  `json:"playback_id"`
	CandidateSeq   uint64  `json:"candidate_seq"`
	SpeechEpoch    uint64  `json:"speech_epoch"`
	AudioWatermark uint64  `json:"audio_watermark"`
	Decision       string  `json:"decision"`
	Reason         string  `json:"reason,omitempty"`
	ASRText        string  `json:"asr_text,omitempty"`
	ASRTimeMS      float64 `json:"asr_time_ms,omitempty"`
}

func newBargeInDecisionMessage(result BargeInResult) bargeInDecisionMessage {
	return bargeInDecisionMessage{
		Type:           "barge_in_decision",
		TraceID:        result.TraceID,
		UtteranceID:    result.UtteranceID,
		RoundID:        result.RoundID,
		PlaybackID:     result.PlaybackID,
		CandidateSeq:   result.CandidateSeq,
		SpeechEpoch:    result.SpeechEpoch,
		AudioWatermark: result.AudioWatermark,
		Decision:       result.Decision,
		Reason:         result.Reason,
		ASRText:        result.ASRText,
		ASRTimeMS:      result.ASRTimeMS,
	}
}

func newTurnCommitRequestMessage(result TurnCandidateResult) turnCommitRequestMessage {
	return turnCommitRequestMessage{
		Type:           "turn_commit_request",
		TraceID:        result.TraceID,
		UtteranceID:    result.UtteranceID,
		CandidateSeq:   result.CandidateSeq,
		SpeechEpoch:    result.SpeechEpoch,
		AudioWatermark: result.AudioWatermark,
		ASRText:        result.ASRText,
		ASRTimeMS:      result.ASRTimeMS,
	}
}

func newTurnCandidateDecisionMessage(result TurnCandidateResult, decision string, reason string) turnCandidateDecisionMessage {
	return turnCandidateDecisionMessage{
		Type:           "turn_candidate_decision",
		TraceID:        result.TraceID,
		UtteranceID:    result.UtteranceID,
		CandidateSeq:   result.CandidateSeq,
		SpeechEpoch:    result.SpeechEpoch,
		AudioWatermark: result.AudioWatermark,
		Decision:       decision,
		Reason:         reason,
	}
}

func newConnectedMessage(sessionID string) connectedMessage {
	return connectedMessage{Type: "connected", SessionID: sessionID}
}

func newRegisteredMessageFromRegistration(sessionID string, registration deviceRegistration) registeredMessage {
	robotID := registration.RobotID
	if robotID == "" {
		robotID = "unknown_robot"
	}
	clientType := registration.ClientType
	if clientType == "" {
		clientType = "unknown"
	}
	botID := registration.BotID
	if botID == "" {
		botID = "unknown_bot"
	}
	botName := registration.BotName
	if botName == "" {
		botName = botID
	}
	return registeredMessage{
		Type:       "registered",
		SessionID:  sessionID,
		RobotID:    robotID,
		BotID:      botID,
		BotName:    botName,
		ClientType: clientType,
		IsNewRobot: registration.IsNewRobot,
	}
}

func newRTCConfigMessage(sessionID string, cfg Config) rtcConfigMessage {
	return rtcConfigMessage{
		Type:       "rtc_config",
		SessionID:  sessionID,
		ICEServers: append([]ICEServerConfig(nil), cfg.ICEServers...),
		Media: rtcMediaConfig{
			Audio: cfg.Audio,
		},
	}
}

func newRTCAnswerMessage(sessionID string, sdp string) rtcAnswerMessage {
	return rtcAnswerMessage{Type: "rtc_answer", SessionID: sessionID, SDP: sdp}
}

func newRTCIceCandidateMessage(sessionID string, candidate string, sdpMid *string, sdpMLineIndex *uint16) rtcIceCandidateMessage {
	return rtcIceCandidateMessage{
		Type:          "rtc_ice_candidate",
		SessionID:     sessionID,
		Candidate:     candidate,
		SDPMid:        sdpMid,
		SDPMLineIndex: sdpMLineIndex,
	}
}

func newFallbackAckMessage(sessionID string, activeTransport string, reason string) transportFallbackAckMessage {
	if activeTransport == "" {
		activeTransport = "websocket"
	}
	var reasonRef *string
	if reason != "" {
		reasonRef = &reason
	}
	return transportFallbackAckMessage{
		Type:            "transport_fallback_ack",
		SessionID:       sessionID,
		ActiveTransport: activeTransport,
		Reason:          reasonRef,
	}
}

func newErrorMessage(code string, message string) errorMessage {
	return errorMessage{Type: "error", Code: code, Message: message}
}
