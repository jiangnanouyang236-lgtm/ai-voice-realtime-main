package main

import (
	"errors"
	"testing"
	"time"
)

func activeTurnCandidateEvent(at time.Time) CanonicalVoiceEvent {
	return CanonicalVoiceEvent{
		Type:           "turn_candidate",
		SessionID:      "session-1",
		TraceID:        "trace-1",
		UtteranceID:    "utt-1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		Shadow:         false,
		At:             at,
	}
}

func activeTurnCandidateResult() TurnCandidateResult {
	return TurnCandidateResult{
		TraceID:        "trace-1",
		UtteranceID:    "utt-1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		Status:         "observed",
		ASRText:        "好的，就这样吧",
		ASRTimeMS:      80,
		PolicyPreview:  "early_commit",
		Shadow:         false,
	}
}

func TestTurnCandidateDecisionMessagePreservesFreshnessIdentity(t *testing.T) {
	result := activeTurnCandidateResult()
	message := newTurnCandidateDecisionMessage(result, "continue", "models_continue")
	if message.Type != "turn_candidate_decision" || message.Decision != "continue" {
		t.Fatalf("unexpected decision message: %+v", message)
	}
	if message.UtteranceID != result.UtteranceID || message.CandidateSeq != result.CandidateSeq ||
		message.SpeechEpoch != result.SpeechEpoch || message.AudioWatermark != result.AudioWatermark {
		t.Fatalf("freshness identity changed: %+v", message)
	}
}

func TestTurnContinueDecisionRequiresHealthyExplicitContinue(t *testing.T) {
	result := activeTurnCandidateResult()
	result.PolicyPreview = "continue"
	if !shouldSendTurnContinueDecision(result) {
		t.Fatal("healthy explicit continue should be sent to the client")
	}
	result.Status = "unavailable"
	if shouldSendTurnContinueDecision(result) {
		t.Fatal("unavailable model result must use failure fallback")
	}
	result.Status = "observed"
	result.PolicyPreview = "early_commit"
	if shouldSendTurnContinueDecision(result) {
		t.Fatal("early commit must use the two-phase commit message")
	}
	result.PolicyPreview = "continue"
	result.Shadow = true
	if shouldSendTurnContinueDecision(result) {
		t.Fatal("shadow result must not alter client timing")
	}
}

func TestTurnGateRuntimeCommitsCurrentCandidateAndSuppressesFallback(t *testing.T) {
	started := time.Now()
	controller := NewTurnGateRuntimeController(200 * time.Millisecond)
	controller.Observe(CanonicalVoiceEvent{Type: "audio_start", SessionID: "session-1", UtteranceID: "utt-1"})
	controller.Observe(activeTurnCandidateEvent(started))
	sends := 0
	requested, reason, err := controller.ProposeIfCurrent("session-1", activeTurnCandidateResult(), started.Add(150*time.Millisecond), func() error {
		sends++
		return nil
	})
	if err != nil || !requested || reason != "commit_requested" || sends != 1 {
		t.Fatalf("unexpected commit request requested=%t reason=%s sends=%d err=%v", requested, reason, sends, err)
	}
	result, ackReason := controller.AcceptAck(CanonicalVoiceEvent{
		Type:           "turn_commit_ack",
		SessionID:      "session-1",
		UtteranceID:    "utt-1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		Accepted:       true,
	})
	if result == nil || ackReason != "active_commit" {
		t.Fatalf("unexpected commit ack result=%+v reason=%s", result, ackReason)
	}
	if !controller.Observe(CanonicalVoiceEvent{Type: "audio_end", SessionID: "session-1", UtteranceID: "utt-1"}) {
		t.Fatal("legacy audio_end must be suppressed after active commit")
	}
	if !controller.Observe(CanonicalVoiceEvent{Type: canonicalEventAudioPacket, SessionID: "session-1"}) {
		t.Fatal("late RTP must be suppressed after active commit")
	}
}

