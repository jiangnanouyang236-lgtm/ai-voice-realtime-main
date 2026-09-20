package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/pion/rtp"
)

const (
	defaultDownlinkAudioTransport      = downlinkAudioTransportWebRTCRTP
	downlinkAudioTransportWebSocket    = "websocket"
	downlinkAudioTransportWebRTCRTP    = "webrtc_rtp"
	downlinkAudioTransportWebRTCMirror = "webrtc_rtp_mirror"
	downlinkOpusRTPPayloadType         = uint8(111)
	downlinkOpusRTPSSRC                = uint32(0x575a4b02)
	defaultDownlinkOpusFrameMS         = uint16(20)
)

type pythonGatewayDownlinkAudioFrame struct {
	Header      pythonGatewayAudioFrameHeader
	Payload     []byte
	OpusPackets [][]byte
}

type downlinkRTPSendState struct {
	nextSequenceNumber uint16
	timestamp          uint32
	ssrc               uint32
	payloadType        uint8
	nextSendAt         time.Time
}

func parseDownlinkAudioTransport(raw string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "":
		return defaultDownlinkAudioTransport, nil
	case downlinkAudioTransportWebSocket:
		return downlinkAudioTransportWebSocket, nil
	case downlinkAudioTransportWebRTCRTP:
		return downlinkAudioTransportWebRTCRTP, nil
	case downlinkAudioTransportWebRTCMirror:
		return downlinkAudioTransportWebRTCMirror, nil
	default:
		return "", fmt.Errorf("unsupported downlink audio transport: %s", raw)
	}
}

func parsePythonGatewayDownlinkAudioFrame(frame []byte) (pythonGatewayDownlinkAudioFrame, error) {
	if len(frame) < 8 {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink audio frame is too short: %d bytes", len(frame))
	}
	if string(frame[:4]) != pythonGatewayAudioFrameMagic {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink audio frame magic mismatch")
	}

	headerLen := int(binary.BigEndian.Uint32(frame[4:8]))
	if headerLen <= 0 {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink audio frame header is empty")
	}
	if headerLen > pythonGatewayAudioFrameHeaderMaxBytes {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf(
			"python gateway downlink audio frame header too large: %d > %d",
			headerLen,
			pythonGatewayAudioFrameHeaderMaxBytes,
		)
	}
	headerEnd := 8 + headerLen
	if headerEnd > len(frame) {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink audio frame header is truncated")
	}

	var header pythonGatewayAudioFrameHeader
	if err := json.Unmarshal(frame[8:headerEnd], &header); err != nil {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("parse python gateway downlink audio header: %w", err)
	}
	if header.Type != "audio_frame" {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink type = %q, want audio_frame", header.Type)
	}
	if header.Version != pythonGatewayAudioFrameVersion {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink version = %d, want %d", header.Version, pythonGatewayAudioFrameVersion)
	}
	if !strings.EqualFold(header.Encoding, asrHandoffAudioEncodingOpus) {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf("python gateway downlink encoding = %q, want opus", header.Encoding)
	}
	if direction := strings.ToLower(strings.TrimSpace(header.Direction)); direction != "" &&
		direction != "downlink" &&
		direction != "server_tts" &&
		direction != "server_output" {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf(
			"python gateway downlink direction = %q, want downlink/server_tts/server_output",
			header.Direction,
		)
	}

	payload := frame[headerEnd:]
	packets, err := parseDownlinkOpusPacketStream(payload)
	if err != nil {
		return pythonGatewayDownlinkAudioFrame{}, err
	}
	if header.PacketCount > 0 && header.PacketCount != len(packets) {
		return pythonGatewayDownlinkAudioFrame{}, fmt.Errorf(
			"python gateway downlink packet_count mismatch: header=%d payload=%d",
			header.PacketCount,
			len(packets),
		)
	}

	return pythonGatewayDownlinkAudioFrame{
		Header:      header,
		Payload:     append([]byte(nil), payload...),
		OpusPackets: packets,
	}, nil
}

