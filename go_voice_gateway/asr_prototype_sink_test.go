package main

import (
	"bytes"
	"sync"
	"testing"
	"time"
)

func TestASRPrototypeSinkAggregatesCompletedSegment(t *testing.T) {
	sink := NewASRPrototypeSink(8)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		BotID:       "xiaowen",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})
	payload1 := []byte{1, 2, 3}
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		TrackID:          "microphone",
		StreamID:         "voice",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       payload1,
	})
	payload1[0] = 99
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		TrackID:          "microphone",
		StreamID:         "voice",
		PacketCount:      2,
		LastPayloadBytes: 4,
		rtpPayload:       []byte{4, 5, 6, 7},
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.Active) != 0 {
		t.Fatalf("active segments = %+v, want none", snapshot.Active)
	}
	if len(snapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", snapshot.Completed)
	}
	segment := snapshot.Completed[0]
	if segment.SessionID != "session_1" ||
		segment.TraceID != "trace_1" ||
		segment.UtteranceID != "utt_1" ||
		segment.State != asrPrototypeStateCompleted ||
		segment.AudioPackets != 2 ||
		segment.AudioPayloadBytes != 7 ||
		segment.LastPacketCount != 2 ||
		segment.TrackID != "microphone" ||
		segment.StreamID != "voice" {
		t.Fatalf("unexpected completed segment: %+v", segment)
	}
	if segment.EncodedPayloadBytes != 7 ||
		segment.DroppedEncodedPackets != 0 ||
		len(segment.EncodedPackets) != 2 {
		t.Fatalf("unexpected encoded packet stats: %+v", segment)
	}
	if segment.EncodedPackets[0].PacketCount != 1 ||
		segment.EncodedPackets[0].PayloadBytes != 3 ||
		!bytes.Equal(segment.EncodedPackets[0].Payload, []byte{1, 2, 3}) {
		t.Fatalf("unexpected first encoded packet: %+v", segment.EncodedPackets[0])
	}
	segment.EncodedPackets[0].Payload[0] = 42
	if snapshot.Completed[0].EncodedPackets[0].Payload[0] != 42 {
		t.Fatal("test setup failed to mutate snapshot copy")
	}
	if got := sink.Snapshot().Completed[0].EncodedPackets[0].Payload[0]; got != 1 {
		t.Fatalf("snapshot mutation changed sink state: first payload byte = %d, want 1", got)
	}
}

func TestASRPrototypeSinkObservesTurnCandidateWithoutEndingSegment(t *testing.T) {
	var handoffCount int
	var candidateHandoffs []ASREncodedAudioHandoff
	sink := NewASRPrototypeSinkWithHandoffSink(8, ASREncodedAudioHandoffSinkFunc(
		func(ASREncodedAudioHandoff) {
			handoffCount++
		},
	))
	sink.SetTurnCandidateHandoffSink(ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		candidateHandoffs = append(candidateHandoffs, handoff)
	}))
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       []byte{1, 2, 3},
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:           "turn_candidate",
		SessionID:      "session_1",
		TraceID:        "trace_1",
		UtteranceID:    "utt_1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		SilenceMS:      300,
		Shadow:         true,
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.TurnCandidates) != 1 {
		t.Fatalf("turn candidate observations = %+v, want 1", snapshot.TurnCandidates)
	}
	observation := snapshot.TurnCandidates[0]
	if !observation.SegmentActive ||
		observation.CandidateSeq != 1 ||
		observation.AudioWatermark != 16_000 ||
		observation.SilenceMS != 300 ||
		observation.ObservedAudioPackets != 1 ||
		observation.RetainedPackets != 1 {
		t.Fatalf("unexpected turn candidate observation: %+v", observation)
	}
	if len(snapshot.Completed) != 1 || handoffCount != 1 {
		t.Fatalf("final audio_end did not complete normally: completed=%d handoffs=%d", len(snapshot.Completed), handoffCount)
	}
	if len(candidateHandoffs) != 1 || !candidateHandoffs[0].Provisional || candidateHandoffs[0].CandidateSeq != 1 {
		t.Fatalf("candidate handoffs = %+v, want one provisional snapshot", candidateHandoffs)
	}
}

