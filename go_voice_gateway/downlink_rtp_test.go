package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"testing"
	"time"
)

func TestParsePythonGatewayDownlinkAudioFrameAndBuildRTP(t *testing.T) {
	frame := buildPythonGatewayDownlinkAudioFrameForTest(t, pythonGatewayAudioFrameHeader{
		Type:        "audio_frame",
		Version:     pythonGatewayAudioFrameVersion,
		Encoding:    "opus",
		Direction:   "server_tts",
		TraceID:     "trace_1",
		RoundID:     "round_1",
		PlaybackID:  "round_1:playback",
		SampleRate:  16000,
		Channels:    1,
		OpusFrameMS: 20,
		PacketCount: 2,
		ChunkSeq:    7,
		TimestampMS: 12345,
	}, [][]byte{
		[]byte("abc"),
		{4, 5},
	})

	parsed, err := parsePythonGatewayDownlinkAudioFrame(frame)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Header.Direction != "server_tts" ||
		parsed.Header.SampleRate != 16000 ||
		parsed.Header.PacketCount != 2 ||
		parsed.Header.TraceID != "trace_1" ||
		parsed.Header.RoundID != "round_1" ||
		parsed.Header.PlaybackID != "round_1:playback" ||
		parsed.Header.ChunkSeq != 7 ||
		parsed.Header.TimestampMS != 12345 {
		t.Fatalf("unexpected parsed header: %+v", parsed.Header)
	}
	if !bytes.Equal(parsed.Payload, []byte{'O', 'P', 'U', 'S', 'R', 'A', 'W', '1', 0, 3, 'a', 'b', 'c', 0, 2, 4, 5}) {
		t.Fatalf("unexpected OPUSRAW1 payload: %v", parsed.Payload)
	}

	state := newDownlinkRTPSendState()
	state.nextSequenceNumber = 42
	state.timestamp = 960
	state.ssrc = 1234

	packets, err := state.packetsFromPythonGatewayDownlink(parsed)
	if err != nil {
		t.Fatal(err)
	}
	if len(packets) != 2 {
		t.Fatalf("RTP packets = %d, want 2", len(packets))
	}
	if packets[0].Header.Version != 2 ||
		packets[0].Header.PayloadType != downlinkOpusRTPPayloadType ||
		packets[0].Header.SequenceNumber != 42 ||
		packets[0].Header.Timestamp != 960 ||
		packets[0].Header.SSRC != 1234 ||
		packets[0].Header.Marker {
		t.Fatalf("unexpected first RTP packet: %+v", packets[0].Header)
	}
	if packets[1].Header.SequenceNumber != 43 ||
		packets[1].Header.Timestamp != 1920 ||
		!packets[1].Header.Marker {
		t.Fatalf("unexpected second RTP packet: %+v", packets[1].Header)
	}
	if !bytes.Equal(packets[0].Payload, []byte("abc")) || !bytes.Equal(packets[1].Payload, []byte{4, 5}) {
		t.Fatalf("unexpected RTP payloads: %v %v", packets[0].Payload, packets[1].Payload)
	}
	if state.nextSequenceNumber != 44 || state.timestamp != 2880 {
		t.Fatalf("unexpected RTP state: %+v", state)
	}

	frameDuration, err := downlinkOpusFrameDuration(parsed.Header)
	if err != nil {
		t.Fatal(err)
	}
	if frameDuration != 20*time.Millisecond {
		t.Fatalf("frame duration = %s, want 20ms", frameDuration)
	}
	now := time.Unix(100, 0)
	if delay := state.nextPacketSendDelay(frameDuration, now); delay != 0 {
		t.Fatalf("first RTP pacing delay = %s, want 0", delay)
	}
	if delay := state.nextPacketSendDelay(frameDuration, now); delay != 20*time.Millisecond {
		t.Fatalf("second RTP pacing delay = %s, want 20ms", delay)
	}
	if delay := state.nextPacketSendDelay(frameDuration, now.Add(75*time.Millisecond)); delay != 0 {
		t.Fatalf("late RTP pacing delay = %s, want 0", delay)
	}
}

func TestParsePythonGatewayDownlinkAudioFrameAcceptsM1Direction(t *testing.T) {
	frame := buildPythonGatewayDownlinkAudioFrameForTest(t, pythonGatewayAudioFrameHeader{
		Type:        "audio_frame",
		Version:     pythonGatewayAudioFrameVersion,
		Encoding:    "opus",
		Direction:   "downlink",
		OpusFrameMS: 20,
		PacketCount: 1,
	}, [][]byte{[]byte("m1-opus")})

	parsed, err := parsePythonGatewayDownlinkAudioFrame(frame)
	if err != nil {
		t.Fatal(err)
	}
	if parsed.Header.Direction != "downlink" || len(parsed.OpusPackets) != 1 {
		t.Fatalf("unexpected M1 downlink frame: %+v", parsed)
	}
}

