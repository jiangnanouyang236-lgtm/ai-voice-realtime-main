package main

import (
	"bytes"
	"io"
	"log"
	"strings"
	"testing"

	"golang.org/x/net/websocket"
)

func TestGatewaySessionRegistryForwardsPythonGatewayDownlink(t *testing.T) {
	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebSocket
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if connected.SessionID == "" {
		t.Fatalf("missing session id: %+v", connected)
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
			Type:   "status",
			IsJSON: true,
			JSON: map[string]any{
				"type":    "status",
				"message": "识别中...",
			},
		})
	}()
	var status map[string]any
	if err := websocket.JSON.Receive(conn, &status); err != nil {
		t.Fatal(err)
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	if status["type"] != "status" || status["message"] != "识别中..." {
		t.Fatalf("unexpected forwarded status: %+v", status)
	}

	binaryPayload := []byte("VAF1\x00\x00\x00\x02{}")
	go func() {
		errCh <- server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
			Binary: binaryPayload,
		})
	}()
	var gotBinary []byte
	if err := websocket.Message.Receive(conn, &gotBinary); err != nil {
		t.Fatal(err)
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(gotBinary, binaryPayload) {
		t.Fatalf("forwarded binary = %v, want %v", gotBinary, binaryPayload)
	}
}

func TestGatewaySessionRegistryBlocksRTPDownlinkUntilPlaybackStart(t *testing.T) {
	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebRTCRTP
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	session := server.sessions.Lookup(connected.SessionID)
	if session == nil {
		t.Fatalf("missing session %s", connected.SessionID)
	}

	session.cancelDownlinkRTP("test_interrupt")
	if err := server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
		Binary: []byte("bad rtp payload"),
	}); err != nil {
		t.Fatal(err)
	}
	if !session.downBlocked.Load() {
		t.Fatal("expected RTP downlink to remain blocked")
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
			Type:   "playback_start",
			IsJSON: true,
			JSON: map[string]any{
				"type":        "playback_start",
				"trace_id":    "trace_playback_1",
				"round_id":    "round_1",
				"playback_id": "round_1:playback",
			},
		})
	}()
	var playbackStart map[string]any
	if err := websocket.JSON.Receive(conn, &playbackStart); err != nil {
		t.Fatal(err)
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	if playbackStart["type"] != "playback_start" {
		t.Fatalf("unexpected playback_start message: %+v", playbackStart)
	}
	if sequence, ok := playbackStart["rtp_start_sequence"].(float64); !ok || sequence != 0 {
		t.Fatalf("playback_start RTP boundary = %#v, want 0", playbackStart["rtp_start_sequence"])
	}
	if session.downBlocked.Load() {
		t.Fatal("expected playback_start to unblock RTP downlink")
	}
	session.downMu.Lock()
	downTraceID := session.downTraceID
	downRoundID := session.downRoundID
	downPlayID := session.downPlayID
	session.downMu.Unlock()
	if downTraceID != "trace_playback_1" ||
		downRoundID != "round_1" ||
		downPlayID != "round_1:playback" {
		t.Fatalf(
			"unexpected downlink playback state: trace=%s round=%s playback=%s",
			downTraceID,
			downRoundID,
			downPlayID,
		)
	}

	err = server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
		Binary: []byte("bad rtp payload"),
	})
	if err != nil {
		t.Fatalf("RTP downlink enqueue should not block on payload validation: %v", err)
	}
}

func TestGatewaySessionQueuesRTPAudioAndDoneDownlink(t *testing.T) {
	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebRTCRTP
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	session := &gatewaySession{server: server}

	if !session.shouldQueuePythonGatewayDownlink(pythonGatewayBridgeMessage{Binary: []byte("VAF1-test-audio")}) {
		t.Fatal("expected RTP binary downlink to be queued")
	}
	if !session.shouldQueuePythonGatewayDownlink(pythonGatewayBridgeMessage{
		Type:   "done",
		IsJSON: true,
		JSON:   map[string]any{"type": "done"},
	}) {
		t.Fatal("expected done message to be queued behind RTP audio")
	}
	if session.shouldQueuePythonGatewayDownlink(pythonGatewayBridgeMessage{
		Type:   "status",
		IsJSON: true,
		JSON:   map[string]any{"type": "status"},
	}) {
		t.Fatal("status messages should stay on the immediate control path")
	}

	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebSocket
	wsServer, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer wsServer.Close()
	wsSession := &gatewaySession{server: wsServer}
	if wsSession.shouldQueuePythonGatewayDownlink(pythonGatewayBridgeMessage{Binary: []byte("VAF1-test-audio")}) {
		t.Fatal("websocket downlink should not use the RTP pacing queue")
	}
}

func TestGatewaySessionReplacesDoneWhenQueuedRTPAudioFails(t *testing.T) {
	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebRTCRTP
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}

	errCh := make(chan error, 1)
	go func() {
		errCh <- server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
			Type:   "playback_start",
			IsJSON: true,
			JSON: map[string]any{
				"type":        "playback_start",
				"trace_id":    "trace_failed_audio",
				"round_id":    "round_failed_audio",
				"playback_id": "round_failed_audio:playback",
			},
		})
	}()
	var playbackStart map[string]any
	if err := websocket.JSON.Receive(conn, &playbackStart); err != nil {
		t.Fatal(err)
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}

	if err := server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
		Binary: []byte("invalid VAF1 payload"),
	}); err != nil {
		t.Fatal(err)
	}
	if err := server.sessions.ForwardPythonGatewayDownlink(connected.SessionID, pythonGatewayBridgeMessage{
		Type:   "done",
		IsJSON: true,
		JSON:   map[string]any{"type": "done"},
	}); err != nil {
		t.Fatal(err)
	}

	var terminal map[string]any
	if err := websocket.JSON.Receive(conn, &terminal); err != nil {
		t.Fatal(err)
	}
	if terminal["type"] != "error" ||
		terminal["code"] != "DOWNLINK_AUDIO_FAILED" ||
		terminal["trace_id"] != "trace_failed_audio" ||
		terminal["round_id"] != "round_failed_audio" ||
		terminal["playback_id"] != "round_failed_audio:playback" {
		t.Fatalf("unexpected terminal after failed RTP audio: %+v", terminal)
	}
}