func TestASRPrototypeSinkWaitsForPacketsTrailingCandidateWatermark(t *testing.T) {
	candidateHandoffs := make(chan ASREncodedAudioHandoff, 1)
	sink := NewASRPrototypeSink(8)
	sink.SetTurnCandidateSnapshotGrace(20 * time.Millisecond)
	sink.SetTurnCandidateHandoffSink(ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		candidateHandoffs <- handoff
	}))
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       []byte{1, 2, 3},
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:           "turn_candidate",
		SessionID:      "session_1",
		TraceID:        "trace_1",
		UtteranceID:    "utt_1",
		CandidateSeq:   1,
		AudioWatermark: 640,
		SilenceMS:      300,
		Shadow:         true,
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      2,
		LastPayloadBytes: 3,
		rtpPayload:       []byte{4, 5, 6},
	})

	select {
	case handoff := <-candidateHandoffs:
		if got := handoff.RetainedPacketCount; got != 2 {
			t.Fatalf("candidate retained packets = %d, want 2", got)
		}
	case <-time.After(250 * time.Millisecond):
		t.Fatal("timed out waiting for deferred candidate handoff")
	}
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.TurnCandidates) != 1 || snapshot.TurnCandidates[0].RetainedPackets != 2 {
		t.Fatalf("unexpected deferred candidate observation: %+v", snapshot.TurnCandidates)
	}
}

func TestASRPrototypeSinkCandidateDoesNotCrossWatermarkAfterSpeechResume(t *testing.T) {
	candidateHandoffs := make(chan ASREncodedAudioHandoff, 1)
	sink := NewASRPrototypeSink(8)
	sink.SetTurnCandidateHandoffSink(ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		candidateHandoffs <- handoff
	}))
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})
	for packetCount := 1; packetCount <= 2; packetCount++ {
		sink.HandleVoiceEvent(CanonicalVoiceEvent{
			Type:             canonicalEventAudioPacket,
			SessionID:        "session_1",
			PacketCount:      uint64(packetCount),
			LastPayloadBytes: 3,
			rtpPayload:       []byte{byte(packetCount), 2, 3},
		})
	}
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:           "turn_candidate",
		SessionID:      "session_1",
		TraceID:        "trace_1",
		UtteranceID:    "utt_1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 320,
		SilenceMS:      300,
		Shadow:         true,
	})

	select {
	case handoff := <-candidateHandoffs:
		if handoff.RetainedPacketCount != 1 || handoff.ObservedPacketCount != 1 {
			t.Fatalf("candidate crossed watermark: %+v", handoff)
		}
	case <-time.After(250 * time.Millisecond):
		t.Fatal("timed out waiting for candidate handoff")
	}
	sink.Close()
}

func TestASRPrototypeSinkInterruptCancelsActiveSegment(t *testing.T) {
	sink := NewASRPrototypeSink(8)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		BotID:       "xiaowen",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      1,
		LastPayloadBytes: 5,
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "interrupt",
		SessionID: "session_1",
		Reason:    "user_interrupt",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.Active) != 0 {
		t.Fatalf("active segments = %+v, want none", snapshot.Active)
	}
	if len(snapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", snapshot.Completed)
	}
	segment := snapshot.Completed[0]
	if segment.State != asrPrototypeStateCancelled ||
		segment.Reason != "user_interrupt" ||
		segment.AudioPackets != 1 ||
		segment.AudioPayloadBytes != 5 {
		t.Fatalf("unexpected cancelled segment: %+v", segment)
	}
}

func TestASRPrototypeSinkSeparatesOverlappingUtterances(t *testing.T) {
	sink := NewASRPrototypeSink(16)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		BotID:       "xiaowen",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       []byte("one"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		BotID:       "xiaowen",
		UtteranceID: "utt_2",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      2,
		LastPayloadBytes: 3,
		rtpPayload:       []byte("two"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_2",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.Active) != 0 {
		t.Fatalf("active segments = %+v, want none", snapshot.Active)
	}
	completed := completedSegmentsByUtterance(t, snapshot.Completed)
	utt1 := completed["utt_1"]
	utt2 := completed["utt_2"]
	if utt1.State != asrPrototypeStateCompleted ||
		utt1.AudioPackets != 1 ||
		utt1.EncodedPayloadBytes != 3 ||
		len(utt1.EncodedPackets) != 1 ||
		!bytes.Equal(utt1.EncodedPackets[0].Payload, []byte("one")) {
		t.Fatalf("unexpected utt_1 segment: %+v", utt1)
	}
	if utt2.State != asrPrototypeStateCompleted ||
		utt2.AudioPackets != 1 ||
		utt2.EncodedPayloadBytes != 3 ||
		len(utt2.EncodedPackets) != 1 ||
		!bytes.Equal(utt2.EncodedPackets[0].Payload, []byte("two")) {
		t.Fatalf("unexpected utt_2 segment: %+v", utt2)
	}
}

func TestASRPrototypeSinkExplicitCancelDoesNotCancelCurrentUtterance(t *testing.T) {
	sink := NewASRPrototypeSink(16)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		UtteranceID: "utt_2",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_cancel",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
		Reason:      "late_cancel",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      2,
		LastPayloadBytes: 3,
		rtpPayload:       []byte("two"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_2",
	})
	sink.Close()

	completed := completedSegmentsByUtterance(t, sink.Snapshot().Completed)
	utt1 := completed["utt_1"]
	if utt1.State != asrPrototypeStateCancelled || utt1.Reason != "late_cancel" {
		t.Fatalf("unexpected utt_1 segment: %+v", utt1)
	}
	utt2 := completed["utt_2"]
	if utt2.State != asrPrototypeStateCompleted ||
		utt2.AudioPackets != 1 ||
		len(utt2.EncodedPackets) != 1 ||
		!bytes.Equal(utt2.EncodedPackets[0].Payload, []byte("two")) {
		t.Fatalf("unexpected utt_2 segment: %+v", utt2)
	}
}

