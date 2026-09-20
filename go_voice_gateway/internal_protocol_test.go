package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestInternalVoiceEnvelopeBuildsSessionOpen(t *testing.T) {
	envelope, err := newInternalVoiceEnvelope(
		internalVoiceTypeSessionOpen,
		"rtc_1",
		internalVoiceSessionOpenPayload{
			RobotID:    "test_01",
			ClientType: "rust",
			BotID:      "xiaowen",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if envelope.Version != internalVoiceProtocolVersion ||
		envelope.Type != internalVoiceTypeSessionOpen ||
		envelope.SessionID != "rtc_1" ||
		envelope.TimestampMS <= 0 {
		t.Fatalf("unexpected envelope: %+v", envelope)
	}

	var payload internalVoiceSessionOpenPayload
	if err := envelope.PayloadInto(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.RobotID != "test_01" || payload.ClientType != "rust" || payload.BotID != "xiaowen" {
		t.Fatalf("unexpected payload: %+v", payload)
	}

	encoded, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(encoded), `"type":"session.open"`) ||
		!strings.Contains(string(encoded), `"robot_id":"test_01"`) {
		t.Fatalf("unexpected envelope JSON: %s", encoded)
	}
}

func TestInternalVoiceEnvelopeValidationRequiresAudioIDs(t *testing.T) {
	envelope := internalVoiceEnvelope{
		Version:     internalVoiceProtocolVersion,
		Type:        internalVoiceTypeInputAudioEnd,
		SessionID:   "rtc_1",
		TimestampMS: 100,
		Payload:     json.RawMessage(`{}`),
	}

	err := envelope.Validate()
	if err == nil || !strings.Contains(err.Error(), "utterance_id") {
		t.Fatalf("expected utterance_id validation error, got %v", err)
	}

	envelope.UtteranceID = "utt_1"
	if err := envelope.Validate(); err != nil {
		t.Fatalf("validate with utterance_id: %v", err)
	}
}

func TestInternalVoiceEnvelopeValidationRequiresResponseRoundIDs(t *testing.T) {
	envelope := internalVoiceEnvelope{
		Version:     internalVoiceProtocolVersion,
		Type:        internalVoiceTypeResponseAudio,
		SessionID:   "rtc_1",
		TraceID:     "trace_1",
		TimestampMS: 100,
		Payload: json.RawMessage(`{
			"chunk_seq": 1,
			"codec": "opus",
			"sample_rate": 48000,
			"channels": 1
		}`),
	}

	err := envelope.Validate()
	if err == nil || !strings.Contains(err.Error(), "round_id") {
		t.Fatalf("expected round_id validation error, got %v", err)
	}

	envelope.RoundID = "round_1"
	envelope.PlaybackID = "round_1:playback"
	if err := envelope.Validate(); err != nil {
		t.Fatalf("validate with round/playback: %v", err)
	}

	var payload internalVoiceResponseAudioPayload
	if err := envelope.PayloadInto(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.ChunkSeq != 1 || payload.Codec != "opus" || payload.SampleRate != 48000 {
		t.Fatalf("unexpected response audio payload: %+v", payload)
	}
}

func TestInternalVoiceEnvelopeAllowsProtocolErrorWithoutRoundIDs(t *testing.T) {
	envelope, err := newInternalVoiceEnvelope(
		internalVoiceTypeProtocolError,
		"rtc_1",
		map[string]string{
			"code":    "INVALID_ENVELOPE",
			"message": "bad payload",
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if envelope.Type != internalVoiceTypeProtocolError {
		t.Fatalf("unexpected type: %s", envelope.Type)
	}
}

func TestInternalVoiceProtocolV1CrossLanguageFixtures(t *testing.T) {
	raw, err := os.ReadFile("../test/fixtures/internal_voice_protocol_v1.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures map[string]internalVoiceEnvelope
	if err := json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatal(err)
	}

	ping := fixtures["heartbeat_ping"]
	if err := ping.Validate(); err != nil {
		t.Fatal(err)
	}
	var heartbeat internalVoiceHeartbeatPayload
	if err := ping.PayloadInto(&heartbeat); err != nil {
		t.Fatal(err)
	}
	if ping.Type != internalVoiceTypeHeartbeatPing || heartbeat.Nonce != "ping-1" {
		t.Fatalf("unexpected heartbeat fixture: envelope=%+v payload=%+v", ping, heartbeat)
	}

	cancelled := fixtures["response_cancelled"]
	if err := cancelled.Validate(); err != nil {
		t.Fatal(err)
	}
	var cancelledPayload internalVoiceResponseCancelledPayload
	if err := cancelled.PayloadInto(&cancelledPayload); err != nil {
		t.Fatal(err)
	}
	if cancelled.Type != internalVoiceTypeResponseCancelled ||
		cancelledPayload.Reason != "wake_interrupt" ||
		!cancelledPayload.Expected {
		t.Fatalf("unexpected cancellation fixture: envelope=%+v payload=%+v", cancelled, cancelledPayload)
	}

	responseError := fixtures["response_error"]
	if err := responseError.Validate(); err != nil {
		t.Fatal(err)
	}
	var errorPayload internalVoiceResponseErrorPayload
	if err := responseError.PayloadInto(&errorPayload); err != nil {
		t.Fatal(err)
	}
	if responseError.Type != internalVoiceTypeResponseError ||
		errorPayload.Code != "TTS_UPSTREAM_UNAVAILABLE" ||
		!errorPayload.Retryable ||
		errorPayload.Fatal {
		t.Fatalf("unexpected response error fixture: envelope=%+v payload=%+v", responseError, errorPayload)
	}
}
