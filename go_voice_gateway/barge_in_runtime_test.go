package main

import "testing"

func activeBargeInResult() BargeInResult {
	return BargeInResult{
		TraceID:        "trace-1",
		UtteranceID:    "barge-1",
		RoundID:        "round-1",
		PlaybackID:     "playback-1",
		CandidateSeq:   2,
		SpeechEpoch:    1,
		AudioWatermark: 19_200,
		Status:         "observed",
		Decision:       "new_intent",
		Reason:         "meaningful_speech",
		ASRText:        "帮我查一下天气",
		ASRTimeMS:      64.5,
	}
}

func ackForBargeIn(result BargeInResult, accepted bool) CanonicalVoiceEvent {
	return CanonicalVoiceEvent{
		Type:           "barge_in_commit_ack",
		SessionID:      "session-1",
		UtteranceID:    result.UtteranceID,
		RoundID:        result.RoundID,
		PlaybackID:     result.PlaybackID,
		CandidateSeq:   result.CandidateSeq,
		SpeechEpoch:    result.SpeechEpoch,
		AudioWatermark: result.AudioWatermark,
		Accepted:       accepted,
	}
}

func startForBargeIn(result BargeInResult) CanonicalVoiceEvent {
	return CanonicalVoiceEvent{
		Type:        "barge_in_start",
		SessionID:   "session-1",
		UtteranceID: result.UtteranceID,
		RoundID:     result.RoundID,
		PlaybackID:  result.PlaybackID,
		SpeechEpoch: result.SpeechEpoch,
	}
}

func TestBargeInDecisionMessagePreservesIdentity(t *testing.T) {
	result := activeBargeInResult()
	message := newBargeInDecisionMessage(result)
	if message.Type != "barge_in_decision" || message.Decision != "new_intent" ||
		message.UtteranceID != result.UtteranceID || message.RoundID != result.RoundID ||
		message.PlaybackID != result.PlaybackID || message.CandidateSeq != result.CandidateSeq ||
		message.SpeechEpoch != result.SpeechEpoch || message.AudioWatermark != result.AudioWatermark {
		t.Fatalf("barge-in identity changed: %+v", message)
	}
}

func TestBargeInRuntimeAcceptsOnlyExactIdentityOnce(t *testing.T) {
	controller := NewBargeInRuntimeController()
	result := activeBargeInResult()
	if !controller.Start("session-1", startForBargeIn(result)) {
		t.Fatal("valid candidate start should be accepted")
	}
	if !controller.Remember("session-1", result) {
		t.Fatal("current result should be remembered")
	}

	stale := ackForBargeIn(result, true)
	stale.AudioWatermark--
	if accepted, reason := controller.Accept("session-1", stale); accepted != nil || reason != "identity_mismatch" {
		t.Fatalf("stale ack accepted=%+v reason=%s", accepted, reason)
	}

	accepted, reason := controller.Accept("session-1", ackForBargeIn(result, true))
	if accepted == nil || accepted.ASRText != result.ASRText || reason != "accepted" {
		t.Fatalf("exact ack accepted=%+v reason=%s", accepted, reason)
	}
	if duplicate, duplicateReason := controller.Accept("session-1", ackForBargeIn(result, true)); duplicate != nil || duplicateReason != "missing_pending" {
		t.Fatalf("duplicate ack accepted=%+v reason=%s", duplicate, duplicateReason)
	}
}

func TestBargeInRuntimeRejectsOlderAsyncResultAndClientRejection(t *testing.T) {
	controller := NewBargeInRuntimeController()
	current := activeBargeInResult()
	controller.Start("session-1", startForBargeIn(current))
	if !controller.Remember("session-1", current) {
		t.Fatal("current result should be remembered")
	}
	older := current
	older.CandidateSeq--
	older.AudioWatermark -= 3_200
	if controller.Remember("session-1", older) {
		t.Fatal("older async result must not replace the current result")
	}
	if accepted, reason := controller.Accept("session-1", ackForBargeIn(current, false)); accepted != nil || reason != "client_rejected" {
		t.Fatalf("rejected ack accepted=%+v reason=%s", accepted, reason)
	}
}

func TestBargeInRuntimeRejectsResultFromPreviousPlayback(t *testing.T) {
	controller := NewBargeInRuntimeController()
	old := activeBargeInResult()
	controller.Start("session-1", startForBargeIn(old))
	current := old
	current.UtteranceID = "barge-2"
	current.RoundID = "round-2"
	current.PlaybackID = "playback-2"
	controller.Start("session-1", startForBargeIn(current))
	if controller.Remember("session-1", old) {
		t.Fatal("late result from previous playback must be rejected")
	}
	if !controller.Remember("session-1", current) {
		t.Fatal("result for active playback should be remembered")
	}
}

func TestBargeInPythonResultValidation(t *testing.T) {
	result := activeBargeInResult()
	parsed, err := bargeInResultFromPythonMessage(map[string]any{
		"trace_id":        result.TraceID,
		"utterance_id":    result.UtteranceID,
		"round_id":        result.RoundID,
		"playback_id":     result.PlaybackID,
		"candidate_seq":   float64(result.CandidateSeq),
		"speech_epoch":    float64(result.SpeechEpoch),
		"audio_watermark": float64(result.AudioWatermark),
		"status":          result.Status,
		"decision":        result.Decision,
		"reason":          result.Reason,
		"asr_text":        result.ASRText,
	})
	if err != nil || parsed.PlaybackID != result.PlaybackID || parsed.Decision != result.Decision {
		t.Fatalf("parsed=%+v err=%v", parsed, err)
	}

	_, err = bargeInResultFromPythonMessage(map[string]any{
		"utterance_id": "barge-1", "round_id": "round-1", "playback_id": "playback-1",
		"candidate_seq": 1, "speech_epoch": 1, "audio_watermark": 1, "decision": "unknown",
	})
	if err == nil {
		t.Fatal("unknown decision must be rejected")
	}
}
