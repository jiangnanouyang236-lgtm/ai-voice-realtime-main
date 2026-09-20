package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"sync"
	"testing"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

func TestWebRTCRTPToPythonGatewayBridgeDownlinkSmoke(t *testing.T) {
	const (
		downlinkRoundID    = "python_smoke:1"
		downlinkPlaybackID = "python_smoke:1:playback"
	)
	fakeDownlinkBinary := buildPythonGatewayDownlinkAudioFrameForTest(t, pythonGatewayAudioFrameHeader{
		Type:        "audio_frame",
		Version:     pythonGatewayAudioFrameVersion,
		Encoding:    "opus",
		Direction:   "server_tts",
		RoundID:     downlinkRoundID,
		PlaybackID:  downlinkPlaybackID,
		SampleRate:  16000,
		Channels:    1,
		OpusFrameMS: 20,
		PacketCount: 1,
	}, [][]byte{{0xf8, 0xff, 0xfe}})
	fakePython := newFakePythonGatewayBridge(t, fakeDownlinkBinary)
	defer fakePython.Close()

	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebRTCMirror
	cfg.ASR = ASRConfig{
		Processor:                 asrProcessorPythonGateway,
		HandoffQueueSize:          4,
		PythonGatewayWSURL:        "ws://python-gateway.test/ws",
		PythonGatewayClientType:   "go_smoke",
		PythonGatewayBotID:        "xiaowen",
		PythonGatewayTimeoutMS:    3000,
		PythonGatewayMaxMessageKB: 64,
	}
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	managed, ok := server.voiceEvents.(*configuredASRVoiceEventSink)
	if !ok {
		t.Fatalf("voiceEvents = %T, want configuredASRVoiceEventSink", server.voiceEvents)
	}
	attachFakePythonGatewayBridge(t, managed, fakePython)

	conn, closeConn := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeConn()
	defer conn.SetDeadline(time.Time{})

	var connected connectedMessage
	if err := websocket.JSON.Receive(conn, &connected); err != nil {
		t.Fatal(err)
	}
	if connected.SessionID == "" {
		t.Fatalf("missing session id: %+v", connected)
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:       "register",
		RobotID:    "robot_smoke",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(conn, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(conn, &rtcConfig); err != nil {
		t.Fatal(err)
	}
	if registered.SessionID != connected.SessionID || rtcConfig.SessionID != connected.SessionID {
		t.Fatalf("session mismatch connected=%s registered=%s rtc_config=%s", connected.SessionID, registered.SessionID, rtcConfig.SessionID)
	}

	clientAPI, err := newWebRTCAPI(cfg)
	if err != nil {
		t.Fatal(err)
	}
	clientPC, err := clientAPI.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	defer clientPC.Close()
	downlinkRTPCh := make(chan *rtp.Packet, 1)
	clientPC.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go func() {
			packet, _, err := track.ReadRTP()
			if err == nil {
				downlinkRTPCh <- packet
			}
		}()
	})

	var candidateMu sync.Mutex
	gatheredCandidates := 0
	clientPC.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		if candidate != nil {
			candidateMu.Lock()
			gatheredCandidates++
			candidateMu.Unlock()
		}
	})

	audioTrack, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus},
		"microphone",
		"voice",
	)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := clientPC.AddTrack(audioTrack); err != nil {
		t.Fatal(err)
	}

	openCh := make(chan struct{})
	dc, err := clientPC.CreateDataChannel("control", nil)
	if err != nil {
		t.Fatal(err)
	}
	dc.OnOpen(func() {
		close(openCh)
	})

	offer, err := clientPC.CreateOffer(nil)
	if err != nil {
		t.Fatal(err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(clientPC)
	if err := clientPC.SetLocalDescription(offer); err != nil {
		t.Fatal(err)
	}
	waitForTestChannel(t, gatherComplete, "client ICE gathering")
	candidateMu.Lock()
	gatheredCandidateCount := gatheredCandidates
	candidateMu.Unlock()
	if gatheredCandidateCount == 0 {
		t.Skip("local sandbox exposed no ICE host candidates; rtc_offer/rtc_answer passed, skipping WebRTC RTP bridge smoke")
	}
	if err := websocket.JSON.Send(conn, clientMessage{
		Type:      "rtc_offer",
		SessionID: connected.SessionID,
		SDP:       clientPC.LocalDescription().SDP,
	}); err != nil {
		t.Fatal(err)
	}
	var answer rtcAnswerMessage
	if err := websocket.JSON.Receive(conn, &answer); err != nil {
		t.Fatal(err)
	}
	if answer.Type != "rtc_answer" || answer.SessionID != connected.SessionID {
		t.Fatalf("unexpected rtc_answer: %+v", answer)
	}
	if err := clientPC.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answer.SDP,
	}); err != nil {
		t.Fatal(err)
	}
	waitForTestChannel(t, openCh, "data channel open")

	utteranceID := "utt_smoke"
	if err := dc.SendText(fmt.Sprintf(
		`{"type":"audio_start","session_id":%q,"bot_id":"xiaowen","utterance_id":%q}`,
		connected.SessionID,
		utteranceID,
	)); err != nil {
		t.Fatalf("send audio_start: %v", err)
	}
	waitForSmokeCondition(t, "ASR active segment", func() bool {
		for _, segment := range managed.prototype.Snapshot().Active {
			if segment.SessionID == connected.SessionID && segment.UtteranceID == utteranceID {
				return true
			}
		}
		return false
	})

	if err := audioTrack.WriteRTP(&rtp.Packet{
		Header: rtp.Header{
			Version:        2,
			PayloadType:    111,
			SequenceNumber: 1,
			Timestamp:      960,
			SSRC:           0x575a4b02,
		},
		Payload: []byte{0xf8, 0xff, 0xfe},
	}); err != nil {
		t.Fatalf("write RTP: %v", err)
	}
	waitForSmokeCondition(t, "ASR RTP payload retention", func() bool {
		for _, segment := range managed.prototype.Snapshot().Active {
			if segment.SessionID == connected.SessionID &&
				segment.AudioPackets > 0 &&
				len(segment.EncodedPackets) > 0 {
				return true
			}
		}
		return false
	})

	if err := dc.SendText(fmt.Sprintf(
		`{"type":"audio_end","session_id":%q,"bot_id":"xiaowen","utterance_id":%q}`,
		connected.SessionID,
		utteranceID,
	)); err != nil {
		t.Fatalf("send audio_end: %v", err)
	}

	var pythonFrame []byte
	select {
	case pythonFrame = <-fakePython.frameCh:
	case err := <-fakePython.errCh:
		t.Fatal(err)
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for Python Gateway bridge audio frame")
	}
	header, payload := decodePythonGatewayAudioFrameForTest(t, pythonFrame)
	if header.Direction != "client_input" ||
		header.Encoding != asrAudioRequestEncodingOpus ||
		header.BotID != "xiaowen" ||
		header.UtteranceID != utteranceID ||
		header.PacketCount != 1 {
		t.Fatalf("unexpected Python Gateway frame header: %+v", header)
	}
	if !bytes.HasPrefix(payload, []byte(opusPacketStreamMagic)) {
		t.Fatalf("Python Gateway frame payload missing %s prefix: %q", opusPacketStreamMagic, payload)
	}

	gotStatus, gotPlaybackStart, gotBinary, gotDone := receiveSmokeDownlink(t, conn, fakeDownlinkBinary)
	if !gotStatus || !gotPlaybackStart || !gotBinary || !gotDone {
		t.Fatalf(
			"downlink status=%t playback_start=%t binary=%t done=%t, want all true",
			gotStatus,
			gotPlaybackStart,
			gotBinary,
			gotDone,
		)
	}
	select {
	case packet := <-downlinkRTPCh:
		if packet.PayloadType != downlinkOpusRTPPayloadType ||
			!bytes.Equal(packet.Payload, []byte{0xf8, 0xff, 0xfe}) {
			t.Fatalf("unexpected downlink RTP packet: header=%+v payload=%v", packet.Header, packet.Payload)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for mirrored WebRTC downlink RTP packet")
	}
}

