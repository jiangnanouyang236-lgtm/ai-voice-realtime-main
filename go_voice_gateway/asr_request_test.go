package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

func TestNewASRAudioRequestFromHandoff(t *testing.T) {
	handoff := ASREncodedAudioHandoff{
		SessionID:                  "session_1",
		BotID:                      "xiaowen",
		TraceID:                    "trace_1",
		UtteranceID:                "utt_1",
		TrackID:                    "microphone",
		StreamID:                   "voice",
		AudioEncoding:              asrHandoffAudioEncodingOpus,
		AudioTransport:             canonicalTransportWebRTCRTP,
		PacketStreamFormat:         opusPacketStreamMagic,
		RetainedPacketCount:        1,
		RetainedPacketPayloadBytes: 3,
		PacketStreamBytes:          13,
		ObservedPacketCount:        1,
		ObservedPayloadBytes:       3,
		DuplicatePackets:           1,
		SequenceGaps:               1,
		MissingPackets:             2,
		TimestampRegressions:       1,
		Lossy:                      true,
		Payload:                    []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c'},
	}

	request, err := NewASRAudioRequestFromHandoff(handoff)
	if err != nil {
		t.Fatal(err)
	}
	if request.Type != asrAudioRequestType ||
		request.SessionID != "session_1" ||
		request.BotID != "xiaowen" ||
		request.TraceID != "trace_1" ||
		request.UtteranceID != "utt_1" ||
		request.TrackID != "microphone" ||
		request.StreamID != "voice" ||
		request.AudioEncoding != "opus" ||
		request.AudioTransport != asrAudioRequestTransportWebRTC ||
		request.PacketStreamFormat != opusPacketStreamMagic ||
		request.SampleRate != 16000 ||
		request.Channels != 1 ||
		request.OpusFrameMS != 20 ||
		request.PacketCount != 1 ||
		request.AudioByteCount != len(handoff.Payload) ||
		request.RetainedPacketPayloadBytes != 3 ||
		request.ObservedPacketCount != 1 ||
		request.ObservedPayloadBytes != 3 ||
		request.DuplicatePackets != 1 ||
		request.SequenceGaps != 1 ||
		request.MissingPackets != 2 ||
		request.TimestampRegressions != 1 ||
		!request.Lossy {
		t.Fatalf("unexpected ASR request: %+v", request)
	}
	if !bytes.Equal(request.AudioBytes, handoff.Payload) {
		t.Fatalf("request payload = %v, want %v", request.AudioBytes, handoff.Payload)
	}
	handoff.Payload[0] = 'X'
	if request.AudioBytes[0] != 'O' {
		t.Fatalf("request payload was not cloned: %q", request.AudioBytes)
	}

	encoded, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encoded, []byte(`"AudioBytes"`)) ||
		bytes.Contains(encoded, []byte(`"audio_bytes"`)) ||
		bytes.Contains(encoded, []byte("T1BVU1JBVzE=")) {
		t.Fatalf("request JSON exposed audio bytes: %s", encoded)
	}
}

func TestNewASRAudioRequestFromHandoffRejectsInvalidInput(t *testing.T) {
	validPayload := []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 1, 'a'}
	tests := []struct {
		name    string
		handoff ASREncodedAudioHandoff
	}{
		{
			name: "unsupported encoding",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:       "pcm16",
				PacketStreamFormat:  opusPacketStreamMagic,
				RetainedPacketCount: 1,
				Payload:             validPayload,
			},
		},
		{
			name: "unsupported packet stream",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:       asrHandoffAudioEncodingOpus,
				PacketStreamFormat:  "other",
				RetainedPacketCount: 1,
				Payload:             validPayload,
			},
		},
		{
			name: "empty payload",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:       asrHandoffAudioEncodingOpus,
				PacketStreamFormat:  opusPacketStreamMagic,
				RetainedPacketCount: 1,
			},
		},
		{
			name: "stream byte mismatch",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:       asrHandoffAudioEncodingOpus,
				PacketStreamFormat:  opusPacketStreamMagic,
				RetainedPacketCount: 1,
				PacketStreamBytes:   len(validPayload) + 1,
				Payload:             validPayload,
			},
		},
		{
			name: "payload magic mismatch",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:       asrHandoffAudioEncodingOpus,
				PacketStreamFormat:  opusPacketStreamMagic,
				RetainedPacketCount: 1,
				Payload:             []byte("not-opus"),
			},
		},
		{
			name: "no retained packets",
			handoff: ASREncodedAudioHandoff{
				AudioEncoding:      asrHandoffAudioEncodingOpus,
				PacketStreamFormat: opusPacketStreamMagic,
				Payload:            validPayload,
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := NewASRAudioRequestFromHandoff(tt.handoff); err == nil {
				t.Fatal("expected error")
			}
		})
	}
}

