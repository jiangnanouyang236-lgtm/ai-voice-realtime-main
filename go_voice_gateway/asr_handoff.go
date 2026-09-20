package main

import "fmt"

const (
	opusPacketStreamMagic       = "OPUSRAW1"
	maxOpusPacketPayloadBytes   = 65535
	asrHandoffAudioEncodingOpus = "opus"
)

type ASREncodedAudioHandoff struct {
	SessionID                  string
	BotID                      string
	TraceID                    string
	UtteranceID                string
	TrackID                    string
	StreamID                   string
	AudioEncoding              string
	AudioTransport             string
	PacketStreamFormat         string
	RetainedPacketCount        int
	RetainedPacketPayloadBytes int
	PacketStreamBytes          int
	ObservedPacketCount        uint64
	ObservedPayloadBytes       uint64
	DroppedEncodedPackets      uint64
	DuplicatePackets           uint64
	SequenceGaps               uint64
	MissingPackets             uint64
	TimestampRegressions       uint64
	Lossy                      bool
	Provisional                bool
	Shadow                     bool
	CandidateSeq               uint64
	SpeechEpoch                uint64
	AudioWatermark             uint64
	SilenceMS                  uint64
	CandidateKind              string
	RoundID                    string
	PlaybackID                 string
	Payload                    []byte `json:"-"`
}

type ASREncodedAudioHandoffSink interface {
	HandleASREncodedAudioHandoff(ASREncodedAudioHandoff)
}

type ASREncodedAudioHandoffSinkFunc func(ASREncodedAudioHandoff)

func (f ASREncodedAudioHandoffSinkFunc) HandleASREncodedAudioHandoff(handoff ASREncodedAudioHandoff) {
	f(handoff)
}

func NewASREncodedAudioHandoffFromSegment(segment ASRPrototypeSegment) (ASREncodedAudioHandoff, error) {
	if segment.State != asrPrototypeStateCompleted {
		return ASREncodedAudioHandoff{}, fmt.Errorf("asr handoff requires completed segment, got %q", segment.State)
	}
	return newASREncodedAudioHandoff(segment)
}

func NewTurnCandidateAudioHandoffFromSegment(segment ASRPrototypeSegment, event CanonicalVoiceEvent) (ASREncodedAudioHandoff, error) {
	if segment.State != asrPrototypeStateRecording {
		return ASREncodedAudioHandoff{}, fmt.Errorf("turn candidate handoff requires recording segment, got %q", segment.State)
	}
	if event.Type != "turn_candidate" || event.CandidateSeq == 0 {
		return ASREncodedAudioHandoff{}, fmt.Errorf("turn candidate metadata is invalid")
	}
	handoff, err := newASREncodedAudioHandoff(segment)
	if err != nil {
		return ASREncodedAudioHandoff{}, err
	}
	handoff.Provisional = true
	handoff.Shadow = event.Shadow
	handoff.CandidateSeq = event.CandidateSeq
	handoff.SpeechEpoch = event.SpeechEpoch
	handoff.AudioWatermark = event.AudioWatermark
	handoff.SilenceMS = event.SilenceMS
	return handoff, nil
}

func NewBargeInAudioHandoffFromSegment(segment ASRPrototypeSegment, event CanonicalVoiceEvent) (ASREncodedAudioHandoff, error) {
	if segment.State != asrPrototypeStateRecording {
		return ASREncodedAudioHandoff{}, fmt.Errorf("barge-in handoff requires recording segment, got %q", segment.State)
	}
	if event.Type != "barge_in_probe" || event.CandidateSeq == 0 || event.SpeechEpoch == 0 {
		return ASREncodedAudioHandoff{}, fmt.Errorf("barge-in metadata is invalid")
	}
	handoff, err := newASREncodedAudioHandoff(segment)
	if err != nil {
		return ASREncodedAudioHandoff{}, err
	}
	handoff.Provisional = true
	handoff.CandidateKind = "barge_in"
	handoff.RoundID = event.RoundID
	handoff.PlaybackID = event.PlaybackID
	handoff.CandidateSeq = event.CandidateSeq
	handoff.SpeechEpoch = event.SpeechEpoch
	handoff.AudioWatermark = event.AudioWatermark
	return handoff, nil
}

func newASREncodedAudioHandoff(segment ASRPrototypeSegment) (ASREncodedAudioHandoff, error) {

	orderedPackets := orderedASRPrototypePackets(segment.EncodedPackets)
	payload, retainedPayloadBytes, err := buildOpusPacketStreamFromASRPackets(orderedPackets)
	if err != nil {
		return ASREncodedAudioHandoff{}, err
	}

	return ASREncodedAudioHandoff{
		SessionID:                  segment.SessionID,
		BotID:                      segment.BotID,
		TraceID:                    segment.TraceID,
		UtteranceID:                segment.UtteranceID,
		TrackID:                    segment.TrackID,
		StreamID:                   segment.StreamID,
		AudioEncoding:              asrHandoffAudioEncodingOpus,
		AudioTransport:             canonicalTransportWebRTCRTP,
		PacketStreamFormat:         opusPacketStreamMagic,
		RetainedPacketCount:        len(orderedPackets),
		RetainedPacketPayloadBytes: retainedPayloadBytes,
		PacketStreamBytes:          len(payload),
		ObservedPacketCount:        segment.AudioPackets,
		ObservedPayloadBytes:       segment.EncodedPayloadBytes,
		DroppedEncodedPackets:      segment.DroppedEncodedPackets,
		DuplicatePackets:           segment.DuplicatePackets,
		SequenceGaps:               segment.SequenceGaps,
		MissingPackets:             segment.MissingPackets,
		TimestampRegressions:       segment.TimestampRegressions,
		Lossy: segment.DroppedEncodedPackets > 0 ||
			segment.DuplicatePackets > 0 ||
			segment.MissingPackets > 0 ||
			segment.TimestampRegressions > 0,
		Payload: payload,
	}, nil
}

func buildOpusPacketStreamFromASRPackets(packets []ASRPrototypeEncodedPacket) ([]byte, int, error) {
	if len(packets) == 0 {
		return nil, 0, fmt.Errorf("asr handoff has no encoded audio packets")
	}

	output := make([]byte, 0, len(opusPacketStreamMagic)+len(packets)*2)
	output = append(output, opusPacketStreamMagic...)
	retainedPayloadBytes := 0
	for i, packet := range packets {
		payload := packet.Payload
		if len(payload) == 0 {
			return nil, 0, fmt.Errorf("asr handoff packet %d is empty", i)
		}
		if len(payload) > maxOpusPacketPayloadBytes {
			return nil, 0, fmt.Errorf("asr handoff packet %d is too large: %d bytes", i, len(payload))
		}
		if packet.PayloadBytes != 0 && packet.PayloadBytes != len(payload) {
			return nil, 0, fmt.Errorf(
				"asr handoff packet %d payload byte mismatch: metadata=%d actual=%d",
				i,
				packet.PayloadBytes,
				len(payload),
			)
		}
		output = append(output, byte(len(payload)>>8), byte(len(payload)))
		output = append(output, payload...)
		retainedPayloadBytes += len(payload)
	}
	return output, retainedPayloadBytes, nil
}