type fakePythonGatewayBridge struct {
	frameCh chan []byte
	errCh   chan error
	handler websocket.Handler
	t       *testing.T
}

func (b *fakePythonGatewayBridge) Close() {}

func (b *fakePythonGatewayBridge) dial(ctx context.Context, _ string) (*websocket.Conn, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	default:
	}
	conn, closeConn := newInMemoryWebSocket(b.t, websocket.Server{Handler: b.handler}, "/ws")
	b.t.Cleanup(closeConn)
	return conn, nil
}

func newFakePythonGatewayBridge(t *testing.T, downlinkBinary []byte) *fakePythonGatewayBridge {
	t.Helper()
	frameCh := make(chan []byte, 1)
	errCh := make(chan error, 1)
	recordErr := func(err error) {
		select {
		case errCh <- err:
		default:
		}
	}
	handler := websocket.Handler(func(conn *websocket.Conn) {
		defer conn.Close()
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":       "connected",
			"session_id": "python_smoke",
		}); err != nil {
			recordErr(fmt.Errorf("send fake Python connected: %w", err))
			return
		}

		var frame []byte
		if err := websocket.Message.Receive(conn, &frame); err != nil {
			recordErr(fmt.Errorf("receive Go bridge audio frame: %w", err))
			return
		}
		frameCh <- append([]byte(nil), frame...)

		if err := websocket.JSON.Send(conn, map[string]any{
			"type":    "status",
			"message": "smoke processing",
		}); err != nil {
			recordErr(fmt.Errorf("send fake Python status: %w", err))
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{
			"type":        "playback_start",
			"round_id":    "python_smoke:1",
			"playback_id": "python_smoke:1:playback",
		}); err != nil {
			recordErr(fmt.Errorf("send fake Python playback_start: %w", err))
			return
		}
		if err := websocket.Message.Send(conn, downlinkBinary); err != nil {
			recordErr(fmt.Errorf("send fake Python binary downlink: %w", err))
			return
		}
		if err := websocket.JSON.Send(conn, map[string]any{"type": "done"}); err != nil {
			recordErr(fmt.Errorf("send fake Python done: %w", err))
			return
		}
	})
	return &fakePythonGatewayBridge{
		frameCh: frameCh,
		errCh:   errCh,
		handler: handler,
		t:       t,
	}
}

