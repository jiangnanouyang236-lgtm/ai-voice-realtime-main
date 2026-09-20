package main

import (
	"bytes"
	"encoding/json"
	"testing"
)

func TestNewASREncodedAudioHandoffFromSegmentBuildsOPUSRAW1(t *testing.T) {
	segment := ASRPrototypeSegment{
		SessionID:             "session_1",
		BotID:                 "xiaowen",
		TraceID:               "trace_1",
		UtteranceID:           "utt_1",
		State:                 asrPrototypeStateCompleted,
		TrackID:               "microphone",
		StreamID:              "voice",
		AudioPackets:          2,
		EncodedPayloadBytes:   5,
		DroppedEncodedPackets: 1,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{PacketCount: 1, Payload: []byte("abc"), PayloadBytes: 3},
			{PacketCount: 2, Payload: []byte{4, 5}, PayloadBytes: 2},
		},
	}

	handoff, err := NewASREncodedAudioHandoffFromSegment(segment)
	if err != nil {
		t.Fatal(err)
	}

	wantPayload := []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c', 0, 2, 4, 5}
	if !bytes.Equal(handoff.Payload, wantPayload) {
		t.Fatalf("handoff payload = %v, want %v", handoff.Payload, wantPayload)
	}
	if handoff.SessionID != "session_1" ||
		handoff.BotID != "xiaowen" ||
		handoff.TraceID != "trace_1" ||
		handoff.UtteranceID != "utt_1" ||
		handoff.TrackID != "microphone" ||
		handoff.StreamID != "voice" ||
		handoff.AudioEncoding != asrHandoffAudioEncodingOpus ||
		handoff.AudioTransport != canonicalTransportWebRTCRTP ||
		handoff.PacketStreamFormat != opusPacketStreamMagic ||
		handoff.RetainedPacketCount != 2 ||
		handoff.RetainedPacketPayloadBytes != 5 ||
		handoff.PacketStreamBytes != len(wantPayload) ||
		handoff.ObservedPacketCount != 2 ||
		handoff.ObservedPayloadBytes != 5 ||
		handoff.DroppedEncodedPackets != 1 ||
		!handoff.Lossy {
		t.Fatalf("unexpected handoff metadata: %+v", handoff)
	}

	encoded, err := json.Marshal(handoff)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encoded, []byte(`"Payload"`)) || bytes.Contains(encoded, []byte("YWJj")) {
		t.Fatalf("handoff JSON exposed payload bytes: %s", encoded)
	}
}

func TestNewASREncodedAudioHandoffOrdersRTPPacketsAndCarriesQualityStats(t *testing.T) {
	segment := ASRPrototypeSegment{
		SessionID:            "session_1",
		UtteranceID:          "utt_1",
		State:                asrPrototypeStateCompleted,
		AudioPackets:         4,
		EncodedPayloadBytes:  3,
		DuplicatePackets:     1,
		SequenceGaps:         1,
		MissingPackets:       1,
		TimestampRegressions: 1,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{
				PacketCount:    2,
				Payload:        []byte("b"),
				PayloadBytes:   1,
				HasRTPMetadata: true,
				SequenceNumber: 11,
				Timestamp:      1920,
			},
			{
				PacketCount:    1,
				Payload:        []byte("a"),
				PayloadBytes:   1,
				HasRTPMetadata: true,
				SequenceNumber: 10,
				Timestamp:      960,
			},
			{
				PacketCount:    4,
				Payload:        []byte("c"),
				PayloadBytes:   1,
				HasRTPMetadata: true,
				SequenceNumber: 13,
				Timestamp:      1000,
			},
		},
	}

	handoff, err := NewASREncodedAudioHandoffFromSegment(segment)
	if err != nil {
		t.Fatal(err)
	}

	wantPayload := []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 1, 'a', 0, 1, 'b', 0, 1, 'c'}
	if !bytes.Equal(handoff.Payload, wantPayload) {
		t.Fatalf("handoff payload = %v, want %v", handoff.Payload, wantPayload)
	}
	if handoff.RetainedPacketCount != 3 ||
		handoff.DuplicatePackets != 1 ||
		handoff.SequenceGaps != 1 ||
		handoff.MissingPackets != 1 ||
		handoff.TimestampRegressions != 1 ||
		!handoff.Lossy {
		t.Fatalf("unexpected handoff quality metadata: %+v", handoff)
	}
}