func TestASRAudioRequestProcessorAdapterConvertsHandoff(t *testing.T) {
	var received ASRAudioRequest
	adapter := NewASRAudioRequestProcessorAdapter(ASRAudioRequestProcessorFunc(func(request ASRAudioRequest) error {
		received = request
		return nil
	}))
	handoff := ASREncodedAudioHandoff{
		SessionID:                  "session_1",
		BotID:                      "xiaowen",
		TraceID:                    "trace_adapter_1",
		UtteranceID:                "utt_1",
		TrackID:                    "microphone",
		StreamID:                   "voice",
		AudioEncoding:              asrHandoffAudioEncodingOpus,
		AudioTransport:             canonicalTransportWebRTCRTP,
		PacketStreamFormat:         opusPacketStreamMagic,
		RetainedPacketCount:        1,
		RetainedPacketPayloadBytes: 3,
		PacketStreamBytes:          13,
		ObservedPacketCount:        1,
		ObservedPayloadBytes:       3,
		Payload:                    []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c'},
	}

	if err := adapter.ProcessASREncodedAudioHandoff(handoff); err != nil {
		t.Fatal(err)
	}
	if received.SessionID != "session_1" ||
		received.BotID != "xiaowen" ||
		received.TraceID != "trace_adapter_1" ||
		received.UtteranceID != "utt_1" ||
		received.AudioTransport != asrAudioRequestTransportWebRTC ||
		received.AudioEncoding != asrAudioRequestEncodingOpus ||
		received.PacketStreamFormat != asrAudioRequestPacketStreamOpus ||
		received.PacketCount != 1 ||
		received.AudioByteCount != len(handoff.Payload) {
		t.Fatalf("unexpected received request: %+v", received)
	}
	if !bytes.Equal(received.AudioBytes, handoff.Payload) {
		t.Fatalf("received payload = %v, want %v", received.AudioBytes, handoff.Payload)
	}
	handoff.Payload[0] = 'X'
	if received.AudioBytes[0] != 'O' {
		t.Fatalf("received request payload was not cloned: %q", received.AudioBytes)
	}
}

func TestASRAudioRequestProcessorAdapterPropagatesRequestErrors(t *testing.T) {
	called := false
	adapter := NewASRAudioRequestProcessorAdapter(ASRAudioRequestProcessorFunc(func(ASRAudioRequest) error {
		called = true
		return nil
	}))

	err := adapter.ProcessASREncodedAudioHandoff(ASREncodedAudioHandoff{
		AudioEncoding:       asrHandoffAudioEncodingOpus,
		PacketStreamFormat:  opusPacketStreamMagic,
		RetainedPacketCount: 1,
		Payload:             []byte("not-opus"),
	})
	if err == nil {
		t.Fatal("expected error")
	}
	if called {
		t.Fatal("processor was called for an invalid request")
	}
}

func TestASRAudioRequestProcessorAdapterPropagatesProcessorErrors(t *testing.T) {
	wantErr := errors.New("asr bridge unavailable")
	adapter := NewASRAudioRequestProcessorAdapter(ASRAudioRequestProcessorFunc(func(ASRAudioRequest) error {
		return wantErr
	}))

	err := adapter.ProcessASREncodedAudioHandoff(ASREncodedAudioHandoff{
		AudioEncoding:              asrHandoffAudioEncodingOpus,
		PacketStreamFormat:         opusPacketStreamMagic,
		RetainedPacketCount:        1,
		RetainedPacketPayloadBytes: 1,
		PacketStreamBytes:          11,
		Payload:                    []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 1, 'a'},
	})
	if !errors.Is(err, wantErr) {
		t.Fatalf("error = %v, want %v", err, wantErr)
	}
}

func TestASRBridgeErrorReportingProcessorNotifiesClient(t *testing.T) {
	downlink := &recordingDownlinkForwarder{}
	processor := newASRBridgeErrorReportingProcessor(
		ASRAudioRequestProcessorFunc(func(ASRAudioRequest) error {
			return errors.New("broken pipe")
		}),
		downlink,
		nil,
	)

	err := processor.ProcessASRAudioRequest(ASRAudioRequest{
		SessionID:   "session_1",
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})

	if err == nil || err.Error() != "broken pipe" {
		t.Fatalf("err = %v, want broken pipe", err)
	}
	if downlink.sessionID != "session_1" {
		t.Fatalf("downlink session = %q, want session_1", downlink.sessionID)
	}
	if !downlink.message.IsJSON || downlink.message.Type != "error" {
		t.Fatalf("downlink message = %+v, want JSON error", downlink.message)
	}
	if downlink.message.JSON["code"] != "ASR_BRIDGE_FAILED" ||
		downlink.message.JSON["trace_id"] != "trace_1" ||
		downlink.message.JSON["utterance_id"] != "utt_1" {
		t.Fatalf("unexpected error payload: %+v", downlink.message.JSON)
	}
	message, _ := downlink.message.JSON["message"].(string)
	if !strings.Contains(message, "broken pipe") {
		t.Fatalf("error message = %q, want broken pipe", message)
	}
}

func TestASRBridgeErrorReportingProcessorDoesNotWrapM1BusinessError(t *testing.T) {
	businessErr := internalVoiceBusinessError{
		payload: internalVoiceResponseErrorPayload{
			Origin:  "python_gateway",
			Stage:   "tts",
			Code:    "TTS_UPSTREAM_UNAVAILABLE",
			Message: "unavailable",
		},
	}
	downlink := &recordingDownlinkForwarder{}
	processor := newASRBridgeErrorReportingProcessor(
		ASRAudioRequestProcessorFunc(func(ASRAudioRequest) error {
			return businessErr
		}),
		downlink,
		nil,
	)
	err := processor.ProcessASRAudioRequest(testASRAudioRequest())
	var gotBusinessErr internalVoiceBusinessError
	if !errors.As(err, &gotBusinessErr) {
		t.Fatalf("error = %v, want M1 business error", err)
	}
	if downlink.message.Type != "" {
		t.Fatalf("unexpected ASR_BRIDGE_FAILED wrapper: %+v", downlink.message)
	}
}

type recordingDownlinkForwarder struct {
	sessionID string
	message   pythonGatewayBridgeMessage
}

func (r *recordingDownlinkForwarder) ForwardPythonGatewayDownlink(sessionID string, message pythonGatewayBridgeMessage) error {
	r.sessionID = sessionID
	r.message = message
	return nil
}