func TestTurnGateRuntimeRejectsResumeFallbackDeadlineAndShadow(t *testing.T) {
	for _, test := range []struct {
		name   string
		mutate func(*TurnGateRuntimeController, time.Time, *TurnCandidateResult)
		reason string
	}{
		{
			name: "speech resumed",
			mutate: func(c *TurnGateRuntimeController, _ time.Time, _ *TurnCandidateResult) {
				c.Observe(CanonicalVoiceEvent{Type: "turn_candidate_cancel", SessionID: "session-1", UtteranceID: "utt-1", SpeechEpoch: 1})
			},
			reason: "no_active_candidate",
		},
		{
			name: "fallback won",
			mutate: func(c *TurnGateRuntimeController, _ time.Time, _ *TurnCandidateResult) {
				c.Observe(CanonicalVoiceEvent{Type: "audio_end", SessionID: "session-1", UtteranceID: "utt-1"})
			},
			reason: "no_active_candidate",
		},
		{
			name: "deadline",
			mutate: func(_ *TurnGateRuntimeController, started time.Time, result *TurnCandidateResult) {
				_ = started
				result.ASRTimeMS = 220
			},
			reason: "deadline_expired",
		},
		{
			name: "shadow result",
			mutate: func(_ *TurnGateRuntimeController, _ time.Time, result *TurnCandidateResult) {
				result.Shadow = true
			},
			reason: "result_not_eligible",
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			started := time.Now()
			controller := NewTurnGateRuntimeController(200 * time.Millisecond)
			controller.Observe(CanonicalVoiceEvent{Type: "audio_start", SessionID: "session-1", UtteranceID: "utt-1"})
			controller.Observe(activeTurnCandidateEvent(started))
			result := activeTurnCandidateResult()
			test.mutate(controller, started, &result)
			now := started.Add(100 * time.Millisecond)
			if test.name == "deadline" {
				now = started.Add(220 * time.Millisecond)
			}
			requested, reason, err := controller.ProposeIfCurrent("session-1", result, now, func() error { return nil })
			if err != nil || requested || reason != test.reason {
				t.Fatalf("requested=%t reason=%s err=%v", requested, reason, err)
			}
		})
	}
}

func TestTurnGateRuntimeDoesNotCommitWhenClientSendFails(t *testing.T) {
	started := time.Now()
	controller := NewTurnGateRuntimeController(200 * time.Millisecond)
	controller.Observe(CanonicalVoiceEvent{Type: "audio_start", SessionID: "session-1", UtteranceID: "utt-1"})
	controller.Observe(activeTurnCandidateEvent(started))
	wantErr := errors.New("datachannel closed")
	requested, reason, err := controller.ProposeIfCurrent("session-1", activeTurnCandidateResult(), started.Add(100*time.Millisecond), func() error {
		return wantErr
	})
	if requested || reason != "commit_send_failed" || !errors.Is(err, wantErr) {
		t.Fatalf("requested=%t reason=%s err=%v", requested, reason, err)
	}
	if controller.Observe(CanonicalVoiceEvent{Type: "audio_end", SessionID: "session-1", UtteranceID: "utt-1"}) {
		t.Fatal("fallback audio_end must remain available after send failure")
	}
}

func TestTurnGateRuntimeClientRejectionKeepsFallbackAvailable(t *testing.T) {
	started := time.Now()
	controller := NewTurnGateRuntimeController(200 * time.Millisecond)
	controller.Observe(CanonicalVoiceEvent{Type: "audio_start", SessionID: "session-1", UtteranceID: "utt-1"})
	controller.Observe(activeTurnCandidateEvent(started))
	requested, reason, err := controller.ProposeIfCurrent(
		"session-1",
		activeTurnCandidateResult(),
		started.Add(100*time.Millisecond),
		func() error { return nil },
	)
	if err != nil || !requested || reason != "commit_requested" {
		t.Fatalf("requested=%t reason=%s err=%v", requested, reason, err)
	}
	result, ackReason := controller.AcceptAck(CanonicalVoiceEvent{
		Type:           "turn_commit_ack",
		SessionID:      "session-1",
		UtteranceID:    "utt-1",
		CandidateSeq:   1,
		SpeechEpoch:    0,
		AudioWatermark: 16_000,
		Accepted:       false,
	})
	if result != nil || ackReason != "client_rejected" {
		t.Fatalf("result=%+v reason=%s", result, ackReason)
	}
	if controller.Observe(CanonicalVoiceEvent{Type: "audio_end", SessionID: "session-1", UtteranceID: "utt-1"}) {
		t.Fatal("fallback audio_end must remain available after client rejection")
	}
}
