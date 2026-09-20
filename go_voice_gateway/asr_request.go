package main

import "fmt"

const (
	asrAudioRequestType             = "audio"
	asrAudioRequestTransportWebRTC  = "webrtc_rtp"
	asrAudioRequestDefaultRate      = 16000
	asrAudioRequestDefaultChannels  = 1
	asrAudioRequestDefaultFrameMS   = 20
	asrAudioRequestEncodingOpus     = "opus"
	asrAudioRequestPacketStreamOpus = "OPUSRAW1"
)

type ASRAudioRequest struct {
	Type                       string `json:"type"`
	SessionID                  string `json:"session_id,omitempty"`
	ContextSessionID           string `json:"context_session_id,omitempty"`
	BotID                      string `json:"bot_id,omitempty"`
	TraceID                    string `json:"trace_id,omitempty"`
	UtteranceID                string `json:"utterance_id,omitempty"`
	TrackID                    string `json:"track_id,omitempty"`
	StreamID                   string `json:"stream_id,omitempty"`
	AudioEncoding              string `json:"audio_encoding"`
	AudioTransport             string `json:"audio_transport"`
	PacketStreamFormat         string `json:"packet_stream_format"`
	SampleRate                 uint32 `json:"sample_rate"`
	Channels                   uint16 `json:"channels"`
	OpusFrameMS                uint16 `json:"opus_frame_ms"`
	PacketCount                int    `json:"packet_count"`
	AudioBytes                 []byte `json:"-"`
	AudioByteCount             int    `json:"audio_byte_count"`
	RetainedPacketPayloadBytes int    `json:"retained_packet_payload_bytes"`
	ObservedPacketCount        uint64 `json:"observed_packet_count"`
	ObservedPayloadBytes       uint64 `json:"observed_payload_bytes"`
	DroppedEncodedPackets      uint64 `json:"dropped_encoded_packets,omitempty"`
	DuplicatePackets           uint64 `json:"duplicate_packets,omitempty"`
	SequenceGaps               uint64 `json:"sequence_gaps,omitempty"`
	MissingPackets             uint64 `json:"missing_packets,omitempty"`
	TimestampRegressions       uint64 `json:"timestamp_regressions,omitempty"`
	Lossy                      bool   `json:"lossy,omitempty"`
	Provisional                bool   `json:"provisional,omitempty"`
	Shadow                     bool   `json:"shadow,omitempty"`
	CandidateSeq               uint64 `json:"candidate_seq,omitempty"`
	SpeechEpoch                uint64 `json:"speech_epoch,omitempty"`
	AudioWatermark             uint64 `json:"audio_watermark,omitempty"`
	SilenceMS                  uint64 `json:"silence_ms,omitempty"`
	CandidateKind              string `json:"candidate_kind,omitempty"`
	RoundID                    string `json:"round_id,omitempty"`
	PlaybackID                 string `json:"playback_id,omitempty"`
}

type ASRAudioRequestProcessor interface {
	ProcessASRAudioRequest(ASRAudioRequest) error
}

type ASRAudioRequestProcessorFunc func(ASRAudioRequest) error

func (f ASRAudioRequestProcessorFunc) ProcessASRAudioRequest(request ASRAudioRequest) error {
	return f(request)
}

type ASRAudioRequestProcessorAdapter struct {
	processor ASRAudioRequestProcessor
}

func NewASRAudioRequestProcessorAdapter(processor ASRAudioRequestProcessor) ASREncodedAudioHandoffProcessor {
	if processor == nil {
		processor = noopASRAudioRequestProcessor{}
	}
	return ASRAudioRequestProcessorAdapter{processor: processor}
}

func (a ASRAudioRequestProcessorAdapter) ProcessASREncodedAudioHandoff(handoff ASREncodedAudioHandoff) error {
	request, err := NewASRAudioRequestFromHandoff(handoff)
	if err != nil {
		return err
	}
	return a.processor.ProcessASRAudioRequest(request)
}

