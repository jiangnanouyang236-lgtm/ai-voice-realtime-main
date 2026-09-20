package main

import "time"

const (
	canonicalTransportWebSocket      = "websocket"
	canonicalTransportWebRTCControl  = "webrtc_datachannel"
	canonicalTransportWebRTCRTP      = "webrtc_rtp"
	canonicalTransportServerCleanup  = "server_cleanup"
	canonicalEventAudioPacket        = "audio_packet"
	canonicalEventRTPPacket          = "rtp_packet"
	canonicalEventRTPTrack           = "rtp_track"
	maxCachedCanonicalVoiceEvents    = 128
	canonicalEventLogSkipAudioPacket = "audio_packet"
	canonicalEventLogSkipHeartbeat   = "heartbeat"
)

type CanonicalVoiceEvent struct {
	Sequence  uint64    `json:"sequence"`
	Type      string    `json:"type"`
	SessionID string    `json:"session_id"`
	Transport string    `json:"transport"`
	At        time.Time `json:"at"`

	RobotID         string `json:"robot_id,omitempty"`
	BotID           string `json:"bot_id,omitempty"`
	ClientType      string `json:"client_type,omitempty"`
	ActiveTransport string `json:"active_transport,omitempty"`
	FromTransport   string `json:"from_transport,omitempty"`
	ToTransport     string `json:"to_transport,omitempty"`
	Source          string `json:"source,omitempty"`
	ClientEvent     string `json:"client_event,omitempty"`
	TraceID         string `json:"trace_id,omitempty"`
	UtteranceID     string `json:"utterance_id,omitempty"`
	RoundID         string `json:"round_id,omitempty"`
	PlaybackID      string `json:"playback_id,omitempty"`
	Reason          string `json:"reason,omitempty"`
	CandidateSeq    uint64 `json:"candidate_seq,omitempty"`
	SpeechEpoch     uint64 `json:"speech_epoch,omitempty"`
	AudioWatermark  uint64 `json:"audio_watermark,omitempty"`
	SilenceMS       uint64 `json:"silence_ms,omitempty"`
	Shadow          bool   `json:"shadow,omitempty"`
	Accepted        bool   `json:"accepted,omitempty"`
	Decision        string `json:"decision,omitempty"`

	TrackKind            string `json:"track_kind,omitempty"`
	TrackID              string `json:"track_id,omitempty"`
	StreamID             string `json:"stream_id,omitempty"`
	Codec                string `json:"codec,omitempty"`
	ClockRate            uint32 `json:"clock_rate,omitempty"`
	Channels             uint16 `json:"channels,omitempty"`
	PayloadType          int    `json:"payload_type,omitempty"`
	PacketCount          uint64 `json:"packet_count,omitempty"`
	PayloadBytes         uint64 `json:"payload_bytes,omitempty"`
	LastPayloadBytes     int    `json:"last_payload_bytes,omitempty"`
	FirstSequenceNumber  uint16 `json:"first_sequence_number,omitempty"`
	LastSequenceNumber   uint16 `json:"last_sequence_number,omitempty"`
	FirstTimestamp       uint32 `json:"first_timestamp,omitempty"`
	LastTimestamp        uint32 `json:"last_timestamp,omitempty"`
	CachedPackets        int    `json:"cached_packets,omitempty"`
	SequenceGaps         uint64 `json:"sequence_gaps,omitempty"`
	TimestampRegressions uint64 `json:"timestamp_regressions,omitempty"`

	rtpPayload             []byte
	candidateSnapshotReady bool
}

type VoiceEventSink interface {
	HandleVoiceEvent(CanonicalVoiceEvent)
}

type VoiceEventSinkFunc func(CanonicalVoiceEvent)

func (f VoiceEventSinkFunc) HandleVoiceEvent(event CanonicalVoiceEvent) {
	f(event)
}

type noopVoiceEventSink struct{}

func (noopVoiceEventSink) HandleVoiceEvent(CanonicalVoiceEvent) {}