func parseDownlinkOpusPacketStream(payload []byte) ([][]byte, error) {
	if !bytes.HasPrefix(payload, []byte(opusPacketStreamMagic)) {
		return nil, fmt.Errorf("python gateway downlink Opus payload magic mismatch")
	}
	packets := make([][]byte, 0)
	offset := len(opusPacketStreamMagic)
	for offset < len(payload) {
		if offset+2 > len(payload) {
			return nil, fmt.Errorf("python gateway downlink Opus packet length is truncated")
		}
		packetLen := int(binary.BigEndian.Uint16(payload[offset : offset+2]))
		offset += 2
		if packetLen <= 0 {
			return nil, fmt.Errorf("python gateway downlink Opus packet is empty")
		}
		packetEnd := offset + packetLen
		if packetEnd > len(payload) {
			return nil, fmt.Errorf("python gateway downlink Opus packet payload is truncated")
		}
		packets = append(packets, append([]byte(nil), payload[offset:packetEnd]...))
		offset = packetEnd
	}
	if len(packets) == 0 {
		return nil, fmt.Errorf("python gateway downlink Opus payload has no packets")
	}
	return packets, nil
}

func newDownlinkRTPSendState() downlinkRTPSendState {
	return downlinkRTPSendState{
		ssrc:        downlinkOpusRTPSSRC,
		payloadType: downlinkOpusRTPPayloadType,
	}
}

func (s *downlinkRTPSendState) packetsFromPythonGatewayDownlink(frame pythonGatewayDownlinkAudioFrame) ([]rtp.Packet, error) {
	if s.ssrc == 0 {
		s.ssrc = downlinkOpusRTPSSRC
	}
	if s.payloadType == 0 {
		s.payloadType = downlinkOpusRTPPayloadType
	}

	timestampStep, err := downlinkOpusRTPTimestampStep(frame.Header)
	if err != nil {
		return nil, err
	}
	packets := make([]rtp.Packet, 0, len(frame.OpusPackets))
	lastIndex := len(frame.OpusPackets) - 1
	for index, opusPacket := range frame.OpusPackets {
		packets = append(packets, rtp.Packet{
			Header: rtp.Header{
				Version:        2,
				Marker:         index == lastIndex,
				PayloadType:    s.payloadType,
				SequenceNumber: s.nextSequenceNumber,
				Timestamp:      s.timestamp,
				SSRC:           s.ssrc,
			},
			Payload: append([]byte(nil), opusPacket...),
		})
		s.nextSequenceNumber++
		s.timestamp += timestampStep
	}
	return packets, nil
}

func downlinkOpusRTPTimestampStep(header pythonGatewayAudioFrameHeader) (uint32, error) {
	frameMS := header.OpusFrameMS
	if frameMS == 0 {
		frameMS = defaultDownlinkOpusFrameMS
	}
	if frameMS == 0 {
		return 0, fmt.Errorf("python gateway downlink Opus frame duration is zero")
	}
	return 48 * uint32(frameMS), nil
}

func downlinkOpusFrameDuration(header pythonGatewayAudioFrameHeader) (time.Duration, error) {
	frameMS := header.OpusFrameMS
	if frameMS == 0 {
		frameMS = defaultDownlinkOpusFrameMS
	}
	if frameMS == 0 {
		return 0, fmt.Errorf("python gateway downlink Opus frame duration is zero")
	}
	return time.Duration(frameMS) * time.Millisecond, nil
}

func (s *downlinkRTPSendState) nextPacketSendDelay(frameDuration time.Duration, now time.Time) time.Duration {
	if frameDuration <= 0 {
		return 0
	}
	if s.nextSendAt.IsZero() || now.Sub(s.nextSendAt) > frameDuration {
		s.nextSendAt = now
	}
	delay := s.nextSendAt.Sub(now)
	s.nextSendAt = s.nextSendAt.Add(frameDuration)
	if delay < 0 {
		return 0
	}
	return delay
}
