package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"golang.org/x/net/websocket"
)

func TestBuildPythonGatewayAudioFrame(t *testing.T) {
	request := testASRAudioRequest()
	frame, err := buildPythonGatewayAudioFrame(request, "fallback_bot")
	if err != nil {
		t.Fatal(err)
	}

	header, payload := decodePythonGatewayAudioFrameForTest(t, frame)
	if header.Type != "audio_frame" ||
		header.Version != pythonGatewayAudioFrameVersion ||
		header.Encoding != "opus" ||
		header.Direction != "client_input" ||
		header.BotID != "xiaowen" ||
		header.TraceID != "trace_1" ||
		header.UtteranceID != "utt_1" ||
		header.SampleRate != 16000 ||
		header.Channels != 1 ||
		header.OpusFrameMS != 20 ||
		header.PacketCount != 1 {
		t.Fatalf("unexpected frame header: %+v", header)
	}
	if !bytes.Equal(payload, request.AudioBytes) {
		t.Fatalf("payload = %v, want %v", payload, request.AudioBytes)
	}
}

func TestBuildPythonGatewayTurnCandidateFrame(t *testing.T) {
	request := testASRAudioRequest()
	request.Provisional = true
	request.Shadow = true
	request.CandidateSeq = 3
	request.SpeechEpoch = 2
	request.AudioWatermark = 32_000
	request.SilenceMS = 300
	request.ContextSessionID = "python-main-session"
	frame, err := buildPythonGatewayTurnCandidateFrame(request, "fallback_bot")
	if err != nil {
		t.Fatal(err)
	}
	header, payload := decodePythonGatewayAudioFrameForTest(t, frame)
	if header.EventType != "turn_candidate" || !header.Shadow || header.CandidateSeq != 3 ||
		header.SpeechEpoch != 2 || header.AudioWatermark != 32_000 || header.SilenceMS != 300 ||
		header.ContextSessionID != "python-main-session" {
		t.Fatalf("unexpected candidate frame header: %+v", header)
	}
	if !bytes.Equal(payload, request.AudioBytes) {
		t.Fatalf("candidate payload = %v, want %v", payload, request.AudioBytes)
	}
}

func TestBuildPythonGatewayBargeInFrame(t *testing.T) {
	request := testASRAudioRequest()
	request.Provisional = true
	request.CandidateKind = "barge_in"
	request.RoundID = "round-1"
	request.PlaybackID = "playback-1"
	request.CandidateSeq = 2
	request.SpeechEpoch = 1
	request.AudioWatermark = 19_200
	frame, err := buildPythonGatewayBargeInFrame(request, "fallback_bot")
	if err != nil {
		t.Fatal(err)
	}
	header, payload := decodePythonGatewayAudioFrameForTest(t, frame)
	if header.EventType != "barge_in_probe" || header.RoundID != request.RoundID ||
		header.PlaybackID != request.PlaybackID || header.CandidateSeq != request.CandidateSeq ||
		header.SpeechEpoch != request.SpeechEpoch || header.AudioWatermark != request.AudioWatermark {
		t.Fatalf("unexpected barge-in frame header: %+v", header)
	}
	if !bytes.Equal(payload, request.AudioBytes) {
		t.Fatalf("barge-in payload = %v, want %v", payload, request.AudioBytes)
	}
}

func TestTurnCandidateResultFromPythonMessage(t *testing.T) {
	result, err := turnCandidateResultFromPythonMessage(map[string]any{
		"trace_id":        "trace-1",
		"utterance_id":    "utt-1",
		"candidate_seq":   float64(2),
		"speech_epoch":    float64(1),
		"audio_watermark": float64(24_000),
		"status":          "observed",
		"asr_text":        "好的，就这样吧",
		"asr_time_ms":     123.4,
		"policy_preview":  "early_commit",
		"shadow":          false,
	})
	if err != nil {
		t.Fatalf("parse turn candidate result: %v", err)
	}
	if result.UtteranceID != "utt-1" || result.CandidateSeq != 2 || result.SpeechEpoch != 1 ||
		result.AudioWatermark != 24_000 || result.ASRText != "好的，就这样吧" || result.Shadow {
		t.Fatalf("unexpected result: %+v", result)
	}
}

func TestLooksLikePythonGatewayJSONPayload(t *testing.T) {
	tests := []struct {
		name    string
		payload []byte
		want    bool
	}{
		{name: "json_object", payload: []byte(`{"type":"done"}`), want: true},
		{name: "json_object_with_space", payload: []byte("\n\t {\"type\":\"done\"}"), want: true},
		{name: "audio_frame", payload: []byte("VAF1-test-audio"), want: false},
		{name: "empty", payload: nil, want: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := looksLikePythonGatewayJSONPayload(tt.payload); got != tt.want {
				t.Fatalf("looksLikePythonGatewayJSONPayload() = %t, want %t", got, tt.want)
			}
		})
	}
}

func TestIsRetryablePythonGatewayStaleWrite(t *testing.T) {
	if !isRetryablePythonGatewayStaleWrite(pythonGatewayAudioSendError{err: syscall.EPIPE}) {
		t.Fatal("expected broken-pipe audio send to be retryable")
	}
	if isRetryablePythonGatewayStaleWrite(fmt.Errorf("receive Python Gateway message: %w", syscall.ECONNRESET)) {
		t.Fatal("receive failure must not retry the whole audio request")
	}
	if isRetryablePythonGatewayStaleWrite(pythonGatewayAudioSendError{err: context.DeadlineExceeded}) {
		t.Fatal("ambiguous audio send timeout must not be retried")
	}
}