func TestASRPrototypeSinkDeduplicatesOrdersAndMarksLossyRTP(t *testing.T) {
	sink := NewASRPrototypeSink(16)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         1,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 11,
		LastSequenceNumber:  11,
		LastTimestamp:       1920,
		rtpPayload:          []byte("b"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         2,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 10,
		LastSequenceNumber:  10,
		LastTimestamp:       960,
		rtpPayload:          []byte("a"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         3,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 11,
		LastSequenceNumber:  11,
		LastTimestamp:       1920,
		rtpPayload:          []byte("duplicate"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         4,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 13,
		LastSequenceNumber:  13,
		LastTimestamp:       1000,
		rtpPayload:          []byte("c"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", snapshot.Completed)
	}
	segment := snapshot.Completed[0]
	if segment.AudioPackets != 4 ||
		segment.EncodedPayloadBytes != 3 ||
		segment.DuplicatePackets != 1 ||
		segment.SequenceGaps != 1 ||
		segment.MissingPackets != 1 ||
		segment.TimestampRegressions != 1 ||
		len(segment.EncodedPackets) != 3 {
		t.Fatalf("unexpected RTP quality segment: %+v", segment)
	}
	if got := bytes.Join([][]byte{
		segment.EncodedPackets[0].Payload,
		segment.EncodedPackets[1].Payload,
		segment.EncodedPackets[2].Payload,
	}, nil); !bytes.Equal(got, []byte("abc")) {
		t.Fatalf("ordered retained payloads = %q, want abc", got)
	}
}

func TestASRPrototypeSinkEndGraceKeepsLateRTPInSameHandoff(t *testing.T) {
	handoffCh := make(chan ASREncodedAudioHandoff, 1)
	sink := NewASRPrototypeSinkWithOptions(16, ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		handoffCh <- handoff
	}), 20*time.Millisecond)

	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         1,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 1,
		LastSequenceNumber:  1,
		LastTimestamp:       960,
		rtpPayload:          []byte("a"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:                canonicalEventAudioPacket,
		SessionID:           "session_1",
		PacketCount:         2,
		LastPayloadBytes:    1,
		FirstSequenceNumber: 2,
		LastSequenceNumber:  2,
		LastTimestamp:       1920,
		rtpPayload:          []byte("b"),
	})

	var handoff ASREncodedAudioHandoff
	select {
	case handoff = <-handoffCh:
	case <-time.After(250 * time.Millisecond):
		t.Fatal("timed out waiting for handoff")
	}
	sink.Close()

	wantPayload := []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 1, 'a', 0, 1, 'b'}
	if handoff.RetainedPacketCount != 2 ||
		handoff.ObservedPacketCount != 2 ||
		!bytes.Equal(handoff.Payload, wantPayload) {
		t.Fatalf("unexpected grace handoff: %+v payload=%v", handoff, handoff.Payload)
	}
}

func TestASRPrototypeSinkTurnCommitCompletesWithoutFormalHandoff(t *testing.T) {
	handoffCh := make(chan ASREncodedAudioHandoff, 1)
	sink := NewASRPrototypeSinkWithHandoffSink(8, ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		handoffCh <- handoff
	}))
	sink.HandleVoiceEvent(CanonicalVoiceEvent{Type: "audio_start", SessionID: "session_1", UtteranceID: "utt_1"})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		PacketCount:      1,
		LastPayloadBytes: 1,
		rtpPayload:       []byte("a"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "turn_commit",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
		Reason:      "turn_gate_active_commit",
	})
	sink.Close()

	select {
	case handoff := <-handoffCh:
		t.Fatalf("active promotion must not dispatch duplicate formal ASR handoff: %+v", handoff)
	default:
	}
	snapshot := sink.Snapshot()
	if len(snapshot.Active) != 0 || len(snapshot.Completed) != 1 || snapshot.Completed[0].Reason != "turn_gate_active_commit" {
		t.Fatalf("unexpected turn commit snapshot: %+v", snapshot)
	}
}