func attachFakePythonGatewayBridge(t *testing.T, managed *configuredASRVoiceEventSink, fake *fakePythonGatewayBridge) {
	t.Helper()
	adapter, ok := managed.handoffQueue.processor.(ASRAudioRequestProcessorAdapter)
	if !ok {
		t.Fatalf("handoff processor = %T, want ASRAudioRequestProcessorAdapter", managed.handoffQueue.processor)
	}
	reporting, ok := adapter.processor.(asrBridgeErrorReportingProcessor)
	if !ok {
		t.Fatalf("ASR request processor = %T, want asrBridgeErrorReportingProcessor", adapter.processor)
	}
	bridge, ok := reporting.processor.(*PythonGatewayBridgeProcessor)
	if !ok {
		t.Fatalf("wrapped ASR request processor = %T, want *PythonGatewayBridgeProcessor", reporting.processor)
	}
	bridge.dialer = fake.dial
}

func waitForSmokeCondition(t *testing.T, label string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if ok() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", label)
}

func receiveSmokeDownlink(t *testing.T, conn *websocket.Conn, expectedBinary []byte) (bool, bool, bool, bool) {
	t.Helper()
	if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		t.Fatal(err)
	}

	gotStatus := false
	gotPlaybackStart := false
	gotBinary := false
	gotDone := false
	for !(gotStatus && gotPlaybackStart && gotBinary && gotDone) {
		var payload []byte
		if err := websocket.Message.Receive(conn, &payload); err != nil {
			t.Fatalf("receive smoke downlink: %v", err)
		}

		var decoded map[string]any
		if err := json.Unmarshal(payload, &decoded); err == nil {
			switch decoded["type"] {
			case "status":
				if decoded["message"] != "smoke processing" {
					t.Fatalf("unexpected status downlink: %+v", decoded)
				}
				gotStatus = true
			case "playback_start":
				if decoded["round_id"] != "python_smoke:1" ||
					decoded["playback_id"] != "python_smoke:1:playback" {
					t.Fatalf("unexpected playback_start downlink: %+v", decoded)
				}
				gotPlaybackStart = true
			case "done":
				gotDone = true
			case "rtc_ice_candidate":
				continue
			default:
				t.Fatalf("unexpected JSON downlink: %+v", decoded)
			}
			continue
		}

		if bytes.Equal(payload, expectedBinary) {
			gotBinary = true
			continue
		}
		t.Fatalf("unexpected binary downlink: %v", payload)
	}
	return gotStatus, gotPlaybackStart, gotBinary, gotDone
}
