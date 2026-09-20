package main

import (
	"errors"
	"sync"
	"testing"
	"time"
)

type recordingBargeInCommitProcessor struct {
	mu         sync.Mutex
	calls      []clientMessage
	controlErr error
	done       chan struct{}
}

func (p *recordingBargeInCommitProcessor) ProcessClientControl(_ string, message clientMessage) error {
	p.mu.Lock()
	p.calls = append(p.calls, message)
	p.mu.Unlock()
	if p.controlErr != nil {
		select {
		case p.done <- struct{}{}:
		default:
		}
	}
	return p.controlErr
}

func (p *recordingBargeInCommitProcessor) ProcessClientText(_ string, message clientMessage) error {
	p.mu.Lock()
	p.calls = append(p.calls, message)
	p.mu.Unlock()
	select {
	case p.done <- struct{}{}:
	default:
	}
	return nil
}

func (p *recordingBargeInCommitProcessor) snapshot() []clientMessage {
	p.mu.Lock()
	defer p.mu.Unlock()
	return append([]clientMessage(nil), p.calls...)
}

func waitForBargeInCommit(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("barge-in commit did not finish")
	}
}

func TestCommitBargeInInterruptsBeforePromotingNewIntent(t *testing.T) {
	processor := &recordingBargeInCommitProcessor{done: make(chan struct{}, 1)}
	sink := &configuredASRVoiceEventSink{primaryBridge: processor}
	result := activeBargeInResult()
	sink.commitBargeIn("session-1", result)
	waitForBargeInCommit(t, processor.done)
	calls := processor.snapshot()
	if len(calls) != 2 || calls[0].Type != "interrupt" || calls[0].Reason != "natural_barge_in" ||
		calls[0].TraceID != result.TraceID || calls[0].UtteranceID != result.UtteranceID ||
		calls[0].RoundID != result.RoundID || calls[0].PlaybackID != result.PlaybackID ||
		calls[1].Type != "text" || calls[1].Source != "barge_in_candidate_asr" ||
		calls[1].Content != result.ASRText || calls[1].CandidateSeq != result.CandidateSeq ||
		calls[1].SpeechEpoch != result.SpeechEpoch || calls[1].AudioWatermark != result.AudioWatermark ||
		calls[1].ASRTimeMS != result.ASRTimeMS {
		t.Fatalf("unexpected commit order: %+v", calls)
	}
}

func TestPromoteTurnCandidatePreservesCandidateIdentity(t *testing.T) {
	processor := &recordingBargeInCommitProcessor{done: make(chan struct{}, 1)}
	sink := &configuredASRVoiceEventSink{primaryBridge: processor}
	result := TurnCandidateResult{
		TraceID: "trace-turn-1", UtteranceID: "utt-turn-1", CandidateSeq: 4,
		SpeechEpoch: 3, AudioWatermark: 24_000, ASRText: "好，开始吧", ASRTimeMS: 72.5,
	}
	sink.promoteTurnCandidate("session-1", result)
	waitForBargeInCommit(t, processor.done)
	calls := processor.snapshot()
	if len(calls) != 1 || calls[0].Type != "text" ||
		calls[0].Source != "turn_gate_candidate_asr" || calls[0].TraceID != result.TraceID ||
		calls[0].UtteranceID != result.UtteranceID || calls[0].Content != result.ASRText ||
		calls[0].CandidateSeq != result.CandidateSeq || calls[0].SpeechEpoch != result.SpeechEpoch ||
		calls[0].AudioWatermark != result.AudioWatermark || calls[0].ASRTimeMS != result.ASRTimeMS {
		t.Fatalf("unexpected turn candidate promotion: %+v", calls)
	}
}

func TestCommitBargeInStopDoesNotPromoteText(t *testing.T) {
	processor := &recordingBargeInCommitProcessor{done: make(chan struct{}, 1)}
	sink := &configuredASRVoiceEventSink{primaryBridge: processor}
	result := activeBargeInResult()
	result.Decision = "interrupt"
	sink.commitBargeIn("session-1", result)
	time.Sleep(30 * time.Millisecond)
	calls := processor.snapshot()
	if len(calls) != 1 || calls[0].Type != "interrupt" {
		t.Fatalf("stop decision must only interrupt: %+v", calls)
	}
}

func TestCommitBargeInDoesNotPromoteWhenInterruptForwardingFails(t *testing.T) {
	processor := &recordingBargeInCommitProcessor{
		controlErr: errors.New("bridge unavailable"),
		done:       make(chan struct{}, 1),
	}
	sink := &configuredASRVoiceEventSink{primaryBridge: processor}
	sink.commitBargeIn("session-1", activeBargeInResult())
	waitForBargeInCommit(t, processor.done)
	time.Sleep(10 * time.Millisecond)
	calls := processor.snapshot()
	if len(calls) != 1 || calls[0].Type != "interrupt" {
		t.Fatalf("failed interrupt must stop promotion: %+v", calls)
	}
}

func TestNaturalBargeInInterruptIsForwardedOnlyByAcceptedAck(t *testing.T) {
	if shouldForwardClientInterruptToPrimary("natural_barge_in") {
		t.Fatal("natural barge-in transport interrupt must wait for accepted ACK forwarding")
	}
	if !shouldForwardClientInterruptToPrimary("wake_interrupt") ||
		!shouldForwardClientInterruptToPrimary("") {
		t.Fatal("existing interrupt reasons must keep their original forwarding path")
	}
}