func TestPythonGatewayBridgeProcessorSendsRegisterAndAudioFrame(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	frameCh := make(chan []byte, 1)
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			frameCh <- append([]byte(nil), frame...)
			_ = websocket.JSON.Send(conn, map[string]any{"type": "status", "message": "识别中..."})
			_ = websocket.Message.Send(conn, []byte{1, 2, 3})
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}
	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}

	register := <-registerCh
	if register["type"] != "register" ||
		register["robot_id"] != "robot_1" ||
		register["robot_secret"] != "secret_1" ||
		register["client_type"] != "go_test" {
		t.Fatalf("unexpected register payload: %+v", register)
	}

	header, payload := decodePythonGatewayAudioFrameForTest(t, <-frameCh)
	if header.BotID != "xiaowen" || header.TraceID != "trace_1" || header.UtteranceID != "utt_1" {
		t.Fatalf("unexpected audio frame header: %+v", header)
	}
	if !bytes.Equal(payload, testASRAudioRequest().AudioBytes) {
		t.Fatalf("unexpected audio payload: %v", payload)
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 3 {
		t.Fatalf("forwarded messages = %+v, want 3", forwarded)
	}
	if forwarded[0].sessionID != "session_1" ||
		!forwarded[0].message.IsJSON ||
		forwarded[0].message.Type != "status" ||
		forwarded[1].message.IsJSON ||
		!bytes.Equal(forwarded[1].message.Binary, []byte{1, 2, 3}) ||
		!forwarded[2].message.IsJSON ||
		forwarded[2].message.Type != "done" {
		t.Fatalf("unexpected forwarded messages: %+v", forwarded)
	}
}

func TestPythonGatewayBridgeProcessorWrapsDownlinkForwardError(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.downlink = failingPythonGatewayDownlinkForwarder{err: errors.New("session send failed")}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "status", "message": "识别中..."})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	err = processor.ProcessASRAudioRequest(testASRAudioRequest())
	if err == nil {
		t.Fatal("expected downlink forward error")
	}
	for _, want := range []string{
		"forward Python Gateway downlink failed",
		"session=session_1",
		"type=status",
		"json=true",
		"binary=false",
		"payload_bytes=",
		"elapsed_ms=",
		"session send failed",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Fatalf("error missing %q in: %v", want, err)
		}
	}
}

func TestPythonGatewayBridgeProcessorForwardsClientEvent(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	eventCh := make(chan map[string]any, 1)
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var event map[string]any
			if err := websocket.JSON.Receive(conn, &event); err != nil {
				t.Errorf("receive event: %v", err)
				return
			}
			eventCh <- event
			_ = websocket.JSON.Send(conn, map[string]any{"type": "playback_start", "round_id": "round_1"})
			_ = websocket.Message.Send(conn, []byte{4, 5, 6})
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	err = processor.ProcessClientEvent("rust_session", clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "wake_idle-1",
		TraceID: "trace_wake_1",
		Source:  "hardware_wake",
		BotID:   "xiaowen",
	})
	if err != nil {
		t.Fatal(err)
	}

	register := <-registerCh
	if register["type"] != "register" ||
		register["robot_id"] != "robot_1" ||
		register["robot_secret"] != "secret_1" ||
		register["client_type"] != "go_test" {
		t.Fatalf("unexpected register payload: %+v", register)
	}

	event := <-eventCh
	if event["type"] != "client_event" ||
		event["event"] != "wake_idle" ||
		event["event_id"] != "wake_idle-1" ||
		event["trace_id"] != "trace_wake_1" ||
		event["source"] != "hardware_wake" ||
		event["bot_id"] != "xiaowen" {
		t.Fatalf("unexpected client_event payload: %+v", event)
	}

	forwarded := downlink.Messages()
	if len(forwarded) != 3 {
		t.Fatalf("forwarded messages = %+v, want 3", forwarded)
	}
	if forwarded[0].sessionID != "rust_session" ||
		!forwarded[0].message.IsJSON ||
		forwarded[0].message.Type != "playback_start" ||
		forwarded[1].message.IsJSON ||
		!bytes.Equal(forwarded[1].message.Binary, []byte{4, 5, 6}) ||
		!forwarded[2].message.IsJSON ||
		forwarded[2].message.Type != "done" {
		t.Fatalf("unexpected forwarded messages: %+v", forwarded)
	}
}

