package main

import (
	"bytes"
	"errors"
	"log"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestQueuedASREncodedAudioHandoffSinkClonesPayload(t *testing.T) {
	received := make(chan []byte, 1)
	sink := NewQueuedASREncodedAudioHandoffSink(1, ASREncodedAudioHandoffProcessorFunc(func(handoff ASREncodedAudioHandoff) error {
		received <- append([]byte(nil), handoff.Payload...)
		return nil
	}))

	payload := []byte("OPUSRAW1")
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{
		SessionID: "session_1",
		Payload:   payload,
	})
	payload[0] = 'X'
	sink.Close()

	got := <-received
	if !bytes.Equal(got, []byte("OPUSRAW1")) {
		t.Fatalf("received payload = %q, want OPUSRAW1", got)
	}
	snapshot := sink.Snapshot()
	if snapshot.ProcessedCount != 1 ||
		snapshot.DroppedCount != 0 ||
		snapshot.ErrorCount != 0 ||
		snapshot.QueueCapacity != 1 {
		t.Fatalf("unexpected queue snapshot: %+v", snapshot)
	}
}

func TestConcurrentASREncodedAudioHandoffSinkProcessesDifferentSessionsInParallel(t *testing.T) {
	started := make(chan struct{}, 2)
	release := make(chan struct{})
	var active atomic.Int64
	var maxActive atomic.Int64
	processor := ASREncodedAudioHandoffProcessorFunc(func(ASREncodedAudioHandoff) error {
		current := active.Add(1)
		for {
			observed := maxActive.Load()
			if current <= observed || maxActive.CompareAndSwap(observed, current) {
				break
			}
		}
		started <- struct{}{}
		<-release
		active.Add(-1)
		return nil
	})
	sink := NewConcurrentASREncodedAudioHandoffSink(4, 2, processor)
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_1"})
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_2"})

	for range 2 {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatal("two workers did not start concurrently")
		}
	}
	close(release)
	sink.Close()
	if maxActive.Load() < 2 || sink.Snapshot().Workers != 2 {
		t.Fatalf("max_active=%d snapshot=%+v", maxActive.Load(), sink.Snapshot())
	}
}

func TestConcurrentASREncodedAudioHandoffSinkKeepsOneSessionOrdered(t *testing.T) {
	firstStarted := make(chan struct{})
	releaseFirst := make(chan struct{})
	order := make(chan string, 2)
	processor := ASREncodedAudioHandoffProcessorFunc(func(handoff ASREncodedAudioHandoff) error {
		if handoff.UtteranceID == "first" {
			close(firstStarted)
			<-releaseFirst
		}
		order <- handoff.UtteranceID
		return nil
	})
	sink := NewConcurrentASREncodedAudioHandoffSink(4, 4, processor)
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_1", UtteranceID: "first"})
	<-firstStarted
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_1", UtteranceID: "second"})
	select {
	case got := <-order:
		t.Fatalf("same-session second item overtook first: %s", got)
	case <-time.After(30 * time.Millisecond):
	}
	close(releaseFirst)
	sink.Close()
	if first, second := <-order, <-order; first != "first" || second != "second" {
		t.Fatalf("processing order = %q, %q", first, second)
	}
}

func TestQueuedASREncodedAudioHandoffSinkDropsWhenQueueFull(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	sink := NewQueuedASREncodedAudioHandoffSink(1, ASREncodedAudioHandoffProcessorFunc(func(ASREncodedAudioHandoff) error {
		select {
		case started <- struct{}{}:
		default:
		}
		<-release
		return nil
	}))
	var logs bytes.Buffer
	sink.SetLogger(log.New(&logs, "", 0))

	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_1", TraceID: "trace_1"})
	<-started
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_2", TraceID: "trace_2"})
	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{
		SessionID:           "session_3",
		TraceID:             "trace_3",
		UtteranceID:         "utt_3",
		RetainedPacketCount: 9,
		ObservedPacketCount: 10,
		PacketStreamBytes:   123,
		Lossy:               true,
	})

	snapshot := sink.Snapshot()
	if snapshot.DroppedCount != 1 {
		t.Fatalf("dropped count = %d, want 1", snapshot.DroppedCount)
	}
	logText := logs.String()
	for _, want := range []string{
		"asr_handoff_drop",
		"reason=queue_full",
		"session=session_3",
		"trace_id=trace_3",
		"utterance_id=utt_3",
		"retained_packets=9",
		"observed_packets=10",
		"payload_bytes=123",
		"dropped_total=1",
		"lossy=true",
	} {
		if !strings.Contains(logText, want) {
			t.Fatalf("drop log missing %q in:\n%s", want, logText)
		}
	}
	close(release)
	sink.Close()

	snapshot = sink.Snapshot()
	if snapshot.ProcessedCount != 2 ||
		snapshot.DroppedCount != 1 ||
		snapshot.ErrorCount != 0 {
		t.Fatalf("unexpected final queue snapshot: %+v", snapshot)
	}
}

func TestQueuedASREncodedAudioHandoffSinkCountsProcessorErrors(t *testing.T) {
	wantErr := errors.New("adapter unavailable")
	sink := NewQueuedASREncodedAudioHandoffSink(1, ASREncodedAudioHandoffProcessorFunc(func(ASREncodedAudioHandoff) error {
		return wantErr
	}))

	sink.HandleASREncodedAudioHandoff(ASREncodedAudioHandoff{SessionID: "session_1"})
	sink.Close()

	snapshot := sink.Snapshot()
	if snapshot.ProcessedCount != 1 ||
		snapshot.ErrorCount != 1 ||
		snapshot.LastError != wantErr.Error() {
		t.Fatalf("unexpected error snapshot: %+v", snapshot)
	}
}