func TestGatewaySessionRegistryRejectsMissingSession(t *testing.T) {
	registry := newGatewaySessionRegistry()
	err := registry.ForwardPythonGatewayDownlink("missing", pythonGatewayBridgeMessage{
		Type:   "done",
		IsJSON: true,
		JSON:   map[string]any{"type": "done"},
	})
	if err == nil {
		t.Fatal("expected missing session error")
	}
}

func TestServerCloseClosesRegisteredSessions(t *testing.T) {
	var logs bytes.Buffer
	closedSessions := make(chan string, 1)
	server := &Server{
		cfg:          testConfig(),
		logger:       log.New(&logs, "", 0),
		clientEvents: &recordingClientEventProcessor{closedSessions: closedSessions},
		sessions:     newGatewaySessionRegistry(),
	}
	session := &gatewaySession{
		id:        "session_1",
		server:    server,
		createdAt: server.startedAt,
	}
	session.downTraceID = "trace_1"
	session.downRoundID = "round_1"
	session.downPlayID = "round_1:playback"
	server.sessions.Add(session)

	server.Close()

	if got := server.sessions.Count(); got != 0 {
		t.Fatalf("active sessions = %d, want 0", got)
	}
	select {
	case got := <-closedSessions:
		if got != "session_1" {
			t.Fatalf("closed session = %q, want session_1", got)
		}
	default:
		t.Fatal("expected client session closer to be called")
	}
	if !session.downBlocked.Load() {
		t.Fatal("expected downlink to be blocked after session close")
	}
	session.downMu.Lock()
	downTraceID := session.downTraceID
	downRoundID := session.downRoundID
	downPlayID := session.downPlayID
	session.downMu.Unlock()
	if downTraceID != "" || downRoundID != "" || downPlayID != "" {
		t.Fatalf("downlink ids not cleared: trace=%q round=%q playback=%q", downTraceID, downRoundID, downPlayID)
	}
	logText := logs.String()
	for _, want := range []string{
		"session_cleanup_done",
		"session=session_1",
		"python_bridge=true",
		"trace_id=trace_1",
		"round_id=round_1",
		"playback_id=round_1:playback",
		"server_sessions_closed count=1",
	} {
		if !strings.Contains(logText, want) {
			t.Fatalf("cleanup log missing %q in:\n%s", want, logText)
		}
	}
}

func TestSessionCloseCancelsActiveASRPrototypeSegment(t *testing.T) {
	prototype := NewASRPrototypeSink(8)
	server := &Server{
		cfg:         testConfig(),
		logger:      log.New(io.Discard, "", 0),
		voiceEvents: prototype,
		sessions:    newGatewaySessionRegistry(),
	}
	session := &gatewaySession{
		id:        "session_1",
		server:    server,
		createdAt: server.startedAt,
	}
	session.recordCanonicalEvent(CanonicalVoiceEvent{
		Type:        "audio_start",
		Transport:   canonicalTransportWebRTCRTP,
		TraceID:     "trace_1",
		UtteranceID: "utt_1",
	})

	session.close()
	prototype.Close()

	snapshot := prototype.Snapshot()
	if len(snapshot.Active) != 0 {
		t.Fatalf("active segments = %+v, want none", snapshot.Active)
	}
	if len(snapshot.Completed) != 1 {
		t.Fatalf("completed segments = %+v, want 1", snapshot.Completed)
	}
	segment := snapshot.Completed[0]
	if segment.State != asrPrototypeStateCancelled ||
		segment.Reason != "session_close" ||
		segment.TraceID != "trace_1" ||
		segment.UtteranceID != "utt_1" {
		t.Fatalf("unexpected cancelled segment: %+v", segment)
	}
}

func TestGatewaySessionRegistryResolvesPythonGatewayCredentials(t *testing.T) {
	registry := newGatewaySessionRegistry()
	session := &gatewaySession{id: "session_1"}
	secret := "secret_1"
	session.storeRegistrationCredentials(clientMessage{
		RobotSecret: &secret,
	}, deviceRegistration{
		RobotID:    "robot_1",
		BotID:      "bot_1",
		ClientType: "rust",
	})
	registry.Add(session)

	credentials, ok := registry.PythonGatewayCredentialsForSession("session_1")
	if !ok {
		t.Fatal("expected Python Gateway credentials")
	}
	if credentials.RobotID != "robot_1" ||
		credentials.RobotSecret != "secret_1" ||
		credentials.ClientType != "rust" {
		t.Fatalf("unexpected credentials: %+v", credentials)
	}
}

func TestGatewaySessionRegistrySkipsIncompletePythonGatewayCredentials(t *testing.T) {
	registry := newGatewaySessionRegistry()
	session := &gatewaySession{id: "session_1"}
	session.storeRegistrationCredentials(clientMessage{}, deviceRegistration{
		RobotID:    "robot_1",
		ClientType: "rust",
	})
	registry.Add(session)

	if credentials, ok := registry.PythonGatewayCredentialsForSession("session_1"); ok {
		t.Fatalf("unexpected incomplete credentials: %+v", credentials)
	}
}