func TestDownlinkPlaybackMatchesLockedFiltersStalePlayback(t *testing.T) {
	session := &gatewaySession{
		downRoundID: "round_current",
		downPlayID:  "round_current:playback",
	}

	if !session.downlinkPlaybackMatchesLocked(pythonGatewayAudioFrameHeader{
		RoundID:    "round_current",
		PlaybackID: "round_current:playback",
	}) {
		t.Fatal("expected current playback frame to match")
	}
	if session.downlinkPlaybackMatchesLocked(pythonGatewayAudioFrameHeader{
		RoundID:    "round_old",
		PlaybackID: "round_old:playback",
	}) {
		t.Fatal("expected stale playback frame to be rejected")
	}
	if session.downlinkPlaybackMatchesLocked(pythonGatewayAudioFrameHeader{
		RoundID: "round_old",
	}) {
		t.Fatal("expected stale round-only frame to be rejected")
	}
	if session.downlinkPlaybackMatchesLocked(pythonGatewayAudioFrameHeader{}) {
		t.Fatal("expected metadata-free frame to be rejected while playback is active")
	}

	session.downRoundID = ""
	session.downPlayID = ""
	if !session.downlinkPlaybackMatchesLocked(pythonGatewayAudioFrameHeader{}) {
		t.Fatal("legacy frames without playback metadata should remain compatible before playback is active")
	}
}

func TestParsePythonGatewayDownlinkAudioFrameRejectsInvalidInput(t *testing.T) {
	validHeader := pythonGatewayAudioFrameHeader{
		Type:        "audio_frame",
		Version:     pythonGatewayAudioFrameVersion,
		Encoding:    "opus",
		Direction:   "server_tts",
		OpusFrameMS: 20,
		PacketCount: 1,
	}
	tests := []struct {
		name  string
		frame []byte
	}{
		{
			name:  "invalid magic",
			frame: []byte("NOPE\x00\x00\x00\x02{}"),
		},
		{
			name:  "truncated header",
			frame: []byte("VAF1\x00\x00\x00\x04{}"),
		},
		{
			name:  "invalid direction",
			frame: buildPythonGatewayDownlinkAudioFrameForTest(t, withDownlinkDirection(validHeader, "client_input"), [][]byte{[]byte("abc")}),
		},
		{
			name:  "packet count mismatch",
			frame: buildPythonGatewayDownlinkAudioFrameForTest(t, withDownlinkPacketCount(validHeader, 2), [][]byte{[]byte("abc")}),
		},
		{
			name:  "bad opus payload",
			frame: buildPythonGatewayDownlinkAudioFrameWithPayloadForTest(t, validHeader, []byte("BADRAW1")),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := parsePythonGatewayDownlinkAudioFrame(tt.frame); err == nil {
				t.Fatal("expected error")
			}
		})
	}
}

func TestParseDownlinkAudioTransport(t *testing.T) {
	got, err := parseDownlinkAudioTransport("")
	if err != nil {
		t.Fatalf("parseDownlinkAudioTransport(empty): %v", err)
	}
	if got != defaultDownlinkAudioTransport {
		t.Fatalf("empty transport = %q, want default %q", got, defaultDownlinkAudioTransport)
	}
	for _, value := range []string{"", "websocket", "webrtc_rtp", "webrtc_rtp_mirror", " WebRTC_RTP "} {
		if _, err := parseDownlinkAudioTransport(value); err != nil {
			t.Fatalf("parseDownlinkAudioTransport(%q): %v", value, err)
		}
	}
	if _, err := parseDownlinkAudioTransport("udp"); err == nil {
		t.Fatal("expected unsupported transport error")
	}
}

func buildPythonGatewayDownlinkAudioFrameForTest(t *testing.T, header pythonGatewayAudioFrameHeader, packets [][]byte) []byte {
	t.Helper()
	payload := []byte(opusPacketStreamMagic)
	for _, packet := range packets {
		if len(packet) == 0 || len(packet) > maxOpusPacketPayloadBytes {
			t.Fatalf("invalid test packet length: %d", len(packet))
		}
		payload = append(payload, byte(len(packet)>>8), byte(len(packet)))
		payload = append(payload, packet...)
	}
	return buildPythonGatewayDownlinkAudioFrameWithPayloadForTest(t, header, payload)
}

func buildPythonGatewayDownlinkAudioFrameWithPayloadForTest(t *testing.T, header pythonGatewayAudioFrameHeader, payload []byte) []byte {
	t.Helper()
	headerBytes, err := json.Marshal(header)
	if err != nil {
		t.Fatal(err)
	}
	frame := make([]byte, 8, 8+len(headerBytes)+len(payload))
	copy(frame[:4], pythonGatewayAudioFrameMagic)
	binary.BigEndian.PutUint32(frame[4:8], uint32(len(headerBytes)))
	frame = append(frame, headerBytes...)
	frame = append(frame, payload...)
	return frame
}

func withDownlinkDirection(header pythonGatewayAudioFrameHeader, direction string) pythonGatewayAudioFrameHeader {
	header.Direction = direction
	return header
}

func withDownlinkPacketCount(header pythonGatewayAudioFrameHeader, packetCount int) pythonGatewayAudioFrameHeader {
	header.PacketCount = packetCount
	return header
}