func TestNewTurnCandidateAudioHandoffKeepsRecordingSegmentProvisional(t *testing.T) {
	segment := ASRPrototypeSegment{
		SessionID:    "session_1",
		TraceID:      "trace_1",
		UtteranceID:  "utt_1",
		State:        asrPrototypeStateRecording,
		AudioPackets: 1,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{PacketCount: 1, Payload: []byte("abc"), PayloadBytes: 3},
		},
	}
	handoff, err := NewTurnCandidateAudioHandoffFromSegment(segment, CanonicalVoiceEvent{
		Type:           "turn_candidate",
		CandidateSeq:   2,
		SpeechEpoch:    1,
		AudioWatermark: 16_000,
		SilenceMS:      300,
		Shadow:         true,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !handoff.Provisional || handoff.CandidateSeq != 2 || handoff.SpeechEpoch != 1 ||
		handoff.AudioWatermark != 16_000 || handoff.SilenceMS != 300 || handoff.RetainedPacketCount != 1 {
		t.Fatalf("unexpected candidate handoff: %+v", handoff)
	}
	if segment.State != asrPrototypeStateRecording {
		t.Fatalf("candidate mutated segment state: %s", segment.State)
	}
}

func TestNewTurnCandidateAudioHandoffPreservesActiveEligibility(t *testing.T) {
	segment := ASRPrototypeSegment{
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
		State:       asrPrototypeStateRecording,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{PacketCount: 1, Payload: []byte("abc"), PayloadBytes: 3},
		},
	}
	handoff, err := NewTurnCandidateAudioHandoffFromSegment(segment, CanonicalVoiceEvent{
		Type:           "turn_candidate",
		CandidateSeq:   1,
		AudioWatermark: 320,
		Shadow:         false,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !handoff.Provisional || handoff.Shadow {
		t.Fatalf("unexpected active candidate handoff: %+v", handoff)
	}
}

func TestNewBargeInAudioHandoffPreservesPlaybackIdentity(t *testing.T) {
	segment := ASRPrototypeSegment{
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "barge_1",
		State:       asrPrototypeStateRecording,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{PacketCount: 1, Payload: []byte("abc"), PayloadBytes: 3},
		},
	}
	event := CanonicalVoiceEvent{
		Type:           "barge_in_probe",
		UtteranceID:    "barge_1",
		RoundID:        "round_1",
		PlaybackID:     "playback_1",
		CandidateSeq:   2,
		SpeechEpoch:    1,
		AudioWatermark: 19_200,
	}
	handoff, err := NewBargeInAudioHandoffFromSegment(segment, event)
	if err != nil {
		t.Fatal(err)
	}
	if handoff.CandidateKind != "barge_in" || handoff.RoundID != event.RoundID ||
		handoff.PlaybackID != event.PlaybackID || handoff.CandidateSeq != event.CandidateSeq ||
		handoff.SpeechEpoch != event.SpeechEpoch || handoff.AudioWatermark != event.AudioWatermark ||
		!handoff.Provisional {
		t.Fatalf("unexpected barge-in handoff: %+v", handoff)
	}
}

func TestNewASREncodedAudioHandoffRejectsUnsafeSegments(t *testing.T) {
	if _, err := NewASREncodedAudioHandoffFromSegment(ASRPrototypeSegment{
		State: asrPrototypeStateRecording,
	}); err == nil {
		t.Fatal("expected recording segment error")
	}

	if _, err := NewASREncodedAudioHandoffFromSegment(ASRPrototypeSegment{
		State: asrPrototypeStateCompleted,
	}); err == nil {
		t.Fatal("expected empty audio packet error")
	}

	if _, err := NewASREncodedAudioHandoffFromSegment(ASRPrototypeSegment{
		State: asrPrototypeStateCompleted,
		EncodedPackets: []ASRPrototypeEncodedPacket{
			{Payload: []byte{1, 2, 3}, PayloadBytes: 2},
		},
	}); err == nil {
		t.Fatal("expected payload byte mismatch error")
	}
}