func TestPythonGatewayBridgeProcessorForwardsClientControl(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	controlCh := make(chan map[string]any, 2)
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			for i := 0; i < 2; i++ {
				var control map[string]any
				if err := websocket.JSON.Receive(conn, &control); err != nil {
					t.Errorf("receive control %d: %v", i, err)
					return
				}
				controlCh <- control
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessClientControl("rust_session", clientMessage{
		Type:    "interrupt",
		TraceID: "trace_interrupt_1",
		Reason:  "response_timeout",
		Source:  "rust_client",
	}); err != nil {
		t.Fatal(err)
	}
	if err := processor.ProcessClientControl("rust_session", clientMessage{
		Type:        "audio_cancel",
		UtteranceID: "utt_1",
		TraceID:     "trace_cancel_1",
		Reason:      "wake_interrupt",
		Source:      "hardware_wake",
		BotID:       "xiaowen",
	}); err != nil {
		t.Fatal(err)
	}

	register := <-registerCh
	if register["type"] != "register" || register["robot_id"] != "robot_1" {
		t.Fatalf("unexpected register payload: %+v", register)
	}
	first := <-controlCh
	if first["type"] != "interrupt" ||
		first["trace_id"] != "trace_interrupt_1" ||
		first["reason"] != "response_timeout" ||
		first["source"] != "rust_client" {
		t.Fatalf("unexpected interrupt payload: %+v", first)
	}
	second := <-controlCh
	if second["type"] != "audio_cancel" ||
		second["utterance_id"] != "utt_1" ||
		second["trace_id"] != "trace_cancel_1" ||
		second["reason"] != "wake_interrupt" ||
		second["source"] != "hardware_wake" ||
		second["bot_id"] != "xiaowen" {
		t.Fatalf("unexpected audio_cancel payload: %+v", second)
	}
}

func TestPythonGatewayBridgeProcessorCloseClientSessionDropsReusedConnection(t *testing.T) {
	var mu sync.Mutex
	dialCount := 0
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		mu.Lock()
		dialCount++
		mu.Unlock()
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var control map[string]any
			_ = websocket.JSON.Receive(conn, &control)
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	for i := 0; i < 2; i++ {
		if err := processor.ProcessClientControl("session_1", clientMessage{
			Type:   "interrupt",
			Reason: "disconnect_cleanup",
		}); err != nil {
			t.Fatal(err)
		}
		processor.CloseClientSession("session_1")
	}

	mu.Lock()
	defer mu.Unlock()
	if dialCount != 2 {
		t.Fatalf("dial count = %d, want 2 after session close", dialCount)
	}
}

func TestPythonGatewayBridgeProcessorWarmSessionReusesRegisteredConnection(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	dials := 0
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		dials++
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})
			for {
				var payload any
				if err := websocket.JSON.Receive(conn, &payload); err != nil {
					return
				}
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.WarmSession("session_1"); err != nil {
		t.Fatal(err)
	}
	if err := processor.WarmSession("session_1"); err != nil {
		t.Fatal(err)
	}
	if dials != 1 {
		t.Fatalf("dials=%d, want one persistent candidate connection", dials)
	}
	processor.CloseClientSession("session_1")
}

func TestPythonGatewayBridgeProcessorForwardsClientText(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	textCh := make(chan map[string]any, 1)
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var text map[string]any
			if err := websocket.JSON.Receive(conn, &text); err != nil {
				t.Errorf("receive text: %v", err)
				return
			}
			textCh <- text
			_ = websocket.JSON.Send(conn, map[string]any{"type": "playback_start", "round_id": "round_1"})
			_ = websocket.Message.Send(conn, []byte{7, 8, 9})
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	err = processor.ProcessClientText("rust_session", clientMessage{
		Type:        "text",
		Content:     "请根据以下环境主动回应：有人靠近",
		TraceID:     "trace_text_1",
		UtteranceID: "utt_text_1",
		Source:      "environment_http",
		BotID:       "xiaowen",
	})
	if err != nil {
		t.Fatal(err)
	}

	register := <-registerCh
	if register["type"] != "register" ||
		register["robot_id"] != "robot_1" ||
		register["robot_secret"] != "secret_1" ||
		register["client_type"] != "go_test" {
		t.Fatalf("unexpected register payload: %+v", register)
	}

	text := <-textCh
	if text["type"] != "text" ||
		text["content"] != "请根据以下环境主动回应：有人靠近" ||
		text["trace_id"] != "trace_text_1" ||
		text["utterance_id"] != "utt_text_1" ||
		text["source"] != "environment_http" ||
		text["bot_id"] != "xiaowen" {
		t.Fatalf("unexpected text payload: %+v", text)
	}

	forwarded := downlink.Messages()
	if len(forwarded) != 3 {
		t.Fatalf("forwarded messages = %+v, want 3", forwarded)
	}
	if forwarded[0].sessionID != "rust_session" ||
		!forwarded[0].message.IsJSON ||
		forwarded[0].message.Type != "playback_start" ||
		forwarded[1].message.IsJSON ||
		!bytes.Equal(forwarded[1].message.Binary, []byte{7, 8, 9}) ||
		!forwarded[2].message.IsJSON ||
		forwarded[2].message.Type != "done" {
		t.Fatalf("unexpected forwarded messages: %+v", forwarded)
	}
}

func TestPythonGatewayBridgeProcessorForwardsPlaybackReport(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	reportCh := make(chan map[string]any, 1)
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var report map[string]any
			if err := websocket.JSON.Receive(conn, &report); err != nil {
				t.Errorf("receive playback report: %v", err)
				return
			}
			reportCh <- report
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	firstAudioToPlayback := 12.5
	playbackDuration := 345.25
	pushedChunks := uint64(9)
	pushedSamples := uint64(9600)
	underruns := uint64(1)
	zeroFilledSamples := uint64(480)
	maxBufferedSamples := uint64(24000)
	err = processor.ProcessPlaybackReport("rust_session", clientMessage{
		Type:                        "playback_complete",
		TraceID:                     "trace_playback_1",
		RoundID:                     "py_session:4",
		PlaybackID:                  "py_session:4:playback",
		FirstAudioToPlaybackStartMS: &firstAudioToPlayback,
		PlaybackStartToCompleteMS:   &playbackDuration,
		PushedChunks:                &pushedChunks,
		PushedSamples:               &pushedSamples,
		UnderrunCallbacks:           &underruns,
		ZeroFilledSamples:           &zeroFilledSamples,
		MaxBufferedSamples:          &maxBufferedSamples,
	})
	if err != nil {
		t.Fatal(err)
	}

	register := <-registerCh
	if register["type"] != "register" || register["robot_id"] != "robot_1" {
		t.Fatalf("unexpected register payload: %+v", register)
	}

	report := <-reportCh
	if report["type"] != "playback_complete" ||
		report["trace_id"] != "trace_playback_1" ||
		report["round_id"] != "py_session:4" ||
		report["playback_id"] != "py_session:4:playback" ||
		report["first_audio_to_playback_start_ms"] != firstAudioToPlayback ||
		report["playback_start_to_complete_ms"] != playbackDuration ||
		report["pushed_chunks"] != float64(pushedChunks) ||
		report["pushed_samples"] != float64(pushedSamples) ||
		report["underrun_callbacks"] != float64(underruns) ||
		report["zero_filled_samples"] != float64(zeroFilledSamples) ||
		report["max_buffered_samples"] != float64(maxBufferedSamples) {
		t.Fatalf("unexpected playback report payload: %+v", report)
	}
}

