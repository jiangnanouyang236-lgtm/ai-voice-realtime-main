package main

import (
	"encoding/json"
	"io"
	"log"
	"strings"
	"testing"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

func TestWebRTCWakeInterruptDropsOldRTPAndKeepsReplacementAfterLateReport(t *testing.T) {
	cfg := testConfig()
	cfg.Audio.DownlinkTransport = downlinkAudioTransportWebRTCRTP
	server, err := NewServer(cfg, log.New(io.Discard, "", 0))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()

	signaling, closeSignaling := newInMemoryWebSocket(t, server.Handler(), cfg.WebSocketPath)
	defer closeSignaling()

	var connected connectedMessage
	if err := websocket.JSON.Receive(signaling, &connected); err != nil {
		t.Fatal(err)
	}
	if err := websocket.JSON.Send(signaling, clientMessage{
		Type:       "register",
		RobotID:    "robot_1",
		ClientType: "rust",
	}); err != nil {
		t.Fatal(err)
	}
	var registered registeredMessage
	if err := websocket.JSON.Receive(signaling, &registered); err != nil {
		t.Fatal(err)
	}
	var rtcConfig rtcConfigMessage
	if err := websocket.JSON.Receive(signaling, &rtcConfig); err != nil {
		t.Fatal(err)
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

	downlinkRTP := make(chan *rtp.Packet, 32)
	clientPC.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go func() {
			for {
				packet, _, err := track.ReadRTP()
				if err != nil {
					return
				}
				select {
				case downlinkRTP <- packet:
				default:
				}
			}
		}()
	})

	controlOpen := make(chan struct{})
	controlReplies := make(chan string, 8)
	control, err := clientPC.CreateDataChannel("control", nil)
	if err != nil {
		t.Fatal(err)
	}
	control.OnOpen(func() {
		close(controlOpen)
	})
	control.OnMessage(func(message webrtc.DataChannelMessage) {
		controlReplies <- string(message.Data)
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
	if err := websocket.JSON.Send(signaling, clientMessage{
		Type:      "rtc_offer",
		SessionID: connected.SessionID,
		SDP:       clientPC.LocalDescription().SDP,
	}); err != nil {
		t.Fatal(err)
	}
	var answer rtcAnswerMessage
	if err := websocket.JSON.Receive(signaling, &answer); err != nil {
		t.Fatal(err)
	}
	if err := clientPC.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answer.SDP,
	}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(clientPC.LocalDescription().SDP, "candidate:") {
		t.Skip("local sandbox exposed no ICE host candidates")
	}
	waitForTestChannel(t, controlOpen, "control data channel open")

	session := server.sessions.Lookup(connected.SessionID)
	if session == nil {
		t.Fatalf("missing gateway session %s", connected.SessionID)
	}

	oldStart := forwardPlaybackStartForTest(
		t,
		server,
		signaling,
		connected.SessionID,
		"trace_old",
		"round_old",
		"round_old:playback",
	)
	if oldStart["rtp_start_sequence"] != float64(0) {
		t.Fatalf("old RTP boundary = %#v, want 0", oldStart["rtp_start_sequence"])
	}
	oldPackets := make([][]byte, 12)
	for index := range oldPackets {
		oldPackets[index] = []byte("old")
	}
	if err := server.sessions.ForwardPythonGatewayDownlink(
		connected.SessionID,
		pythonGatewayBridgeMessage{
			Binary: buildPythonGatewayDownlinkAudioFrameForTest(
				t,
				pythonGatewayAudioFrameHeader{
					Type:        "audio_frame",
					Version:     pythonGatewayAudioFrameVersion,
					Encoding:    "opus",
					Direction:   "server_tts",
					TraceID:     "trace_old",
					RoundID:     "round_old",
					PlaybackID:  "round_old:playback",
					OpusFrameMS: 20,
					PacketCount: len(oldPackets),
				},
				oldPackets,
			),
		},
	); err != nil {
		t.Fatal(err)
	}
	firstOld := waitForRTPPacket(t, downlinkRTP, "first old RTP")
	if string(firstOld.Payload) != "old" {
		t.Fatalf("first old RTP payload = %q", firstOld.Payload)
	}

	wakeEvent, err := json.Marshal(clientMessage{
		Type:    "client_event",
		Event:   "wake_interrupt",
		EventID: "wake_interrupt-webrtc-1",
		Source:  "hardware_wake",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := control.SendText(string(wakeEvent)); err != nil {
		t.Fatal(err)
	}
	waitForSmokeCondition(t, "wake interrupt blocks old RTP", func() bool {
		return session.downBlocked.Load()
	})
	drainRTPPackets(downlinkRTP, 100*time.Millisecond)

	newStart := forwardPlaybackStartForTest(
		t,
		server,
		signaling,
		connected.SessionID,
		"trace_new",
		"round_new",
		"round_new:playback",
	)
	startSequence, ok := newStart["rtp_start_sequence"].(float64)
	if !ok || startSequence < 1 {
		t.Fatalf("replacement RTP boundary = %#v, want sequence after old RTP", newStart["rtp_start_sequence"])
	}

	lateReport, err := json.Marshal(clientMessage{
		Type:       "playback_interrupted",
		TraceID:    "trace_old",
		RoundID:    "round_old",
		PlaybackID: "round_old:playback",
		Reason:     "wake_interrupt",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := control.SendText(string(lateReport)); err != nil {
		t.Fatal(err)
	}
	if err := control.SendText("ping"); err != nil {
		t.Fatal(err)
	}
	select {
	case reply := <-controlReplies:
		if reply != "pong:ping" {
			t.Fatalf("control reply = %q, want pong:ping", reply)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout waiting for control ordering barrier")
	}

	session.downMu.Lock()
	replacementRoundID := session.downRoundID
	replacementPlaybackID := session.downPlayID
	session.downMu.Unlock()
	if session.downBlocked.Load() ||
		replacementRoundID != "round_new" ||
		replacementPlaybackID != "round_new:playback" {
		t.Fatalf(
			"late old report changed replacement: blocked=%t round=%s playback=%s",
			session.downBlocked.Load(),
			replacementRoundID,
			replacementPlaybackID,
		)
	}

	if err := server.sessions.ForwardPythonGatewayDownlink(
		connected.SessionID,
		pythonGatewayBridgeMessage{
			Binary: buildPythonGatewayDownlinkAudioFrameForTest(
				t,
				pythonGatewayAudioFrameHeader{
					Type:        "audio_frame",
					Version:     pythonGatewayAudioFrameVersion,
					Encoding:    "opus",
					Direction:   "server_tts",
					TraceID:     "trace_new",
					RoundID:     "round_new",
					PlaybackID:  "round_new:playback",
					OpusFrameMS: 20,
					PacketCount: 1,
				},
				[][]byte{[]byte("new")},
			),
		},
	); err != nil {
		t.Fatal(err)
	}
	replacement := waitForRTPPacket(t, downlinkRTP, "replacement RTP")
	if string(replacement.Payload) != "new" {
		t.Fatalf("RTP after replacement start = %q, want new", replacement.Payload)
	}
	if replacement.SequenceNumber != uint16(startSequence) {
		t.Fatalf(
			"replacement RTP sequence = %d, playback_start boundary = %.0f",
			replacement.SequenceNumber,
			startSequence,
		)
	}
}

func forwardPlaybackStartForTest(
	t *testing.T,
	server *Server,
	signaling *websocket.Conn,
	sessionID string,
	traceID string,
	roundID string,
	playbackID string,
) map[string]any {
	t.Helper()
	errCh := make(chan error, 1)
	go func() {
		errCh <- server.sessions.ForwardPythonGatewayDownlink(sessionID, pythonGatewayBridgeMessage{
			Type:   "playback_start",
			IsJSON: true,
			JSON: map[string]any{
				"type":        "playback_start",
				"trace_id":    traceID,
				"round_id":    roundID,
				"playback_id": playbackID,
			},
		})
	}()
	var playbackStart map[string]any
	for {
		if err := websocket.JSON.Receive(signaling, &playbackStart); err != nil {
			t.Fatal(err)
		}
		if playbackStart["type"] == "playback_start" {
			break
		}
	}
	if err := <-errCh; err != nil {
		t.Fatal(err)
	}
	return playbackStart
}

func waitForRTPPacket(t *testing.T, packets <-chan *rtp.Packet, label string) *rtp.Packet {
	t.Helper()
	select {
	case packet := <-packets:
		return packet
	case <-time.After(5 * time.Second):
		t.Fatalf("timeout waiting for %s", label)
		return nil
	}
}

func drainRTPPackets(packets <-chan *rtp.Packet, quiet time.Duration) {
	timer := time.NewTimer(quiet)
	defer timer.Stop()
	for {
		select {
		case <-packets:
			if !timer.Stop() {
				<-timer.C
			}
			timer.Reset(quiet)
		case <-timer.C:
			return
		}
	}
}