type noopASRAudioRequestProcessor struct{}

func (noopASRAudioRequestProcessor) ProcessASRAudioRequest(ASRAudioRequest) error {
	return nil
}

func NewASRAudioRequestFromHandoff(handoff ASREncodedAudioHandoff) (ASRAudioRequest, error) {
	if handoff.AudioEncoding != "" && handoff.AudioEncoding != asrHandoffAudioEncodingOpus {
		return ASRAudioRequest{}, fmt.Errorf("unsupported ASR handoff audio encoding: %s", handoff.AudioEncoding)
	}
	if handoff.PacketStreamFormat != "" && handoff.PacketStreamFormat != opusPacketStreamMagic {
		return ASRAudioRequest{}, fmt.Errorf("unsupported ASR handoff packet stream: %s", handoff.PacketStreamFormat)
	}
	if len(handoff.Payload) == 0 {
		return ASRAudioRequest{}, fmt.Errorf("ASR handoff payload is empty")
	}
	if len(handoff.Payload) < len(opusPacketStreamMagic) || string(handoff.Payload[:len(opusPacketStreamMagic)]) != opusPacketStreamMagic {
		return ASRAudioRequest{}, fmt.Errorf("ASR handoff payload magic mismatch")
	}
	if handoff.PacketStreamBytes != 0 && handoff.PacketStreamBytes != len(handoff.Payload) {
		return ASRAudioRequest{}, fmt.Errorf(
			"ASR handoff packet stream byte mismatch: metadata=%d actual=%d",
			handoff.PacketStreamBytes,
			len(handoff.Payload),
		)
	}
	if handoff.RetainedPacketCount <= 0 {
		return ASRAudioRequest{}, fmt.Errorf("ASR handoff has no retained packets")
	}

	return ASRAudioRequest{
		Type:                       asrAudioRequestType,
		SessionID:                  handoff.SessionID,
		BotID:                      handoff.BotID,
		TraceID:                    traceIDForVoiceTurn(handoff.TraceID, handoff.SessionID, handoff.UtteranceID),
		UtteranceID:                handoff.UtteranceID,
		TrackID:                    handoff.TrackID,
		StreamID:                   handoff.StreamID,
		AudioEncoding:              asrAudioRequestEncodingOpus,
		AudioTransport:             asrAudioRequestTransportWebRTC,
		PacketStreamFormat:         asrAudioRequestPacketStreamOpus,
		SampleRate:                 asrAudioRequestDefaultRate,
		Channels:                   asrAudioRequestDefaultChannels,
		OpusFrameMS:                asrAudioRequestDefaultFrameMS,
		PacketCount:                handoff.RetainedPacketCount,
		AudioBytes:                 append([]byte(nil), handoff.Payload...),
		AudioByteCount:             len(handoff.Payload),
		RetainedPacketPayloadBytes: handoff.RetainedPacketPayloadBytes,
		ObservedPacketCount:        handoff.ObservedPacketCount,
		ObservedPayloadBytes:       handoff.ObservedPayloadBytes,
		DroppedEncodedPackets:      handoff.DroppedEncodedPackets,
		DuplicatePackets:           handoff.DuplicatePackets,
		SequenceGaps:               handoff.SequenceGaps,
		MissingPackets:             handoff.MissingPackets,
		TimestampRegressions:       handoff.TimestampRegressions,
		Lossy:                      handoff.Lossy,
		Provisional:                handoff.Provisional,
		Shadow:                     handoff.Shadow,
		CandidateSeq:               handoff.CandidateSeq,
		SpeechEpoch:                handoff.SpeechEpoch,
		AudioWatermark:             handoff.AudioWatermark,
		SilenceMS:                  handoff.SilenceMS,
		CandidateKind:              handoff.CandidateKind,
		RoundID:                    handoff.RoundID,
		PlaybackID:                 handoff.PlaybackID,
	}, nil
}