func TestPythonGatewayBridgeProcessorReusesSessionConnection(t *testing.T) {
	var mu sync.Mutex
	dialCount := 0
	registerCount := 0
	messageTypes := make([]string, 0, 3)
	var logs bytes.Buffer

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.downlink = &recordingPythonGatewayDownlinkForwarder{}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		mu.Lock()
		dialCount++
		mu.Unlock()
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			for {
				var payload []byte
				if err := websocket.Message.Receive(conn, &payload); err != nil {
					return
				}
				var decoded map[string]any
				if err := json.Unmarshal(payload, &decoded); err == nil && decoded["type"] != nil {
					switch decoded["type"] {
					case "register":
						mu.Lock()
						registerCount++
						mu.Unlock()
						_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})
					case "client_event":
						mu.Lock()
						messageTypes = append(messageTypes, "client_event")
						mu.Unlock()
						_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
					case "playback_complete":
						mu.Lock()
						messageTypes = append(messageTypes, "playback_complete")
						mu.Unlock()
					default:
						mu.Lock()
						messageTypes = append(messageTypes, stringValue(decoded["type"]))
						mu.Unlock()
						_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
					}
					continue
				}
				mu.Lock()
				messageTypes = append(messageTypes, "audio_frame")
				mu.Unlock()
				_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}
	if err := processor.ProcessClientEvent("session_1", clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "wake_idle-1",
		Source:  "http_wakeup",
	}); err != nil {
		t.Fatal(err)
	}
	if err := processor.ProcessPlaybackReport("session_1", clientMessage{
		Type:       "playback_complete",
		RoundID:    "session_1:1",
		PlaybackID: "session_1:1:playback",
	}); err != nil {
		t.Fatal(err)
	}
	waitForBridgeMessageTypes(t, &mu, &messageTypes, 3)

	mu.Lock()
	defer mu.Unlock()
	if dialCount != 1 {
		t.Fatalf("dial count = %d, want 1", dialCount)
	}
	if registerCount != 1 {
		t.Fatalf("register count = %d, want 1", registerCount)
	}
	if len(messageTypes) != 3 ||
		messageTypes[0] != "audio_frame" ||
		messageTypes[1] != "client_event" ||
		messageTypes[2] != "playback_complete" {
		t.Fatalf("message types = %v, want [audio_frame client_event playback_complete]", messageTypes)
	}
	logText := logs.String()
	for _, want := range []string{
		"python_gateway_bridge_done",
		"python_gateway_client_event_bridge_done",
		"python_gateway_playback_report_forwarded",
		"connection_mode=session",
		"connection_reused=false",
		"connection_reused=true",
		"connection_open_ms=",
		"send_ms=",
		"receive_ms=",
		"action_ms=",
		"first_text_ms=",
		"first_binary_ms=",
		"last_message_ms=",
	} {
		if !strings.Contains(logText, want) {
			t.Fatalf("bridge log missing %q in:\n%s", want, logText)
		}
	}

	health := processor.BridgeHealthStatus()
	if health["connection_mode"] != pythonGatewayConnectionModeSession ||
		health["active_session_bridges"] != 1 ||
		health["active_connections"] != 1 ||
		health["session_bridges_created"] != int64(1) ||
		health["connection_open_attempts"] != int64(1) ||
		health["connection_open_successes"] != int64(1) ||
		health["connection_open_failures"] != int64(0) ||
		health["connection_reused"] != int64(2) ||
		health["send_errors"] != int64(0) ||
		health["receive_errors"] != int64(0) ||
		health["gateway_errors"] != int64(0) ||
		health["downlink_forward_errors"] != int64(0) ||
		health["fast_path_send_attempts"] != int64(0) {
		t.Fatalf("unexpected bridge health: %+v", health)
	}
}