func TestASRPrototypeSinkCapsEncodedPayloadWindow(t *testing.T) {
	sink := NewASRPrototypeSink(maxASRPrototypeEncodedPackets + 16)
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_start",
		SessionID: "session_1",
	})
	for i := 1; i <= maxASRPrototypeEncodedPackets+4; i++ {
		sink.HandleVoiceEvent(CanonicalVoiceEvent{
			Type:             canonicalEventAudioPacket,
			SessionID:        "session_1",
			PacketCount:      uint64(i),
			LastPayloadBytes: 1,
			rtpPayload:       []byte{byte(i)},
		})
	}
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_end",
		SessionID: "session_1",
	})
	sink.Close()

	snapshot := sink.Snapshot()
	if len(snapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", snapshot.Completed)
	}
	segment := snapshot.Completed[0]
	if len(segment.EncodedPackets) != maxASRPrototypeEncodedPackets {
		t.Fatalf("stored encoded packets = %d, want %d", len(segment.EncodedPackets), maxASRPrototypeEncodedPackets)
	}
	if segment.DroppedEncodedPackets != 4 || segment.EncodedPayloadBytes != maxASRPrototypeEncodedPackets+4 {
		t.Fatalf("unexpected bounded window stats: %+v", segment)
	}
	if segment.EncodedPackets[0].PacketCount != 5 {
		t.Fatalf("first retained packet count = %d, want 5", segment.EncodedPackets[0].PacketCount)
	}
}

func TestASRPrototypeSinkDispatchesCompletedHandoff(t *testing.T) {
	var mu sync.Mutex
	var handoffs []ASREncodedAudioHandoff
	sink := NewASRPrototypeSinkWithHandoffSink(8, ASREncodedAudioHandoffSinkFunc(func(handoff ASREncodedAudioHandoff) {
		mu.Lock()
		defer mu.Unlock()
		handoffs = append(handoffs, handoff)
	}))

	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		SessionID:   "session_1",
		BotID:       "xiaowen",
		UtteranceID: "utt_1",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:             canonicalEventAudioPacket,
		SessionID:        "session_1",
		TrackID:          "microphone",
		StreamID:         "voice",
		PacketCount:      1,
		LastPayloadBytes: 3,
		rtpPayload:       []byte("abc"),
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:        "audio_end",
		SessionID:   "session_1",
		UtteranceID: "utt_1",
	})
	sink.Close()

	mu.Lock()
	defer mu.Unlock()
	if len(handoffs) != 1 {
		t.Fatalf("handoffs = %+v, want 1", handoffs)
	}
	handoff := handoffs[0]
	wantPayload := []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c'}
	if handoff.SessionID != "session_1" ||
		handoff.BotID != "xiaowen" ||
		handoff.UtteranceID != "utt_1" ||
		handoff.TrackID != "microphone" ||
		handoff.StreamID != "voice" ||
		handoff.RetainedPacketCount != 1 ||
		!bytes.Equal(handoff.Payload, wantPayload) {
		t.Fatalf("unexpected handoff: %+v payload=%v", handoff, handoff.Payload)
	}
}

func TestASRPrototypeSinkSkipsCancelledHandoffAndCountsBuildErrors(t *testing.T) {
	var handoffCount int
	sink := NewASRPrototypeSinkWithHandoffSink(8, ASREncodedAudioHandoffSinkFunc(func(ASREncodedAudioHandoff) {
		handoffCount++
	}))

	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_start",
		SessionID: "cancelled_session",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_cancel",
		SessionID: "cancelled_session",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_start",
		SessionID: "empty_session",
	})
	sink.HandleVoiceEvent(CanonicalVoiceEvent{
		Type:      "audio_end",
		SessionID: "empty_session",
	})
	sink.Close()

	if handoffCount != 0 {
		t.Fatalf("handoff count = %d, want 0", handoffCount)
	}
	snapshot := sink.Snapshot()
	if snapshot.HandoffBuildErrorCount != 1 {
		t.Fatalf("handoff build errors = %d, want 1", snapshot.HandoffBuildErrorCount)
	}
}

func completedSegmentsByUtterance(t *testing.T, segments []ASRPrototypeSegment) map[string]ASRPrototypeSegment {
	t.Helper()
	completed := make(map[string]ASRPrototypeSegment, len(segments))
	for _, segment := range segments {
		if segment.UtteranceID == "" {
			t.Fatalf("completed segment without utterance_id: %+v", segment)
		}
		if _, ok := completed[segment.UtteranceID]; ok {
			t.Fatalf("duplicate completed utterance_id %q in %+v", segment.UtteranceID, segments)
		}
		completed[segment.UtteranceID] = segment
	}
	if len(completed) != len(segments) {
		t.Fatalf("completed segment index mismatch: %+v", segments)
	}
	return completed
}
