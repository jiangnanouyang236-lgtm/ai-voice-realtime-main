package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"strings"
	"sync"
	"testing"
	"time"

	"golang.org/x/net/websocket"
)

func TestInternalVoiceClientOpenAndCloseSession(t *testing.T) {
	openedCh := make(chan internalVoiceEnvelope, 1)
	closedCh := make(chan internalVoiceEnvelope, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		openedCh <- opened
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		closedCh <- closed
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{
		SessionID:  "rtc_1",
		RobotID:    "test_01",
		ClientType: "rust",
		BotID:      "xiaowen",
		Source:     "go_test",
		Transport:  "webrtc",
		BridgeMode: "m1_handshake_probe",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	opened := <-openedCh
	if opened.Type != internalVoiceTypeSessionOpen || opened.SessionID != "rtc_1" {
		t.Fatalf("unexpected open envelope: %+v", opened)
	}
	var openPayload internalVoiceSessionOpenPayload
	if err := opened.PayloadInto(&openPayload); err != nil {
		t.Fatal(err)
	}
	if openPayload.RobotID != "test_01" ||
		openPayload.ClientType != "rust" ||
		openPayload.BotID != "xiaowen" ||
		openPayload.Source != "go_test" ||
		openPayload.Transport != "webrtc" ||
		openPayload.BridgeMode != "m1_handshake_probe" {
		t.Fatalf("unexpected open payload: %+v", openPayload)
	}
	closed := <-closedCh
	if closed.Type != internalVoiceTypeSessionClose || closed.SessionID != "rtc_1" {
		t.Fatalf("unexpected close envelope: %+v", closed)
	}
	status := client.Status()
	if status["open_successes"] != int64(1) ||
		status["close_successes"] != int64(1) ||
		status["active_sessions"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientSupportsTenConcurrentSessions(t *testing.T) {
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		if event.SessionID != opened.SessionID {
			t.Errorf("event session = %q, want %q", event.SessionID, opened.SessionID)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send status: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	const sessionCount = 10
	errs := make(chan error, sessionCount)
	var wg sync.WaitGroup
	for index := 0; index < sessionCount; index++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			sessionID := fmt.Sprintf("rtc_concurrent_%02d", index)
			session, err := client.OpenSession(
				context.Background(),
				InternalVoiceSessionOpenRequest{SessionID: sessionID},
			)
			if err != nil {
				errs <- fmt.Errorf("%s open: %w", sessionID, err)
				return
			}
			if err := session.SendClientEvent(context.Background(), clientMessage{
				Type:    "client_event",
				Event:   "wake_interrupt",
				EventID: "event_" + sessionID,
			}, canonicalTransportWebRTCControl); err != nil {
				errs <- fmt.Errorf("%s event: %w", sessionID, err)
				_ = session.Close(context.Background())
				return
			}
			if err := session.Close(context.Background()); err != nil {
				errs <- fmt.Errorf("%s close: %w", sessionID, err)
			}
		}(index)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	status := client.Status()
	if status["open_successes"] != int64(sessionCount) ||
		status["client_event_successes"] != int64(sessionCount) ||
		status["close_successes"] != int64(sessionCount) ||
		status["active_sessions"] != int64(0) {
		t.Fatalf("unexpected concurrent session status: %+v", status)
	}
}

func TestInternalVoiceClientSendsClientEventAndReceivesStatus(t *testing.T) {
	eventCh := make(chan internalVoiceEnvelope, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		eventCh <- event
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send status: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	err = session.SendClientEvent(context.Background(), clientMessage{
		Type:    "client_event",
		Event:   "wake_interrupt",
		EventID: "event_1",
		Source:  "hardware_wake",
		BotID:   "xiaowen",
	}, canonicalTransportWebRTCControl)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	event := <-eventCh
	if event.Type != internalVoiceTypeClientEvent ||
		event.SessionID != "rtc_1" ||
		event.TraceID == "" {
		t.Fatalf("unexpected client_event envelope: %+v", event)
	}
	var eventPayload internalVoiceClientEventPayload
	if err := event.PayloadInto(&eventPayload); err != nil {
		t.Fatal(err)
	}
	if eventPayload.Event != "wake_interrupt" ||
		eventPayload.EventID != "event_1" ||
		eventPayload.Source != "hardware_wake" ||
		eventPayload.BotID != "xiaowen" {
		t.Fatalf("unexpected client_event payload: %+v", eventPayload)
	}
	status := client.Status()
	if status["client_event_successes"] != int64(1) ||
		status["client_event_failures"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientSendsClientEventActiveAndForwardsDownlink(t *testing.T) {
	eventCh := make(chan internalVoiceEnvelope, 1)
	binaryPayload := []byte{1, 2, 3, 4}
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		eventCh <- event
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send client_event acceptance: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"trace_id":    event.TraceID,
			"round_id":    "round_1",
			"playback_id": "playback_1",
		}); err != nil {
			t.Errorf("send playback_start: %v", err)
			return
		}
		if err := websocket.Message.Send(conn, binaryPayload); err != nil {
			t.Errorf("send binary: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "done",
			"exit":        false,
			"trace_id":    event.TraceID,
			"round_id":    "round_1",
			"playback_id": "playback_1",
		}); err != nil {
			t.Errorf("send done: %v", err)
			return
		}
		responseDone := internalVoiceEnvelope{
			Version:     internalVoiceProtocolVersion,
			Type:        internalVoiceTypeResponseDone,
			SessionID:   event.SessionID,
			TraceID:     event.TraceID,
			RoundID:     "round_1",
			PlaybackID:  "playback_1",
			TimestampMS: internalVoiceNowMillis(),
			Payload:     json.RawMessage(`{"status":"completed"}`),
		}
		if err := websocket.JSON.Send(conn, responseDone); err != nil {
			t.Errorf("send response.done: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:              true,
		ClientEventMode:      "active",
		ClientEventTimeoutMS: 1000,
		WSURL:                defaultInternalVoiceWSURL,
		TimeoutMS:            1000,
		MaxMessageKB:         64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	summary, err := session.SendClientEventActive(context.Background(), clientMessage{
		Type:    "client_event",
		Event:   "wake_interrupt",
		EventID: "event_1",
		Source:  "hardware_wake",
	}, canonicalTransportWebRTCControl, downlink)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	event := <-eventCh
	if event.Type != internalVoiceTypeClientEvent || event.TraceID == "" {
		t.Fatalf("unexpected active client_event envelope: %+v", event)
	}
	if summary.TextMessages != 2 || summary.BinaryMessages != 1 || summary.BinaryBytes != len(binaryPayload) {
		t.Fatalf("unexpected active summary: %+v", summary)
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 3 {
		t.Fatalf("forwarded messages = %d, want 3: %+v", len(forwarded), forwarded)
	}
	if forwarded[0].sessionID != "rtc_1" || !forwarded[0].message.IsJSON || forwarded[0].message.Type != "playback_start" {
		t.Fatalf("unexpected playback_start downlink: %+v", forwarded[0])
	}
	if forwarded[1].message.IsJSON || !bytes.Equal(forwarded[1].message.Binary, binaryPayload) {
		t.Fatalf("unexpected binary downlink: %+v", forwarded[1])
	}
	if !forwarded[2].message.IsJSON || forwarded[2].message.Type != "done" {
		t.Fatalf("unexpected done downlink: %+v", forwarded[2])
	}
	status := client.Status()
	if status["client_event_mode"] != "active" ||
		status["client_event_timeout_ms"] != int64(1000) ||
		status["client_event_successes"] != int64(1) ||
		status["client_event_failures"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientActiveForwardsStructuredPythonGatewayError(t *testing.T) {
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send client_event acceptance: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"trace_id":    event.TraceID,
			"round_id":    "round_1",
			"playback_id": "playback_1",
		}); err != nil {
			t.Errorf("send playback_start: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":    "error",
			"code":    "TTS_FAILED",
			"message": "Channel closed!",
		}); err != nil {
			t.Errorf("send error: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceResponseErrorForTest(t, event, "TTS_FAILED", "Channel closed!")); err != nil {
			t.Errorf("send response.error: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:              true,
		ClientEventMode:      "active",
		ClientEventTimeoutMS: 1000,
		WSURL:                defaultInternalVoiceWSURL,
		TimeoutMS:            1000,
		MaxMessageKB:         64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	summary, err := session.SendClientEventActive(context.Background(), clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "event_1",
	}, canonicalTransportWebRTCControl, downlink)
	if err == nil || !strings.Contains(err.Error(), "TTS_FAILED") {
		t.Fatalf("error = %v, want TTS_FAILED", err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	if summary.TextMessages != 3 || summary.BinaryMessages != 0 || summary.LastType != "playback_cancel" {
		t.Fatalf("unexpected summary: %+v", summary)
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 4 {
		t.Fatalf("forwarded messages = %d, want playback_start, legacy error, response.error, playback_cancel: %+v", len(forwarded), forwarded)
	}
	if !forwarded[0].message.IsJSON || forwarded[0].message.Type != "playback_start" {
		t.Fatalf("unexpected forwarded message: %+v", forwarded[0])
	}
	if forwarded[1].message.Type != "error" ||
		forwarded[2].message.Type != internalVoiceTypeResponseError ||
		forwarded[3].message.Type != "playback_cancel" {
		t.Fatalf("unexpected error downlinks: %+v", forwarded)
	}
}

func TestInternalVoiceClientActiveForwardsErrorAfterAudioDownlink(t *testing.T) {
	binaryPayload := []byte{9, 8, 7}
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send client_event acceptance: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"trace_id":    event.TraceID,
			"round_id":    "round_1",
			"playback_id": "playback_1",
		}); err != nil {
			t.Errorf("send playback_start: %v", err)
			return
		}
		if err := websocket.Message.Send(conn, binaryPayload); err != nil {
			t.Errorf("send binary: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":    "error",
			"code":    "TTS_FAILED",
			"message": "stream failed after audio",
		}); err != nil {
			t.Errorf("send error: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceResponseErrorForTest(t, event, "TTS_FAILED", "stream failed after audio")); err != nil {
			t.Errorf("send response.error: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:              true,
		ClientEventMode:      "active",
		ClientEventTimeoutMS: 1000,
		WSURL:                defaultInternalVoiceWSURL,
		TimeoutMS:            1000,
		MaxMessageKB:         64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	summary, err := session.SendClientEventActive(context.Background(), clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "event_1",
	}, canonicalTransportWebRTCControl, downlink)
	if err == nil || !strings.Contains(err.Error(), "TTS_FAILED") {
		t.Fatalf("error = %v, want TTS_FAILED", err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	if summary.TextMessages != 3 || summary.BinaryMessages != 1 || summary.BinaryBytes != len(binaryPayload) || summary.LastType != "playback_cancel" {
		t.Fatalf("unexpected summary: %+v", summary)
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 5 {
		t.Fatalf("forwarded messages = %d, want playback_start, binary, error, response.error, playback_cancel: %+v", len(forwarded), forwarded)
	}
	if !forwarded[0].message.IsJSON || forwarded[0].message.Type != "playback_start" {
		t.Fatalf("unexpected playback_start downlink: %+v", forwarded[0])
	}
	if forwarded[1].message.IsJSON || !bytes.Equal(forwarded[1].message.Binary, binaryPayload) {
		t.Fatalf("unexpected binary downlink: %+v", forwarded[1])
	}
	if !forwarded[2].message.IsJSON || forwarded[2].message.Type != "error" {
		t.Fatalf("unexpected error downlink: %+v", forwarded[2])
	}
	if !forwarded[3].message.IsJSON || forwarded[3].message.Type != internalVoiceTypeResponseError {
		t.Fatalf("unexpected structured error downlink: %+v", forwarded[3])
	}
	if !forwarded[4].message.IsJSON || forwarded[4].message.Type != "playback_cancel" ||
		stringValue(forwarded[4].message.JSON["reason"]) != "response_error:TTS_FAILED" {
		t.Fatalf("unexpected error terminal cancellation downlink: %+v", forwarded[4])
	}
}

func TestInternalVoiceClientSendsInputAudioMetadataAndReceivesStatus(t *testing.T) {
	eventsCh := make(chan internalVoiceEnvelope, 3)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		for i := 0; i < 3; i++ {
			var event internalVoiceEnvelope
			if err := websocket.JSON.Receive(conn, &event); err != nil {
				t.Errorf("receive input_audio %d: %v", i, err)
				return
			}
			eventsCh <- event
			if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
				t.Errorf("send status: %v", err)
				return
			}
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	audio := AudioConfig{Codec: "opus", SampleRate: 48000, Channels: 1, PtimeMS: 20}
	for _, msg := range []clientMessage{
		{Type: "audio_start", UtteranceID: "utt_1", TraceID: "trace_1"},
		{Type: "audio_end", UtteranceID: "utt_1", TraceID: "trace_1"},
		{Type: "audio_cancel", UtteranceID: "utt_1", TraceID: "trace_1", Reason: "interrupt", Source: "hardware_wake"},
	} {
		if err := session.SendInputAudioMetadata(context.Background(), msg, canonicalTransportWebRTCControl, audio); err != nil {
			t.Fatal(err)
		}
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	start := <-eventsCh
	if start.Type != internalVoiceTypeInputAudioStart ||
		start.SessionID != "rtc_1" ||
		start.UtteranceID != "utt_1" ||
		start.TraceID == "" {
		t.Fatalf("unexpected input_audio.start envelope: %+v", start)
	}
	var startPayload internalVoiceInputAudioStartPayload
	if err := start.PayloadInto(&startPayload); err != nil {
		t.Fatal(err)
	}
	if startPayload.Codec != "opus" ||
		startPayload.PacketFormat != "rtp_opus" ||
		startPayload.SampleRate != 48000 ||
		startPayload.Channels != 1 ||
		startPayload.FrameMS != 20 {
		t.Fatalf("unexpected input_audio.start payload: %+v", startPayload)
	}

	end := <-eventsCh
	if end.Type != internalVoiceTypeInputAudioEnd || end.UtteranceID != "utt_1" {
		t.Fatalf("unexpected input_audio.end envelope: %+v", end)
	}
	var endPayload internalVoiceInputAudioEndPayload
	if err := end.PayloadInto(&endPayload); err != nil {
		t.Fatal(err)
	}
	if endPayload.PacketCount != 0 || endPayload.PayloadBytes != 0 || endPayload.DurationMS != 0 {
		t.Fatalf("unexpected input_audio.end payload: %+v", endPayload)
	}

	cancel := <-eventsCh
	if cancel.Type != internalVoiceTypeInputAudioCancel || cancel.UtteranceID != "utt_1" {
		t.Fatalf("unexpected input_audio.cancel envelope: %+v", cancel)
	}
	var cancelPayload internalVoiceInputAudioCancelPayload
	if err := cancel.PayloadInto(&cancelPayload); err != nil {
		t.Fatal(err)
	}
	if cancelPayload.Reason != "interrupt" ||
		cancelPayload.Source != "hardware_wake" ||
		cancelPayload.Transport != canonicalTransportWebRTCControl {
		t.Fatalf("unexpected input_audio.cancel payload: %+v", cancelPayload)
	}

	status := client.Status()
	if status["input_audio_successes"] != int64(3) ||
		status["input_audio_failures"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientSendsInputTextCommitOnActiveSession(t *testing.T) {
	requestCh := make(chan internalVoiceEnvelope, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var request internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &request); err != nil {
			t.Errorf("receive input_text.commit: %v", err)
			return
		}
		requestCh <- request
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, request)); err != nil {
			t.Errorf("send acceptance: %v", err)
			return
		}
		for _, eventType := range []string{internalVoiceTypeResponseASR, internalVoiceTypeResponseDone} {
			response := internalVoiceEnvelope{
				Version: internalVoiceProtocolVersion, Type: eventType, SessionID: request.SessionID,
				TraceID: request.TraceID, UtteranceID: request.UtteranceID,
				RoundID: "round_text_1", PlaybackID: "playback_text_1",
				TimestampMS: internalVoiceNowMillis(), Payload: json.RawMessage(`{"ok":true}`),
			}
			if err := websocket.JSON.Send(conn, response); err != nil {
				t.Errorf("send %s: %v", eventType, err)
				return
			}
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		_ = websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID))
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled: true, WSURL: defaultInternalVoiceWSURL, TimeoutMS: 1000, MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)
	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_agent_1"})
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	summary, err := session.SendInputTextActive(context.Background(), clientMessage{
		Type: "text", Content: "可以开始吧", Source: "turn_gate_candidate_asr",
		TraceID: "trace_text_1", UtteranceID: "utt_text_1", ASRTimeMS: 88.5,
		CandidateSeq: 3, SpeechEpoch: 2, AudioWatermark: 9600,
	}, downlink)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	request := <-requestCh
	if request.Type != internalVoiceTypeInputTextCommit || request.SessionID != "rtc_agent_1" ||
		request.TraceID != "trace_text_1" || request.UtteranceID != "utt_text_1" {
		t.Fatalf("unexpected input_text.commit envelope: %+v", request)
	}
	var payload internalVoiceInputTextCommitPayload
	if err := request.PayloadInto(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.Content != "可以开始吧" || payload.Source != "turn_gate_candidate_asr" ||
		payload.ASRTimeMS != 88.5 || payload.CandidateSeq != 3 || payload.SpeechEpoch != 2 ||
		payload.AudioWatermark != 9600 {
		t.Fatalf("unexpected input_text.commit payload: %+v", payload)
	}
	if !summary.RequestCommitted || !summary.Accepted || summary.TextMessages != 1 ||
		summary.LastType != internalVoiceTypeResponseASR {
		t.Fatalf("unexpected active summary: %+v", summary)
	}
}

func TestInternalVoiceClientSendsCorrelatedInputAudioBatch(t *testing.T) {
	frameCh := make(chan []byte, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var started internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &started); err != nil {
			t.Errorf("receive input_audio.start: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, started)); err != nil {
			t.Errorf("send start status: %v", err)
			return
		}
		var frame []byte
		if err := websocket.Message.Receive(conn, &frame); err != nil {
			t.Errorf("receive input_audio.batch: %v", err)
			return
		}
		frameCh <- append([]byte(nil), frame...)
		header, _, err := splitPythonGatewayAudioFrame(frame)
		if err != nil {
			t.Errorf("decode input_audio.batch: %v", err)
			return
		}
		request, err := newInternalVoiceInputAudioEnvelope(
			internalVoiceTypeInputAudioBatch,
			header.SessionID,
			header.UtteranceID,
			struct{}{},
		)
		if err != nil {
			t.Errorf("build batch request: %v", err)
			return
		}
		request.TraceID = header.TraceID
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, request)); err != nil {
			t.Errorf("send status: %v", err)
			return
		}
		var committed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &committed); err != nil {
			t.Errorf("receive input_audio.end: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, committed)); err != nil {
			t.Errorf("send commit status: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)
	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "session_1"})
	if err != nil {
		t.Fatal(err)
	}
	request := testASRAudioRequest()
	if err := session.SendInputAudioBegin(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if err := session.SendInputAudioBatch(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if err := session.SendInputAudioCommit(context.Background(), request); err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	header, payload, err := splitPythonGatewayAudioFrame(<-frameCh)
	if err != nil {
		t.Fatal(err)
	}
	if header.EventType != internalVoiceTypeInputAudioBatch ||
		header.Direction != "uplink" ||
		header.SessionID != request.SessionID ||
		header.TraceID != request.TraceID ||
		header.UtteranceID != request.UtteranceID ||
		header.PayloadBytes != len(request.AudioBytes) ||
		header.PacketCount != request.PacketCount ||
		!bytes.Equal(payload, request.AudioBytes) {
		t.Fatalf("unexpected input_audio.batch header=%+v payload=%v", header, payload)
	}
	status := client.Status()
	if status["input_batch_successes"] != int64(1) ||
		status["input_batch_failures"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientRunsActiveInputAudioTurn(t *testing.T) {
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		var started internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &started); err != nil {
			t.Errorf("receive input_audio.start: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, started)); err != nil {
			t.Errorf("send start status: %v", err)
			return
		}
		var frame []byte
		if err := websocket.Message.Receive(conn, &frame); err != nil {
			t.Errorf("receive input_audio.batch: %v", err)
			return
		}
		header, _, err := splitPythonGatewayAudioFrame(frame)
		if err != nil {
			t.Errorf("decode input_audio.batch: %v", err)
			return
		}
		batch, err := newInternalVoiceInputAudioEnvelope(
			internalVoiceTypeInputAudioBatch,
			header.SessionID,
			header.UtteranceID,
			struct{}{},
		)
		if err != nil {
			t.Errorf("build batch envelope: %v", err)
			return
		}
		batch.TraceID = header.TraceID
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, batch)); err != nil {
			t.Errorf("send batch status: %v", err)
			return
		}
		var ended internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &ended); err != nil {
			t.Errorf("receive input_audio.end: %v", err)
			return
		}
		var endPayload internalVoiceInputAudioEndPayload
		if err := ended.PayloadInto(&endPayload); err != nil {
			t.Errorf("decode input_audio.end: %v", err)
			return
		}
		if endPayload.OrchestrationMode != "active" {
			t.Errorf("orchestration_mode = %q, want active", endPayload.OrchestrationMode)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, ended)); err != nil {
			t.Errorf("send end acceptance: %v", err)
			return
		}
		roundID := "session_1:1"
		playbackID := roundID + ":playback"
		if err := websocket.JSON.Send(conn, internalVoiceEnvelope{
			Version:     internalVoiceProtocolVersion,
			Type:        internalVoiceTypeResponseASR,
			SessionID:   ended.SessionID,
			TraceID:     ended.TraceID,
			UtteranceID: ended.UtteranceID,
			RoundID:     roundID,
			PlaybackID:  playbackID,
			TimestampMS: internalVoiceNowMillis(),
			Payload:     json.RawMessage(`{"text":"今天天气不错","valid":true,"final":true,"asr_time_ms":123.5}`),
		}); err != nil {
			t.Errorf("send response.asr: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"trace_id":    ended.TraceID,
			"round_id":    roundID,
			"playback_id": playbackID,
		}); err != nil {
			t.Errorf("send playback_start: %v", err)
			return
		}
		if err := websocket.Message.Send(conn, []byte("VAF1-response-audio")); err != nil {
			t.Errorf("send response audio: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "done",
			"trace_id":    ended.TraceID,
			"round_id":    roundID,
			"playback_id": playbackID,
		}); err != nil {
			t.Errorf("send done: %v", err)
			return
		}
		terminalPayload, err := marshalInternalVoicePayload(
			internalVoiceResponseDonePayload{Reason: "completed"},
		)
		if err != nil {
			t.Errorf("build response.done: %v", err)
			return
		}
		terminal := internalVoiceEnvelope{
			Version:     internalVoiceProtocolVersion,
			Type:        internalVoiceTypeResponseDone,
			SessionID:   ended.SessionID,
			TraceID:     ended.TraceID,
			UtteranceID: ended.UtteranceID,
			RoundID:     roundID,
			PlaybackID:  playbackID,
			TimestampMS: internalVoiceNowMillis(),
			Payload:     terminalPayload,
		}
		if err := websocket.JSON.Send(conn, terminal); err != nil {
			t.Errorf("send response.done: %v", err)
			return
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)
	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "session_1"})
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	summary, err := session.SendInputAudioActive(
		context.Background(),
		testASRAudioRequest(),
		downlink,
	)
	if err != nil {
		t.Fatal(err)
	}
	if !summary.RequestCommitted || !summary.Accepted ||
		summary.TextMessages != 3 ||
		summary.BinaryMessages != 1 {
		t.Fatalf("unexpected active summary: %+v", summary)
	}
	messages := downlink.Messages()
	if len(messages) != 4 ||
		messages[0].message.Type != internalVoiceTypeResponseASR ||
		messages[1].message.Type != "playback_start" ||
		messages[2].message.IsJSON ||
		messages[3].message.Type != "done" {
		t.Fatalf("unexpected active downlink: %+v", messages)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestInternalVoiceClientSendsInterruptAndPlaybackReport(t *testing.T) {
	eventsCh := make(chan internalVoiceEnvelope, 2)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, opened.SessionID)); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		for i := 0; i < 2; i++ {
			var event internalVoiceEnvelope
			if err := websocket.JSON.Receive(conn, &event); err != nil {
				t.Errorf("receive control %d: %v", i, err)
				return
			}
			eventsCh <- event
			if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
				t.Errorf("send status: %v", err)
				return
			}
		}
		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionClosed, closed.SessionID)); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := session.SendInterrupt(context.Background(), clientMessage{
		Type:        "interrupt",
		UtteranceID: "utt_1",
		TraceID:     "trace_interrupt",
		Reason:      "wake_interrupt",
		Source:      "hardware_wake",
	}, canonicalTransportWebRTCControl); err != nil {
		t.Fatal(err)
	}
	firstAudioMS := 117.5
	pushedChunks := uint64(72)
	pushedSamples := uint64(23040)
	underruns := uint64(0)
	if err := session.SendPlaybackReport(context.Background(), clientMessage{
		Type:                        "playback_complete",
		TraceID:                     "trace_playback",
		RoundID:                     "round_1",
		PlaybackID:                  "playback_1",
		FirstAudioToPlaybackStartMS: &firstAudioMS,
		PushedChunks:                &pushedChunks,
		PushedSamples:               &pushedSamples,
		UnderrunCallbacks:           &underruns,
	}, canonicalTransportWebRTCControl); err != nil {
		t.Fatal(err)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}

	interrupt := <-eventsCh
	if interrupt.Type != internalVoiceTypeInterrupt ||
		interrupt.SessionID != "rtc_1" ||
		interrupt.UtteranceID != "utt_1" ||
		interrupt.TraceID == "" {
		t.Fatalf("unexpected interrupt envelope: %+v", interrupt)
	}
	var interruptPayload internalVoiceInterruptPayload
	if err := interrupt.PayloadInto(&interruptPayload); err != nil {
		t.Fatal(err)
	}
	if interruptPayload.Reason != "wake_interrupt" ||
		interruptPayload.Source != "hardware_wake" ||
		interruptPayload.Transport != canonicalTransportWebRTCControl ||
		interruptPayload.UtteranceID != "utt_1" {
		t.Fatalf("unexpected interrupt payload: %+v", interruptPayload)
	}

	playback := <-eventsCh
	if playback.Type != internalVoiceTypePlaybackReport ||
		playback.SessionID != "rtc_1" ||
		playback.RoundID != "round_1" ||
		playback.PlaybackID != "playback_1" ||
		playback.TraceID == "" {
		t.Fatalf("unexpected playback.report envelope: %+v", playback)
	}
	var playbackPayload internalVoicePlaybackReportPayload
	if err := playback.PayloadInto(&playbackPayload); err != nil {
		t.Fatal(err)
	}
	if playbackPayload.ReportType != "playback_complete" ||
		playbackPayload.FirstAudioToPlaybackStartMS == nil ||
		*playbackPayload.FirstAudioToPlaybackStartMS != firstAudioMS ||
		playbackPayload.PushedChunks == nil ||
		*playbackPayload.PushedChunks != pushedChunks ||
		playbackPayload.PushedSamples == nil ||
		*playbackPayload.PushedSamples != pushedSamples {
		t.Fatalf("unexpected playback.report payload: %+v", playbackPayload)
	}

	status := client.Status()
	if status["interrupt_successes"] != int64(1) ||
		status["interrupt_failures"] != int64(0) ||
		status["playback_successes"] != int64(1) ||
		status["playback_failures"] != int64(0) {
		t.Fatalf("unexpected status: %+v", status)
	}
}

func TestInternalVoiceClientInterruptIsNotBlockedByActiveTurn(t *testing.T) {
	eventReceived := make(chan internalVoiceEnvelope, 1)
	interruptReceived := make(chan internalVoiceEnvelope, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(
			conn,
			mustInternalVoiceEnvelopeForTest(
				t,
				internalVoiceTypeSessionOpened,
				opened.SessionID,
			),
		); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}

		var event internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &event); err != nil {
			t.Errorf("receive client_event: %v", err)
			return
		}
		eventReceived <- event
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, event)); err != nil {
			t.Errorf("send client_event acceptance: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"trace_id":    event.TraceID,
			"round_id":    "round_1",
			"playback_id": "round_1:playback",
		}); err != nil {
			t.Errorf("send playback_start: %v", err)
			return
		}

		var interrupt internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &interrupt); err != nil {
			t.Errorf("receive interrupt: %v", err)
			return
		}
		interruptReceived <- interrupt
		if err := websocket.JSON.Send(conn, mustInternalVoiceStatusForTest(t, interrupt)); err != nil {
			t.Errorf("send interrupt status: %v", err)
			return
		}

		cancelled := internalVoiceEnvelope{
			Version:     internalVoiceProtocolVersion,
			Type:        internalVoiceTypeResponseCancelled,
			SessionID:   event.SessionID,
			TraceID:     event.TraceID,
			RoundID:     "round_1",
			PlaybackID:  "round_1:playback",
			TimestampMS: internalVoiceNowMillis(),
			Payload: json.RawMessage(
				`{"reason":"wake_interrupt","cancelled_stage":"tts","expected":true}`,
			),
		}
		if err := websocket.JSON.Send(conn, cancelled); err != nil {
			t.Errorf("send response.cancelled: %v", err)
			return
		}

		var closed internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &closed); err != nil {
			t.Errorf("receive close: %v", err)
			return
		}
		if err := websocket.JSON.Send(
			conn,
			mustInternalVoiceEnvelopeForTest(
				t,
				internalVoiceTypeSessionClosed,
				closed.SessionID,
			),
		); err != nil {
			t.Errorf("send closed: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:              true,
		ClientEventMode:      "active",
		ClientEventTimeoutMS: 1000,
		WSURL:                defaultInternalVoiceWSURL,
		TimeoutMS:            1000,
		MaxMessageKB:         64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	session, err := client.OpenSession(
		context.Background(),
		InternalVoiceSessionOpenRequest{SessionID: "rtc_1"},
	)
	if err != nil {
		t.Fatal(err)
	}
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	turnDone := make(chan error, 1)
	go func() {
		_, err := session.SendClientEventActive(
			context.Background(),
			clientMessage{
				Type:    "client_event",
				Event:   "wake_idle",
				EventID: "event_1",
				TraceID: "trace_event_1",
			},
			canonicalTransportWebRTCControl,
			downlink,
		)
		turnDone <- err
	}()

	select {
	case <-eventReceived:
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active client_event")
	}

	interruptStartedAt := time.Now()
	if err := session.SendInterrupt(
		context.Background(),
		clientMessage{
			Type:    "interrupt",
			TraceID: "trace_interrupt_1",
			Reason:  "wake_interrupt",
			Source:  "hardware_wake",
		},
		canonicalTransportWebRTCControl,
	); err != nil {
		t.Fatal(err)
	}
	if elapsed := time.Since(interruptStartedAt); elapsed > 500*time.Millisecond {
		t.Fatalf("interrupt acknowledgement took %s while active turn was open", elapsed)
	}

	select {
	case interrupt := <-interruptReceived:
		if interrupt.Type != internalVoiceTypeInterrupt ||
			interrupt.TraceID != "trace_interrupt_1" {
			t.Fatalf("unexpected interrupt envelope: %+v", interrupt)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for interrupt at Python side")
	}
	select {
	case err := <-turnDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for cancelled active turn")
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 2 {
		t.Fatalf("forwarded messages = %d, want playback_start + playback_cancel: %+v", len(forwarded), forwarded)
	}
	cancel := forwarded[1].message
	if !cancel.IsJSON || cancel.Type != "playback_cancel" ||
		stringValue(cancel.JSON["trace_id"]) != "trace_event_1" ||
		stringValue(cancel.JSON["round_id"]) != "round_1" ||
		stringValue(cancel.JSON["playback_id"]) != "round_1:playback" ||
		stringValue(cancel.JSON["reason"]) != "wake_interrupt" {
		t.Fatalf("unexpected cancellation downlink: %+v", cancel)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestInternalVoiceTerminalDownlinksPreservePrePlaybackIdentity(t *testing.T) {
	cancelEnvelope := internalVoiceEnvelope{
		TraceID:    "trace_cancel_before_playback",
		RoundID:    "round_cancel_before_playback",
		PlaybackID: "round_cancel_before_playback:playback",
		Payload:    json.RawMessage(`{"reason":"wake_interrupt","expected":true}`),
	}
	cancel, err := internalVoiceCancellationDownlink(cancelEnvelope)
	if err != nil {
		t.Fatal(err)
	}
	if cancel.Type != "playback_cancel" ||
		stringValue(cancel.JSON["trace_id"]) != cancelEnvelope.TraceID ||
		stringValue(cancel.JSON["round_id"]) != cancelEnvelope.RoundID ||
		stringValue(cancel.JSON["playback_id"]) != cancelEnvelope.PlaybackID ||
		stringValue(cancel.JSON["reason"]) != "wake_interrupt" {
		t.Fatalf("unexpected cancellation terminal: %+v", cancel)
	}

	errorEnvelope := internalVoiceEnvelope{
		TraceID:    "trace_error_before_playback",
		RoundID:    "round_error_before_playback",
		PlaybackID: "round_error_before_playback:playback",
		Payload: json.RawMessage(
			`{"origin":"python_gateway","stage":"tts","code":"TTS_EMPTY_AUDIO","message":"empty","retryable":false,"fatal":false}`,
		),
	}
	errorCancel, err := internalVoiceErrorDownlink(errorEnvelope)
	if err != nil {
		t.Fatal(err)
	}
	if errorCancel.Type != "playback_cancel" ||
		stringValue(errorCancel.JSON["trace_id"]) != errorEnvelope.TraceID ||
		stringValue(errorCancel.JSON["round_id"]) != errorEnvelope.RoundID ||
		stringValue(errorCancel.JSON["playback_id"]) != errorEnvelope.PlaybackID ||
		stringValue(errorCancel.JSON["reason"]) != "response_error:TTS_EMPTY_AUDIO" {
		t.Fatalf("unexpected error terminal: %+v", errorCancel)
	}
}

func TestInternalVoiceClientHeartbeatKeepsIdleSessionAlive(t *testing.T) {
	heartbeatReceived := make(chan string, 1)
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		if err := websocket.JSON.Send(
			conn,
			mustInternalVoiceEnvelopeForTest(
				t,
				internalVoiceTypeSessionOpened,
				opened.SessionID,
			),
		); err != nil {
			t.Errorf("send opened: %v", err)
			return
		}
		for {
			var envelope internalVoiceEnvelope
			if err := websocket.JSON.Receive(conn, &envelope); err != nil {
				return
			}
			switch envelope.Type {
			case internalVoiceTypeHeartbeatPing:
				var payload internalVoiceHeartbeatPayload
				if err := envelope.PayloadInto(&payload); err != nil {
					t.Errorf("decode heartbeat: %v", err)
					return
				}
				select {
				case heartbeatReceived <- payload.Nonce:
				default:
				}
				pong, err := newInternalVoiceEnvelope(
					internalVoiceTypeHeartbeatPong,
					envelope.SessionID,
					internalVoiceHeartbeatPayload{Nonce: payload.Nonce},
				)
				if err != nil {
					t.Errorf("build heartbeat pong: %v", err)
					return
				}
				if err := websocket.JSON.Send(conn, pong); err != nil {
					t.Errorf("send heartbeat pong: %v", err)
					return
				}
			case internalVoiceTypeSessionClose:
				if err := websocket.JSON.Send(
					conn,
					mustInternalVoiceEnvelopeForTest(
						t,
						internalVoiceTypeSessionClosed,
						envelope.SessionID,
					),
				); err != nil {
					t.Errorf("send closed: %v", err)
				}
				return
			}
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:             true,
		WSURL:               defaultInternalVoiceWSURL,
		TimeoutMS:           1000,
		MaxMessageKB:        64,
		HeartbeatIntervalMS: 10,
		HeartbeatTimeoutMS:  100,
		HeartbeatMissLimit:  2,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)
	session, err := client.OpenSession(
		context.Background(),
		InternalVoiceSessionOpenRequest{SessionID: "rtc_heartbeat_1"},
	)
	if err != nil {
		t.Fatal(err)
	}

	select {
	case nonce := <-heartbeatReceived:
		if nonce == "" {
			t.Fatal("heartbeat nonce is empty")
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for automatic heartbeat")
	}
	deadline := time.Now().Add(time.Second)
	for client.Status()["heartbeat_successes"] != int64(1) &&
		time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if err := session.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	status := client.Status()
	if status["heartbeat_attempts"].(int64) < 1 ||
		status["heartbeat_successes"].(int64) < 1 ||
		status["heartbeat_failures"] != int64(0) ||
		status["heartbeat_dead"] != int64(0) {
		t.Fatalf("unexpected heartbeat status: %+v", status)
	}
}

func TestInternalVoiceClientHeartbeatMarksUnresponsiveSessionDead(t *testing.T) {
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			return
		}
		if err := websocket.JSON.Send(
			conn,
			mustInternalVoiceEnvelopeForTest(
				t,
				internalVoiceTypeSessionOpened,
				opened.SessionID,
			),
		); err != nil {
			return
		}
		var heartbeat internalVoiceEnvelope
		_ = websocket.JSON.Receive(conn, &heartbeat)
		<-time.After(200 * time.Millisecond)
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:             true,
		WSURL:               defaultInternalVoiceWSURL,
		TimeoutMS:           1000,
		MaxMessageKB:        64,
		HeartbeatIntervalMS: 10,
		HeartbeatTimeoutMS:  20,
		HeartbeatMissLimit:  1,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)
	session, err := client.OpenSession(
		context.Background(),
		InternalVoiceSessionOpenRequest{SessionID: "rtc_heartbeat_dead_1"},
	)
	if err != nil {
		t.Fatal(err)
	}

	deadline := time.Now().Add(time.Second)
	for client.Status()["heartbeat_dead"] != int64(1) && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	if client.Status()["heartbeat_dead"] != int64(1) {
		t.Fatalf("heartbeat did not mark session dead: %+v", client.Status())
	}
	if err := session.currentReaderError(); err == nil ||
		!strings.Contains(err.Error(), "heartbeat failed") {
		t.Fatalf("reader error = %v, want heartbeat failure", err)
	}
	_ = session.Close(context.Background())
}

func TestInternalVoiceClientReturnsProtocolError(t *testing.T) {
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		var opened internalVoiceEnvelope
		if err := websocket.JSON.Receive(conn, &opened); err != nil {
			t.Errorf("receive open: %v", err)
			return
		}
		payload := map[string]string{
			"code":    "INVALID_ENVELOPE",
			"message": "bad session",
		}
		envelope, err := newInternalVoiceEnvelope(internalVoiceTypeProtocolError, opened.SessionID, payload)
		if err != nil {
			t.Errorf("build protocol error: %v", err)
			return
		}
		if err := websocket.JSON.Send(conn, envelope); err != nil {
			t.Errorf("send protocol error: %v", err)
		}
	})

	client, err := NewInternalVoiceClient(InternalVoiceConfig{
		Enabled:      true,
		WSURL:        defaultInternalVoiceWSURL,
		TimeoutMS:    1000,
		MaxMessageKB: 64,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	client.dialer = internalVoiceDialerForTest(t, handler)

	_, err = client.OpenSession(context.Background(), InternalVoiceSessionOpenRequest{SessionID: "rtc_1"})
	if err == nil || !strings.Contains(err.Error(), "INVALID_ENVELOPE") {
		t.Fatalf("expected protocol error, got %v", err)
	}
}

func TestInternalVoiceClientRejectsInvalidURL(t *testing.T) {
	_, err := NewInternalVoiceClient(InternalVoiceConfig{WSURL: "http://127.0.0.1/internal/voice/ws"}, nil)
	if err == nil {
		t.Fatal("expected invalid URL error")
	}
}

func mustInternalVoiceEnvelopeForTest(t *testing.T, eventType string, sessionID string) internalVoiceEnvelope {
	t.Helper()
	envelope, err := newInternalVoiceEnvelope(eventType, sessionID, map[string]any{"ok": true})
	if err != nil {
		t.Fatal(err)
	}
	return envelope
}

func mustInternalVoiceStatusForTest(
	t *testing.T,
	request internalVoiceEnvelope,
) internalVoiceEnvelope {
	t.Helper()
	envelope := mustInternalVoiceEnvelopeForTest(
		t,
		internalVoiceTypeOrchestratorStatus,
		request.SessionID,
	)
	envelope.TraceID = request.TraceID
	envelope.UtteranceID = request.UtteranceID
	envelope.RoundID = request.RoundID
	envelope.PlaybackID = request.PlaybackID
	return envelope
}

func mustInternalVoiceResponseErrorForTest(
	t *testing.T,
	request internalVoiceEnvelope,
	code string,
	message string,
) internalVoiceEnvelope {
	t.Helper()
	payload, err := marshalInternalVoicePayload(internalVoiceResponseErrorPayload{
		Origin:  "python_gateway",
		Stage:   "tts",
		Code:    code,
		Message: message,
	})
	if err != nil {
		t.Fatal(err)
	}
	return internalVoiceEnvelope{
		Version:     internalVoiceProtocolVersion,
		Type:        internalVoiceTypeResponseError,
		SessionID:   request.SessionID,
		TraceID:     request.TraceID,
		UtteranceID: request.UtteranceID,
		RoundID:     request.RoundID,
		PlaybackID:  request.PlaybackID,
		TimestampMS: internalVoiceNowMillis(),
		Payload:     payload,
	}
}

func internalVoiceDialerForTest(t *testing.T, handler websocket.Handler) func(context.Context, string) (*websocket.Conn, error) {
	t.Helper()
	return func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: handler}, "/internal/voice/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}
}

func TestInternalVoiceEnvelopeJSONForTest(t *testing.T) {
	envelope := mustInternalVoiceEnvelopeForTest(t, internalVoiceTypeSessionOpened, "rtc_1")
	payload, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(payload), `"type":"session.opened"`) {
		t.Fatalf("unexpected envelope JSON: %s", payload)
	}
}

func TestInternalVoiceConfigTimeoutForTest(t *testing.T) {
	cfg, err := normalizeInternalVoiceConfig(InternalVoiceConfig{
		WSURL:     defaultInternalVoiceWSURL,
		TimeoutMS: 1200,
	})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Timeout != 1200*time.Millisecond {
		t.Fatalf("timeout = %s, want 1200ms", cfg.Timeout)
	}
}