func TestPythonGatewayBridgeProcessorRecyclesIdleSessionConnection(t *testing.T) {
	var mu sync.Mutex
	dialCount := 0
	registerCount := 0
	var logs bytes.Buffer

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:              defaultPythonGatewayWSURL,
		RobotID:            "robot_1",
		RobotSecret:        "secret_1",
		ClientType:         "go_test",
		Timeout:            time.Second,
		MaxMessageBytes:    1024 * 1024,
		ConnectionMode:     pythonGatewayConnectionModeSession,
		SessionIdleRecycle: 20 * time.Millisecond,
	}, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		mu.Lock()
		dialCount++
		mu.Unlock()
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			for {
				var payload []byte
				if err := websocket.Message.Receive(conn, &payload); err != nil {
					return
				}
				var decoded map[string]any
				if err := json.Unmarshal(payload, &decoded); err == nil && decoded["type"] == "register" {
					mu.Lock()
					registerCount++
					mu.Unlock()
					_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})
					continue
				}
				_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessClientEvent("session_1", clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "wake_idle-1",
	}); err != nil {
		t.Fatal(err)
	}
	time.Sleep(40 * time.Millisecond)
	if err := processor.ProcessClientEvent("session_1", clientMessage{
		Type:    "client_event",
		Event:   "wake_idle",
		EventID: "wake_idle-2",
	}); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if dialCount != 2 {
		t.Fatalf("dial count = %d, want 2", dialCount)
	}
	if registerCount != 2 {
		t.Fatalf("register count = %d, want 2", registerCount)
	}
	health := processor.BridgeHealthStatus()
	if health["connection_open_attempts"] != int64(2) ||
		health["connection_open_successes"] != int64(2) ||
		health["idle_recycles"] != int64(1) ||
		health["connection_closed"] != int64(1) ||
		health["active_connections"] != 1 {
		t.Fatalf("unexpected bridge health after idle recycle: %+v", health)
	}
	if !strings.Contains(logs.String(), "python_gateway_session_bridge_idle_recycle") {
		t.Fatalf("bridge log missing idle recycle event in:\n%s", logs.String())
	}
}

func TestPythonGatewayBridgeProcessorRetriesStaleReusedConnectionOnce(t *testing.T) {
	var mu sync.Mutex
	dialCount := 0
	audioFrameCount := 0
	firstConnectionClosed := make(chan struct{})
	var logs bytes.Buffer

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		mu.Lock()
		dialCount++
		connectionNumber := dialCount
		mu.Unlock()

		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			mu.Lock()
			audioFrameCount++
			mu.Unlock()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})

			if connectionNumber == 1 {
				_ = conn.SetDeadline(time.Now())
				_ = conn.Close()
				close(firstConnectionClosed)
				return
			}
			defer conn.Close()
			for {
				if err := websocket.Message.Receive(conn, &frame); err != nil {
					return
				}
				mu.Lock()
				audioFrameCount++
				mu.Unlock()
				_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-firstConnectionClosed:
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for stale connection to close")
	}
	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatalf("retry stale connection: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	if dialCount != 2 {
		t.Fatalf("dial count = %d, want 2", dialCount)
	}
	if audioFrameCount != 2 {
		t.Fatalf("audio frame count = %d, want 2", audioFrameCount)
	}
	health := processor.BridgeHealthStatus()
	if health["stale_connection_retries"] != int64(1) ||
		health["send_errors"] != int64(1) ||
		health["connection_open_successes"] != int64(2) {
		t.Fatalf("unexpected bridge health after stale retry: %+v", health)
	}
	if !strings.Contains(logs.String(), "python_gateway_stale_connection_retry") {
		t.Fatalf("bridge log missing stale retry event in:\n%s", logs.String())
	}
}

func TestPythonGatewayBridgeProcessorFastPathClientEventDuringActiveSession(t *testing.T) {
	audioReceived := make(chan struct{}, 1)
	eventCh := make(chan map[string]any, 1)
	releaseDone := make(chan struct{})
	var logs bytes.Buffer

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         2 * time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.downlink = &recordingPythonGatewayDownlinkForwarder{}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			audioReceived <- struct{}{}

			var eventPayload []byte
			if err := websocket.Message.Receive(conn, &eventPayload); err != nil {
				t.Errorf("receive fast-path event: %v", err)
				return
			}
			var event map[string]any
			if err := json.Unmarshal(eventPayload, &event); err != nil {
				t.Errorf("decode fast-path event: %v", err)
				return
			}
			eventCh <- event

			select {
			case <-releaseDone:
			case <-time.After(2 * time.Second):
				t.Errorf("timeout waiting to release active bridge")
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "done",
				"round_id":    "audio_round",
				"playback_id": "audio_round:playback",
			})
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "done",
				"round_id":    "wake_round",
				"playback_id": "wake_round:playback",
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	asrErrCh := make(chan error, 1)
	go func() {
		asrErrCh <- processor.ProcessASRAudioRequest(testASRAudioRequest())
	}()
	select {
	case <-audioReceived:
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active audio bridge")
	}

	eventErrCh := make(chan error, 1)
	go func() {
		eventErrCh <- processor.ProcessClientEvent("session_1", clientMessage{
			Type:    "client_event",
			Event:   "wake_interrupt",
			EventID: "wake_interrupt-1",
			Source:  "hardware_wake",
			BotID:   "xiaowen",
		})
	}()
	select {
	case err := <-eventErrCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(200 * time.Millisecond):
		t.Fatal("client_event fast path blocked behind active audio bridge")
	}

	select {
	case event := <-eventCh:
		if event["type"] != "client_event" ||
			event["event"] != "wake_interrupt" ||
			event["event_id"] != "wake_interrupt-1" ||
			event["source"] != "hardware_wake" ||
			event["bot_id"] != "xiaowen" {
			t.Fatalf("unexpected fast-path event payload: %+v", event)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for Python Gateway fast-path event")
	}

	close(releaseDone)
	select {
	case err := <-asrErrCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active audio bridge to finish")
	}
	if !strings.Contains(logs.String(), "fast_path=true") {
		t.Fatalf("bridge log missing fast_path=true in:\n%s", logs.String())
	}
}