func canonicalEventFromClientMessage(msg clientMessage, transport string) CanonicalVoiceEvent {
	return CanonicalVoiceEvent{
		Type:            msg.Type,
		Transport:       transport,
		RobotID:         msg.RobotID,
		BotID:           msg.BotID,
		ClientType:      msg.ClientType,
		ActiveTransport: msg.ActiveTransport,
		FromTransport:   msg.FromTransport,
		ToTransport:     msg.ToTransport,
		Source:          msg.Source,
		ClientEvent:     msg.Event,
		TraceID:         msg.TraceID,
		UtteranceID:     msg.UtteranceID,
		RoundID:         msg.RoundID,
		PlaybackID:      msg.PlaybackID,
		Reason:          msg.Reason,
		CandidateSeq:    msg.CandidateSeq,
		SpeechEpoch:     msg.SpeechEpoch,
		AudioWatermark:  msg.AudioWatermark,
		SilenceMS:       msg.SilenceMS,
		Shadow:          msg.Shadow,
		Accepted:        msg.Accepted,
		Decision:        msg.Decision,
	}
}

func canonicalEventFromRTPPacketSnapshot(snapshot rtpTrackSnapshot, payload []byte) CanonicalVoiceEvent {
	eventType := canonicalEventRTPPacket
	if snapshot.key.kind == "audio" {
		eventType = canonicalEventAudioPacket
	}
	event := CanonicalVoiceEvent{
		Type:                 eventType,
		Transport:            canonicalTransportWebRTCRTP,
		TrackKind:            snapshot.key.kind,
		TrackID:              snapshot.key.id,
		StreamID:             snapshot.key.streamID,
		PacketCount:          snapshot.packetCount,
		PayloadBytes:         snapshot.payloadBytes,
		LastPayloadBytes:     snapshot.lastPayloadBytes,
		FirstSequenceNumber:  snapshot.firstSequenceNumber,
		LastSequenceNumber:   snapshot.lastSequenceNumber,
		FirstTimestamp:       snapshot.firstTimestamp,
		LastTimestamp:        snapshot.lastTimestamp,
		CachedPackets:        snapshot.cachedPackets,
		SequenceGaps:         snapshot.sequenceGaps,
		TimestampRegressions: snapshot.timestampRegressions,
	}
	if eventType == canonicalEventAudioPacket && len(payload) > 0 {
		event.rtpPayload = append([]byte(nil), payload...)
	}
	return event
}

func (s *gatewaySession) recordCanonicalEvent(event CanonicalVoiceEvent) CanonicalVoiceEvent {
	if event.SessionID == "" {
		event.SessionID = s.id
	}
	if event.At.IsZero() {
		event.At = time.Now().UTC()
	}
	s.eventMu.Lock()
	s.eventSeq++
	event.Sequence = s.eventSeq
	cacheEvent := event
	cacheEvent.rtpPayload = nil
	s.events = append(s.events, cacheEvent)
	if len(s.events) > maxCachedCanonicalVoiceEvents {
		s.events = s.events[len(s.events)-maxCachedCanonicalVoiceEvents:]
	}
	s.eventMu.Unlock()

	if s.server != nil &&
		s.server.logger != nil &&
		event.Type != canonicalEventLogSkipAudioPacket &&
		event.Type != canonicalEventLogSkipHeartbeat {
		s.logInfo(
			"canonical_event",
			"type", event.Type,
			"transport", printable(event.Transport),
			"trace_id", printable(event.TraceID),
			"utterance_id", printable(event.UtteranceID),
			"round_id", printable(event.RoundID),
			"playback_id", printable(event.PlaybackID),
			"reason", printable(event.Reason),
		)
	}
	if s.server != nil && s.server.voiceEvents != nil {
		s.server.voiceEvents.HandleVoiceEvent(event)
	}
	return event
}

func (s *gatewaySession) recentCanonicalEvents() []CanonicalVoiceEvent {
	s.eventMu.Lock()
	defer s.eventMu.Unlock()
	if len(s.events) == 0 {
		return nil
	}
	events := make([]CanonicalVoiceEvent, len(s.events))
	copy(events, s.events)
	return events
}