func TestPythonGatewayBridgeProcessorFastPathControlDuringActiveTextSession(t *testing.T) {
	textReceived := make(chan struct{}, 1)
	controlCh := make(chan map[string]any, 1)
	releaseDone := make(chan struct{})
	downlink := &recordingPythonGatewayDownlinkForwarder{}

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         2 * time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var textPayload map[string]any
			if err := websocket.JSON.Receive(conn, &textPayload); err != nil {
				t.Errorf("receive text: %v", err)
				return
			}
			textReceived <- struct{}{}

			var control map[string]any
			if err := websocket.JSON.Receive(conn, &control); err != nil {
				t.Errorf("receive fast-path control: %v", err)
				return
			}
			controlCh <- control
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "playback_cancel",
				"round_id":    "text_round",
				"playback_id": "text_round:playback",
				"reason":      "client_interrupt",
			})
			select {
			case <-releaseDone:
			case <-time.After(2 * time.Second):
				t.Errorf("timeout waiting to release active text bridge")
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "done",
				"round_id":    "text_round",
				"playback_id": "text_round:playback",
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	textErrCh := make(chan error, 1)
	go func() {
		textErrCh <- processor.ProcessClientText("session_1", clientMessage{
			Type:    "text",
			Content: "唱首爱你",
		})
	}()
	select {
	case <-textReceived:
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active text bridge")
	}

	sent, err := processor.ProcessClientControlDuringActive("session_1", clientMessage{
		Type:   "interrupt",
		Reason: "user_interrupt",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !sent {
		t.Fatal("active text control was not sent")
	}
	select {
	case control := <-controlCh:
		if control["type"] != "interrupt" || control["reason"] != "user_interrupt" {
			t.Fatalf("unexpected fast-path control payload: %+v", control)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for Python Gateway fast-path control")
	}

	deadline := time.Now().Add(time.Second)
	for len(downlink.Messages()) == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	forwarded := downlink.Messages()
	if len(forwarded) != 1 || forwarded[0].message.Type != "playback_cancel" {
		t.Fatalf("unexpected forwarded cancellation: %+v", forwarded)
	}
	close(releaseDone)
	select {
	case err := <-textErrCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active text bridge to finish")
	}
}

func TestPythonGatewayBridgeProcessorActiveControlDoesNotOpenConnection(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:          defaultPythonGatewayWSURL,
		ConnectionMode: pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	dialed := false
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		dialed = true
		return nil, errors.New("unexpected dial")
	}
	sent, err := processor.ProcessClientControlDuringActive("missing", clientMessage{Type: "interrupt"})
	if err != nil {
		t.Fatal(err)
	}
	if sent || dialed {
		t.Fatalf("inactive control sent=%t dialed=%t, want both false", sent, dialed)
	}
}

func TestPythonGatewayBridgeProcessorDrainsFastPathClientEventAfterActiveDone(t *testing.T) {
	audioReceived := make(chan struct{}, 1)
	eventCh := make(chan map[string]any, 1)
	var logs bytes.Buffer
	downlink := &recordingPythonGatewayDownlinkForwarder{}

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		BotID:           "fallback_bot",
		Timeout:         2 * time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(&logs, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			audioReceived <- struct{}{}

			var eventPayload []byte
			if err := websocket.Message.Receive(conn, &eventPayload); err != nil {
				t.Errorf("receive fast-path event: %v", err)
				return
			}
			var event map[string]any
			if err := json.Unmarshal(eventPayload, &event); err != nil {
				t.Errorf("decode fast-path event: %v", err)
				return
			}
			eventCh <- event

			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "done",
				"round_id":    "audio_round",
				"playback_id": "audio_round:playback",
			})
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "playback_start",
				"round_id":    "wake_round",
				"playback_id": "wake_round:playback",
			})
			_ = websocket.Message.Send(conn, []byte{8, 6, 7, 5})
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":        "done",
				"round_id":    "wake_round",
				"playback_id": "wake_round:playback",
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	asrErrCh := make(chan error, 1)
	go func() {
		asrErrCh <- processor.ProcessASRAudioRequest(testASRAudioRequest())
	}()
	select {
	case <-audioReceived:
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active audio bridge")
	}

	eventErrCh := make(chan error, 1)
	go func() {
		eventErrCh <- processor.ProcessClientEvent("session_1", clientMessage{
			Type:    "client_event",
			Event:   "wake_interrupt",
			EventID: "wake_interrupt-1",
			Source:  "hardware_wake",
			BotID:   "xiaowen",
		})
	}()
	select {
	case err := <-eventErrCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(200 * time.Millisecond):
		t.Fatal("client_event fast path blocked behind active audio bridge")
	}
	select {
	case event := <-eventCh:
		if event["type"] != "client_event" || event["event"] != "wake_interrupt" {
			t.Fatalf("unexpected fast-path event payload: %+v", event)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for Python Gateway fast-path event")
	}
	select {
	case err := <-asrErrCh:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("timeout waiting for active audio bridge to drain fast-path response")
	}

	forwarded := downlink.Messages()
	if len(forwarded) != 4 {
		t.Fatalf("forwarded messages = %+v, want 4", forwarded)
	}
	if !forwarded[0].message.IsJSON || forwarded[0].message.Type != "done" ||
		!forwarded[1].message.IsJSON || forwarded[1].message.Type != "playback_start" ||
		forwarded[2].message.IsJSON || !bytes.Equal(forwarded[2].message.Binary, []byte{8, 6, 7, 5}) ||
		!forwarded[3].message.IsJSON || forwarded[3].message.Type != "done" {
		t.Fatalf("unexpected forwarded messages: %+v", forwarded)
	}
	if !strings.Contains(logs.String(), "fast_path=true") {
		t.Fatalf("bridge log missing fast_path=true in:\n%s", logs.String())
	}
}

func TestPythonGatewayBridgeProcessorUsesResolvedSessionCredentials(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "global_robot",
		RobotSecret:     "global_secret",
		ClientType:      "global_client",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModeSession,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer processor.Close()
	processor.credentialResolver = staticPythonGatewayCredentialResolver{
		"session_1": {
			RobotID:     "session_robot",
			RobotSecret: "session_secret",
			ClientType:  "rust",
		},
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})
			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}
	register := <-registerCh
	if register["robot_id"] != "session_robot" ||
		register["robot_secret"] != "session_secret" ||
		register["client_type"] != "rust" {
		t.Fatalf("unexpected resolved register payload: %+v", register)
	}
}

func TestPythonGatewayBridgeProcessorPerTurnModeReopensConnection(t *testing.T) {
	var mu sync.Mutex
	dialCount := 0
	registerCount := 0

	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		RobotID:         "robot_1",
		RobotSecret:     "secret_1",
		ClientType:      "go_test",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
		ConnectionMode:  pythonGatewayConnectionModePerTurn,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		mu.Lock()
		dialCount++
		mu.Unlock()
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			mu.Lock()
			registerCount++
			mu.Unlock()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "registered", "session_id": "py_session"})
			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{"type": "done"})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}
	if err := processor.ProcessASRAudioRequest(testASRAudioRequest()); err != nil {
		t.Fatal(err)
	}

	mu.Lock()
	defer mu.Unlock()
	if dialCount != 2 {
		t.Fatalf("dial count = %d, want 2", dialCount)
	}
	if registerCount != 2 {
		t.Fatalf("register count = %d, want 2", registerCount)
	}
}

func TestPythonGatewayBridgeProcessorAuthenticatesRegister(t *testing.T) {
	registerCh := make(chan map[string]any, 1)
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		ClientType:      "go_auth",
		BotID:           "fallback_bot",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			registerCh <- register
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":         "registered",
				"session_id":   "py_session",
				"robot_id":     "robot_1",
				"bot_id":       "bot_from_db",
				"bot_name":     "Bot From DB",
				"client_type":  "rust",
				"is_new_robot": false,
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	secret := "secret_1"
	registration, err := processor.AuthenticateRegister(context.Background(), clientMessage{
		Type:        "register",
		RobotID:     "robot_1",
		RobotSecret: &secret,
		ClientType:  "rust",
	})
	if err != nil {
		t.Fatal(err)
	}
	register := <-registerCh
	if register["robot_id"] != "robot_1" ||
		register["robot_secret"] != "secret_1" ||
		register["client_type"] != "rust" {
		t.Fatalf("unexpected register payload: %+v", register)
	}
	if registration.RobotID != "robot_1" ||
		registration.BotID != "bot_from_db" ||
		registration.BotName != "Bot From DB" ||
		registration.ClientType != "rust" {
		t.Fatalf("unexpected registration: %+v", registration)
	}
}

func TestPythonGatewayBridgeProcessorAuthenticateRegisterReturnsGatewayError(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		ClientType:      "go_auth",
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var register map[string]any
			if err := websocket.JSON.Receive(conn, &register); err != nil {
				t.Errorf("receive register: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":    "error",
				"code":    "REGISTER_FAILED",
				"message": "robot_secret 校验失败",
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	secret := "wrong"
	_, err = processor.AuthenticateRegister(context.Background(), clientMessage{
		Type:        "register",
		RobotID:     "robot_1",
		RobotSecret: &secret,
		ClientType:  "rust",
	})
	if err == nil {
		t.Fatal("expected register auth error")
	}
	if !strings.Contains(err.Error(), "REGISTER_FAILED") ||
		!strings.Contains(err.Error(), "robot_secret 校验失败") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestPythonGatewayBridgeProcessorReturnsGatewayError(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		Timeout:         time.Second,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})
			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			_ = websocket.JSON.Send(conn, map[string]any{
				"type":    "error",
				"code":    "PROCESS_FAILED",
				"message": "asr failed",
			})
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}
	err = processor.ProcessASRAudioRequest(testASRAudioRequest())
	if err == nil {
		t.Fatal("expected error")
	}
	if !strings.Contains(err.Error(), "PROCESS_FAILED") || !strings.Contains(err.Error(), "asr failed") {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestPythonGatewayBridgeProcessorTimesOutWhenDoneMissing(t *testing.T) {
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		Timeout:         50 * time.Millisecond,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			time.Sleep(250 * time.Millisecond)
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	startedAt := time.Now()
	err = processor.ProcessASRAudioRequest(testASRAudioRequest())
	elapsed := time.Since(startedAt)
	if err == nil {
		t.Fatal("expected timeout waiting for Python Gateway done")
	}
	if !strings.Contains(err.Error(), "timeout") && !strings.Contains(err.Error(), "deadline") {
		t.Fatalf("unexpected error: %v", err)
	}
	if elapsed > 200*time.Millisecond {
		t.Fatalf("timeout returned after %s, want close to configured timeout", elapsed)
	}
}

func TestPythonGatewayBridgeProcessorTotalTimeoutBoundsChattyMissingDone(t *testing.T) {
	downlink := &recordingPythonGatewayDownlinkForwarder{}
	processor, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
		WSURL:           defaultPythonGatewayWSURL,
		Timeout:         500 * time.Millisecond,
		TotalTimeout:    100 * time.Millisecond,
		MaxMessageBytes: 1024 * 1024,
	}, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	processor.downlink = downlink
	processor.dialer = func(context.Context, string) (*websocket.Conn, error) {
		conn, closeConn := newInMemoryWebSocket(t, websocket.Server{Handler: websocket.Handler(func(conn *websocket.Conn) {
			defer conn.Close()
			_ = websocket.JSON.Send(conn, map[string]any{"type": "connected", "session_id": "py_session"})

			var frame []byte
			if err := websocket.Message.Receive(conn, &frame); err != nil {
				t.Errorf("receive frame: %v", err)
				return
			}
			for {
				if err := websocket.JSON.Send(conn, map[string]any{"type": "status", "message": "still working"}); err != nil {
					return
				}
				time.Sleep(10 * time.Millisecond)
			}
		})}, "/ws")
		t.Cleanup(closeConn)
		return conn, nil
	}

	startedAt := time.Now()
	err = processor.ProcessASRAudioRequest(testASRAudioRequest())
	elapsed := time.Since(startedAt)
	if err == nil {
		t.Fatal("expected total timeout waiting for Python Gateway done")
	}
	if !strings.Contains(err.Error(), "total timeout") {
		t.Fatalf("unexpected error: %v", err)
	}
	if elapsed > 300*time.Millisecond {
		t.Fatalf("total timeout returned after %s, want close to configured total timeout", elapsed)
	}
	if len(downlink.Messages()) == 0 {
		t.Fatal("expected chatty status messages to be forwarded before total timeout")
	}
}

func TestPythonGatewayBridgeProcessorRejectsInvalidURL(t *testing.T) {
	_, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{WSURL: "http://127.0.0.1/ws"}, nil)
	if err == nil {
		t.Fatal("expected invalid URL error")
	}
}

func testASRAudioRequest() ASRAudioRequest {
	return ASRAudioRequest{
		Type:                       asrAudioRequestType,
		SessionID:                  "session_1",
		BotID:                      "xiaowen",
		TraceID:                    "trace_1",
		UtteranceID:                "utt_1",
		AudioEncoding:              asrAudioRequestEncodingOpus,
		AudioTransport:             asrAudioRequestTransportWebRTC,
		PacketStreamFormat:         asrAudioRequestPacketStreamOpus,
		SampleRate:                 16000,
		Channels:                   1,
		OpusFrameMS:                20,
		PacketCount:                1,
		AudioBytes:                 []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c'},
		AudioByteCount:             13,
		RetainedPacketPayloadBytes: 3,
		ObservedPacketCount:        1,
		ObservedPayloadBytes:       3,
	}
}

func decodePythonGatewayAudioFrameForTest(t *testing.T, frame []byte) (pythonGatewayAudioFrameHeader, []byte) {
	t.Helper()
	if len(frame) < len(pythonGatewayAudioFrameMagic)+4 {
		t.Fatalf("frame too short: %d", len(frame))
	}
	if !bytes.Equal(frame[:len(pythonGatewayAudioFrameMagic)], []byte(pythonGatewayAudioFrameMagic)) {
		t.Fatalf("frame magic = %q", frame[:len(pythonGatewayAudioFrameMagic)])
	}
	headerLen := int(binary.BigEndian.Uint32(frame[len(pythonGatewayAudioFrameMagic) : len(pythonGatewayAudioFrameMagic)+4]))
	headerStart := len(pythonGatewayAudioFrameMagic) + 4
	headerEnd := headerStart + headerLen
	if headerEnd > len(frame) {
		t.Fatalf("header len %d exceeds frame len %d", headerLen, len(frame))
	}
	var header pythonGatewayAudioFrameHeader
	if err := json.Unmarshal(frame[headerStart:headerEnd], &header); err != nil {
		t.Fatal(err)
	}
	return header, frame[headerEnd:]
}

type recordingPythonGatewayDownlinkForwarder struct {
	mu       sync.Mutex
	messages []recordedPythonGatewayDownlink
}

type recordedPythonGatewayDownlink struct {
	sessionID string
	message   pythonGatewayBridgeMessage
}

type failingPythonGatewayDownlinkForwarder struct {
	err error
}

func (f failingPythonGatewayDownlinkForwarder) ForwardPythonGatewayDownlink(string, pythonGatewayBridgeMessage) error {
	return f.err
}

func (f *recordingPythonGatewayDownlinkForwarder) ForwardPythonGatewayDownlink(sessionID string, message pythonGatewayBridgeMessage) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(message.Binary) > 0 {
		message.Binary = append([]byte(nil), message.Binary...)
	}
	if len(message.Payload) > 0 {
		message.Payload = append([]byte(nil), message.Payload...)
	}
	f.messages = append(f.messages, recordedPythonGatewayDownlink{
		sessionID: sessionID,
		message:   message,
	})
	return nil
}

func (f *recordingPythonGatewayDownlinkForwarder) Messages() []recordedPythonGatewayDownlink {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := make([]recordedPythonGatewayDownlink, len(f.messages))
	copy(out, f.messages)
	return out
}

type staticPythonGatewayCredentialResolver map[string]pythonGatewaySessionCredentials

func (r staticPythonGatewayCredentialResolver) PythonGatewayCredentialsForSession(sessionID string) (pythonGatewaySessionCredentials, bool) {
	credentials, ok := r[sessionID]
	return credentials, ok
}

func waitForBridgeMessageTypes(t *testing.T, mu *sync.Mutex, messageTypes *[]string, want int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		got := len(*messageTypes)
		mu.Unlock()
		if got >= want {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	mu.Lock()
	got := append([]string(nil), (*messageTypes)...)
	mu.Unlock()
	t.Fatalf("timeout waiting for %d bridge message types, got %v", want, got)
}
