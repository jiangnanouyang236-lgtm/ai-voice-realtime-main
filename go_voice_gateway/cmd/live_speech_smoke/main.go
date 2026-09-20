package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

const (
	defaultWebSocketURL = "ws://127.0.0.1:8282/ws"
	defaultOrigin       = "http://127.0.0.1:8282"
	defaultRobotID      = "companion_01"
	defaultClientType   = "go_live_smoke"
	defaultBotID        = "xiaowen"
	defaultTimeout      = 90 * time.Second
	defaultPacketGap    = 20 * time.Millisecond
)

type connectedMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
}

type registeredMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	RobotID   string `json:"robot_id"`
	BotID     string `json:"bot_id"`
}

type rtcConfigMessage struct {
	Type       string            `json:"type"`
	SessionID  string            `json:"session_id"`
	ICEServers []iceServerConfig `json:"ice_servers"`
	Media      rtcMediaConfig    `json:"media"`
}

type rtcMediaConfig struct {
	Audio rtcAudioConfig `json:"audio"`
}

type rtcAudioConfig struct {
	DownlinkTransport string `json:"downlink_transport"`
}

type iceServerConfig struct {
	URLs       []string `json:"urls"`
	Username   string   `json:"username,omitempty"`
	Credential string   `json:"credential,omitempty"`
}

type rtcAnswerMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
	SDP       string `json:"sdp"`
}

type genericJSONMessage struct {
	Type           string `json:"type"`
	SessionID      string `json:"session_id,omitempty"`
	TraceID        string `json:"trace_id,omitempty"`
	UtteranceID    string `json:"utterance_id,omitempty"`
	RoundID        string `json:"round_id,omitempty"`
	PlaybackID     string `json:"playback_id,omitempty"`
	Message        string `json:"message,omitempty"`
	Text           string `json:"text,omitempty"`
	Content        string `json:"content,omitempty"`
	Code           string `json:"code,omitempty"`
	CandidateSeq   uint64 `json:"candidate_seq,omitempty"`
	SpeechEpoch    uint64 `json:"speech_epoch,omitempty"`
	AudioWatermark uint64 `json:"audio_watermark,omitempty"`
}

type packetFile struct {
	Packets []string `json:"packets"`
}

type config struct {
	WebSocketURL             string
	Origin                   string
	RobotID                  string
	RobotSecret              string
	ClientType               string
	BotID                    string
	PacketsPath              string
	UtteranceID              string
	Timeout                  time.Duration
	PacketGap                time.Duration
	ConnectWait              time.Duration
	DoneWait                 time.Duration
	DisableRTPGap            bool
	InterruptDelay           time.Duration
	InterruptEvent           string
	TraceID                  string
	TurnCandidateEnabled     bool
	TurnCandidateActive      bool
	TurnCandidateSilenceMS   uint64
	TurnCandidateDelay       time.Duration
	TurnCandidateAudioEndGap time.Duration
	TurnCandidateResumeSplit int
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "live_speech_smoke error: %v\n", err)
		os.Exit(1)
	}
}

func run() error {
	cfg := loadConfig()
	if strings.TrimSpace(cfg.PacketsPath) == "" {
		return errors.New("LIVE_OPUS_PACKETS_PATH is required")
	}
	packets, err := loadPackets(cfg.PacketsPath)
	if err != nil {
		return err
	}
	if len(packets) == 0 {
		return errors.New("packet file contains no Opus packets")
	}
	fmt.Printf("packets=%d\n", len(packets))

	conn, err := dialWebSocket(cfg)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(cfg.Timeout))

	connected, err := receiveTyped[connectedMessage](conn, "connected")
	if err != nil {
		return err
	}
	if connected.SessionID == "" {
		return fmt.Errorf("connected missing session_id: %+v", connected)
	}
	fmt.Printf("go_session=%s\n", connected.SessionID)

	registerPayload := map[string]any{
		"type":        "register",
		"robot_id":    cfg.RobotID,
		"client_type": cfg.ClientType,
	}
	if strings.TrimSpace(cfg.RobotSecret) != "" {
		registerPayload["robot_secret"] = cfg.RobotSecret
	}
	if err := websocket.JSON.Send(conn, registerPayload); err != nil {
		return fmt.Errorf("send register: %w", err)
	}
	registered, err := receiveTyped[registeredMessage](conn, "registered")
	if err != nil {
		return err
	}
	if registered.SessionID != connected.SessionID {
		return fmt.Errorf("registered session mismatch: connected=%s registered=%s", connected.SessionID, registered.SessionID)
	}

	rtcCfg, err := receiveTyped[rtcConfigMessage](conn, "rtc_config")
	if err != nil {
		return err
	}
	if rtcCfg.SessionID != connected.SessionID {
		return fmt.Errorf("rtc_config session mismatch: connected=%s rtc_config=%s", connected.SessionID, rtcCfg.SessionID)
	}

	pc, track, dc, downlinkRTPPackets, err := createPeer(rtcCfg)
	if err != nil {
		return err
	}
	defer pc.Close()

	openCh := make(chan struct{})
	iceConnectedCh := make(chan struct{})
	closeOnce := make(chan struct{})
	dc.OnOpen(func() {
		closeChannelOnce(openCh)
	})
	activeCommitCh := make(chan genericJSONMessage, 1)
	var fallbackSent atomic.Bool
	expectedCandidateSeq := uint64(1)
	expectedSpeechEpoch := uint64(0)
	if cfg.TurnCandidateResumeSplit > 0 && cfg.TurnCandidateResumeSplit < len(packets) {
		expectedCandidateSeq = 2
		expectedSpeechEpoch = 1
	}
	dc.OnMessage(func(message webrtc.DataChannelMessage) {
		if !message.IsString {
			return
		}
		var control genericJSONMessage
		if err := json.Unmarshal(message.Data, &control); err != nil || control.Type != "turn_commit_request" {
			return
		}
		accepted := cfg.TurnCandidateActive &&
			!fallbackSent.Load() &&
			control.TraceID == cfg.TraceID &&
			control.UtteranceID == cfg.UtteranceID &&
			control.CandidateSeq == expectedCandidateSeq &&
			control.SpeechEpoch == expectedSpeechEpoch &&
			control.AudioWatermark == uint64(len(packets)*320)
		reason := "current_candidate"
		if !accepted {
			reason = "stale_candidate"
		}
		if err := dc.SendText(jsonString(map[string]any{
			"type":            "turn_commit_ack",
			"trace_id":        control.TraceID,
			"utterance_id":    control.UtteranceID,
			"candidate_seq":   control.CandidateSeq,
			"speech_epoch":    control.SpeechEpoch,
			"audio_watermark": control.AudioWatermark,
			"accepted":        accepted,
			"reason":          reason,
		})); err != nil {
			fmt.Printf("turn_commit_ack_send_error=%v\n", err)
			return
		}
		fmt.Printf(
			"turn_commit_ack_sent utterance_id=%s candidate_seq=%d speech_epoch=%d audio_watermark=%d accepted=%t reason=%s\n",
			control.UtteranceID,
			control.CandidateSeq,
			control.SpeechEpoch,
			control.AudioWatermark,
			accepted,
			reason,
		)
		if accepted {
			select {
			case activeCommitCh <- control:
			default:
			}
		}
	})
	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		if state == webrtc.ICEConnectionStateConnected || state == webrtc.ICEConnectionStateCompleted {
			closeChannelOnce(iceConnectedCh)
		}
		if state == webrtc.ICEConnectionStateClosed || state == webrtc.ICEConnectionStateFailed || state == webrtc.ICEConnectionStateDisconnected {
			closeChannelOnce(closeOnce)
		}
	})

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return fmt.Errorf("create offer: %w", err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		return fmt.Errorf("set local offer: %w", err)
	}
	select {
	case <-gatherComplete:
	case <-time.After(cfg.ConnectWait):
		return errors.New("timeout waiting for local ICE gathering")
	}
	local := pc.LocalDescription()
	if local == nil {
		return errors.New("missing local description")
	}
	if err := websocket.JSON.Send(conn, map[string]any{
		"type":       "rtc_offer",
		"session_id": connected.SessionID,
		"sdp":        local.SDP,
	}); err != nil {
		return fmt.Errorf("send rtc_offer: %w", err)
	}

	answer, err := receiveTyped[rtcAnswerMessage](conn, "rtc_answer")
	if err != nil {
		return err
	}
	if answer.SessionID != connected.SessionID {
		return fmt.Errorf("rtc_answer session mismatch: connected=%s rtc_answer=%s", connected.SessionID, answer.SessionID)
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answer.SDP,
	}); err != nil {
		return fmt.Errorf("set remote answer: %w", err)
	}

	select {
	case <-openCh:
		fmt.Println("datachannel=open")
	case <-closeOnce:
		return errors.New("peer connection closed before data channel opened")
	case <-time.After(cfg.ConnectWait):
		return errors.New("timeout waiting for data channel open")
	}
	select {
	case <-iceConnectedCh:
		fmt.Println("ice=connected")
	default:
	}

	if err := dc.SendText(jsonString(map[string]any{
		"type":             "audio_start",
		"session_id":       connected.SessionID,
		"bot_id":           cfg.BotID,
		"utterance_id":     cfg.UtteranceID,
		"sample_rate":      16000,
		"channels":         1,
		"opus_frame_ms":    20,
		"packet_count":     len(packets),
		"audio_transport":  "webrtc_rtp",
		"audio_encoding":   "opus",
		"packet_stream_id": cfg.UtteranceID,
		"trace_id":         cfg.TraceID,
	})); err != nil {
		return fmt.Errorf("send audio_start: %w", err)
	}

	sendPackets := func(start, end int) error {
		for i := start; i < end; i++ {
			if err := track.WriteRTP(&rtp.Packet{
				Header: rtp.Header{
					Version:        2,
					PayloadType:    111,
					SequenceNumber: uint16(i + 1),
					Timestamp:      uint32((i + 1) * 960),
					SSRC:           0x575a4b03,
				},
				Payload: packets[i],
			}); err != nil {
				return fmt.Errorf("write RTP packet %d: %w", i+1, err)
			}
			if !cfg.DisableRTPGap && cfg.PacketGap > 0 {
				time.Sleep(cfg.PacketGap)
			}
		}
		return nil
	}
	sendCandidate := func(candidateSeq, speechEpoch uint64, packetCount int) error {
		if cfg.TurnCandidateDelay > 0 {
			time.Sleep(cfg.TurnCandidateDelay)
		}
		if err := dc.SendText(jsonString(map[string]any{
			"type":            "turn_candidate",
			"session_id":      connected.SessionID,
			"bot_id":          cfg.BotID,
			"trace_id":        cfg.TraceID,
			"utterance_id":    cfg.UtteranceID,
			"candidate_seq":   candidateSeq,
			"speech_epoch":    speechEpoch,
			"audio_watermark": packetCount * 320,
			"silence_ms":      cfg.TurnCandidateSilenceMS,
			"shadow":          !cfg.TurnCandidateActive,
		})); err != nil {
			return fmt.Errorf("send turn_candidate %d: %w", candidateSeq, err)
		}
		fmt.Printf(
			"turn_candidate_sent utterance_id=%s trace_id=%s candidate_seq=%d speech_epoch=%d silence_ms=%d audio_watermark=%d\n",
			cfg.UtteranceID,
			cfg.TraceID,
			candidateSeq,
			speechEpoch,
			cfg.TurnCandidateSilenceMS,
			packetCount*320,
		)
		return nil
	}
	resumeSplit := cfg.TurnCandidateResumeSplit
	if !cfg.TurnCandidateEnabled || resumeSplit <= 0 || resumeSplit >= len(packets) {
		if err := sendPackets(0, len(packets)); err != nil {
			return err
		}
		if cfg.TurnCandidateEnabled {
			if err := sendCandidate(1, 0, len(packets)); err != nil {
				return err
			}
		}
	} else {
		if err := sendPackets(0, resumeSplit); err != nil {
			return err
		}
		if err := sendCandidate(1, 0, resumeSplit); err != nil {
			return err
		}
		fmt.Printf("speech_resumed after_candidate_seq=1 remaining_packets=%d\n", len(packets)-resumeSplit)
		if err := sendPackets(resumeSplit, len(packets)); err != nil {
			return err
		}
		if err := sendCandidate(2, 1, len(packets)); err != nil {
			return err
		}
	}
	activeCommitted := false
	if cfg.TurnCandidateEnabled && cfg.TurnCandidateActive {
		wait := cfg.TurnCandidateAudioEndGap
		if wait <= 0 {
			wait = 200 * time.Millisecond
		}
		select {
		case commit := <-activeCommitCh:
			activeCommitted = true
			fmt.Printf("turn_commit_active=true utterance_id=%s candidate_seq=%d\n", commit.UtteranceID, commit.CandidateSeq)
		case <-time.After(wait):
			fallbackSent.Store(true)
			fmt.Printf("turn_commit_active=false fallback_after_ms=%d\n", wait.Milliseconds())
		}
	} else if cfg.TurnCandidateEnabled && cfg.TurnCandidateAudioEndGap > 0 {
		time.Sleep(cfg.TurnCandidateAudioEndGap)
	}
	if !activeCommitted {
		fallbackSent.Store(true)
		if err := dc.SendText(jsonString(map[string]any{
			"type":         "audio_end",
			"session_id":   connected.SessionID,
			"bot_id":       cfg.BotID,
			"utterance_id": cfg.UtteranceID,
			"duration_ms":  len(packets) * 20,
			"trace_id":     cfg.TraceID,
		})); err != nil {
			return fmt.Errorf("send audio_end: %w", err)
		}
		fmt.Printf("audio_sent utterance_id=%s packets=%d\n", cfg.UtteranceID, len(packets))
	}

	return receiveDownlinkUntilDone(
		conn,
		cfg.DoneWait,
		expectsDownlinkRTP(rtcCfg),
		downlinkRTPPackets,
		cfg.InterruptDelay,
		cfg.InterruptEvent,
	)
}

func loadConfig() config {
	timeout := envDurationMS("GO_LIVE_SMOKE_TIMEOUT_MS", defaultTimeout)
	cfg := config{
		WebSocketURL:   env("GO_LIVE_SMOKE_WS_URL", defaultWebSocketURL),
		Origin:         env("GO_LIVE_SMOKE_ORIGIN", defaultOrigin),
		RobotID:        env("GO_LIVE_SMOKE_ROBOT_ID", defaultRobotID),
		RobotSecret:    os.Getenv("GO_LIVE_SMOKE_ROBOT_SECRET"),
		ClientType:     env("GO_LIVE_SMOKE_CLIENT_TYPE", defaultClientType),
		BotID:          env("GO_LIVE_SMOKE_BOT_ID", defaultBotID),
		PacketsPath:    strings.TrimSpace(os.Getenv("LIVE_OPUS_PACKETS_PATH")),
		UtteranceID:    env("GO_LIVE_SMOKE_UTTERANCE_ID", fmt.Sprintf("live-%d", time.Now().UnixNano())),
		Timeout:        timeout,
		PacketGap:      envDurationMS("GO_LIVE_SMOKE_PACKET_INTERVAL_MS", defaultPacketGap),
		ConnectWait:    envDurationMS("GO_LIVE_SMOKE_CONNECT_TIMEOUT_MS", 15*time.Second),
		DoneWait:       envDurationMS("GO_LIVE_SMOKE_DONE_TIMEOUT_MS", timeout),
		DisableRTPGap:  envBool("GO_LIVE_SMOKE_DISABLE_RTP_GAP", false),
		InterruptDelay: envOptionalDurationMS("GO_LIVE_SMOKE_INTERRUPT_DELAY_MS"),
		InterruptEvent: env("GO_LIVE_SMOKE_INTERRUPT_EVENT", "wake_interrupt"),
		TraceID: env(
			"GO_LIVE_SMOKE_TRACE_ID",
			fmt.Sprintf("turn-shadow-%d", time.Now().UnixNano()),
		),
		TurnCandidateEnabled: envBool("GO_LIVE_SMOKE_TURN_CANDIDATE_ENABLED", false),
		TurnCandidateActive:  envBool("GO_LIVE_SMOKE_TURN_CANDIDATE_ACTIVE", false),
		TurnCandidateSilenceMS: uint64(envDurationMS(
			"GO_LIVE_SMOKE_TURN_CANDIDATE_SILENCE_MS",
			300*time.Millisecond,
		).Milliseconds()),
		TurnCandidateDelay: envDurationMS(
			"GO_LIVE_SMOKE_TURN_CANDIDATE_DELAY_MS",
			300*time.Millisecond,
		),
		TurnCandidateAudioEndGap: envDurationMS("GO_LIVE_SMOKE_TURN_CANDIDATE_AUDIO_END_GAP_MS", 0),
		TurnCandidateResumeSplit: envPositiveInt("GO_LIVE_SMOKE_TURN_CANDIDATE_RESUME_SPLIT_PACKET", 0),
	}
	return cfg
}

func dialWebSocket(cfg config) (*websocket.Conn, error) {
	location, err := url.Parse(cfg.WebSocketURL)
	if err != nil {
		return nil, fmt.Errorf("parse GO_LIVE_SMOKE_WS_URL: %w", err)
	}
	wsCfg, err := websocket.NewConfig(location.String(), cfg.Origin)
	if err != nil {
		return nil, fmt.Errorf("create websocket config: %w", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), cfg.ConnectWait)
	defer cancel()
	return wsCfg.DialContext(ctx)
}

func createPeer(rtcCfg rtcConfigMessage) (*webrtc.PeerConnection, *webrtc.TrackLocalStaticRTP, *webrtc.DataChannel, *atomic.Uint64, error) {
	iceServers := make([]webrtc.ICEServer, 0, len(rtcCfg.ICEServers))
	for _, server := range rtcCfg.ICEServers {
		if len(server.URLs) == 0 {
			continue
		}
		iceServers = append(iceServers, webrtc.ICEServer{
			URLs:       append([]string(nil), server.URLs...),
			Username:   server.Username,
			Credential: server.Credential,
		})
	}
	api := webrtc.NewAPI()
	pc, err := api.NewPeerConnection(webrtc.Configuration{ICEServers: iceServers})
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("create peer connection: %w", err)
	}
	downlinkRTPPackets := &atomic.Uint64{}
	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		if track.Kind() != webrtc.RTPCodecTypeAudio {
			return
		}
		go func() {
			for {
				packet, _, err := track.ReadRTP()
				if err != nil {
					return
				}
				count := downlinkRTPPackets.Add(1)
				if count == 1 {
					fmt.Printf(
						"downlink_rtp_track id=%s stream=%s payload_type=%d\n",
						track.ID(),
						track.StreamID(),
						packet.PayloadType,
					)
				}
			}
		}()
	})
	track, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus},
		"microphone",
		"voice",
	)
	if err != nil {
		_ = pc.Close()
		return nil, nil, nil, nil, fmt.Errorf("create audio track: %w", err)
	}
	if _, err := pc.AddTrack(track); err != nil {
		_ = pc.Close()
		return nil, nil, nil, nil, fmt.Errorf("add audio track: %w", err)
	}
	dc, err := pc.CreateDataChannel("control", nil)
	if err != nil {
		_ = pc.Close()
		return nil, nil, nil, nil, fmt.Errorf("create data channel: %w", err)
	}
	return pc, track, dc, downlinkRTPPackets, nil
}

func receiveTyped[T any](conn *websocket.Conn, wantType string) (T, error) {
	var zero T
	for {
		var payload []byte
		if err := websocket.Message.Receive(conn, &payload); err != nil {
			return zero, fmt.Errorf("receive %s: %w", wantType, err)
		}
		var typed T
		if err := json.Unmarshal(payload, &typed); err != nil {
			return zero, fmt.Errorf("decode %s JSON: %w payload=%q", wantType, err, string(payload))
		}
		var generic genericJSONMessage
		if err := json.Unmarshal(payload, &generic); err != nil {
			return zero, fmt.Errorf("decode generic JSON: %w payload=%q", err, string(payload))
		}
		switch generic.Type {
		case wantType:
			return typed, nil
		case "rtc_ice_candidate":
			continue
		case "error":
			return zero, fmt.Errorf("gateway error code=%s message=%s", generic.Code, generic.Message)
		default:
			return zero, fmt.Errorf("unexpected message while waiting for %s: %s payload=%s", wantType, generic.Type, string(payload))
		}
	}
}

func receiveDownlinkUntilDone(
	conn *websocket.Conn,
	timeout time.Duration,
	requireDownlinkRTP bool,
	downlinkRTPPackets *atomic.Uint64,
	interruptDelay time.Duration,
	interruptEvent string,
) error {
	deadline := time.Now().Add(timeout)
	if err := conn.SetDeadline(deadline); err != nil {
		return err
	}
	interruptEnabled := interruptDelay > 0
	interruptSent := false
	playbackStarts := 0
	doneMessages := 0
	for {
		var payload []byte
		if err := websocket.Message.Receive(conn, &payload); err != nil {
			return fmt.Errorf("receive downlink: %w", err)
		}
		var generic genericJSONMessage
		if err := json.Unmarshal(payload, &generic); err == nil && generic.Type != "" {
			switch generic.Type {
			case "status":
				fmt.Printf("status=%s\n", firstNonEmpty(generic.Message, generic.Content, generic.Text))
			case "text":
				fmt.Printf("text=%s\n", firstNonEmpty(generic.Content, generic.Message, generic.Text))
			case "done":
				doneMessages++
				if interruptEnabled && playbackStarts < 2 {
					fmt.Printf(
						"done_before_interrupted_playback playback_starts=%d done_messages=%d\n",
						playbackStarts,
						doneMessages,
					)
					continue
				}
				rtpPackets := uint64(0)
				if downlinkRTPPackets != nil {
					rtpPackets = downlinkRTPPackets.Load()
				}
				if requireDownlinkRTP && rtpPackets == 0 {
					return errors.New("done received before any downlink RTP packet")
				}
				if rtpPackets > 0 {
					fmt.Printf("downlink_rtp_packets=%d\n", rtpPackets)
				}
				fmt.Printf(
					"done=true playback_starts=%d done_messages=%d interrupt_sent=%t\n",
					playbackStarts,
					doneMessages,
					interruptSent,
				)
				return nil
			case "playback_start":
				playbackStarts++
				fmt.Printf(
					"json_type=playback_start count=%d round_id=%s playback_id=%s rtp_packets=%d\n",
					playbackStarts,
					generic.RoundID,
					generic.PlaybackID,
					downlinkRTPPackets.Load(),
				)
				if interruptEnabled && !interruptSent {
					time.Sleep(interruptDelay)
					eventID := fmt.Sprintf("%s-%d", interruptEvent, time.Now().UnixNano())
					if err := websocket.JSON.Send(conn, map[string]any{
						"type":     "client_event",
						"event":    interruptEvent,
						"event_id": eventID,
						"source":   "go_live_smoke",
					}); err != nil {
						return fmt.Errorf("send interrupt client_event: %w", err)
					}
					interruptSent = true
					fmt.Printf(
						"interrupt_sent event=%s event_id=%s delay_ms=%d rtp_packets=%d\n",
						interruptEvent,
						eventID,
						interruptDelay.Milliseconds(),
						downlinkRTPPackets.Load(),
					)
				}
			case "rtc_ice_candidate":
				continue
			case "error":
				return fmt.Errorf("gateway error code=%s message=%s", generic.Code, generic.Message)
			default:
				fmt.Printf("json_type=%s\n", generic.Type)
			}
			continue
		}
		if strings.HasPrefix(string(payload), "VAF1") {
			fmt.Printf("binary_vaf1_bytes=%d\n", len(payload))
		} else {
			fmt.Printf("binary_bytes=%d\n", len(payload))
		}
	}
}

func expectsDownlinkRTP(rtcCfg rtcConfigMessage) bool {
	switch strings.ToLower(strings.TrimSpace(rtcCfg.Media.Audio.DownlinkTransport)) {
	case "webrtc_rtp", "webrtc_rtp_mirror":
		return true
	default:
		return false
	}
}

func loadPackets(path string) ([][]byte, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read packet file: %w", err)
	}
	var file packetFile
	if err := json.Unmarshal(raw, &file); err != nil {
		return nil, fmt.Errorf("parse packet file JSON: %w", err)
	}
	packets := make([][]byte, 0, len(file.Packets))
	for i, encoded := range file.Packets {
		packet, err := base64.StdEncoding.DecodeString(encoded)
		if err != nil {
			return nil, fmt.Errorf("decode packet %d: %w", i+1, err)
		}
		if len(packet) == 0 {
			return nil, fmt.Errorf("packet %d is empty", i+1)
		}
		packets = append(packets, packet)
	}
	return packets, nil
}

func jsonString(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return string(raw)
}

func closeChannelOnce(ch chan struct{}) {
	select {
	case <-ch:
	default:
		close(ch)
	}
}

func env(name, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func envBool(name string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	switch strings.ToLower(value) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func envDurationMS(name string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	ms, err := strconv.Atoi(value)
	if err != nil || ms <= 0 {
		return fallback
	}
	return time.Duration(ms) * time.Millisecond
}

func envOptionalDurationMS(name string) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return 0
	}
	ms, err := strconv.Atoi(value)
	if err != nil || ms <= 0 {
		return 0
	}
	return time.Duration(ms) * time.Millisecond
}

func envPositiveInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		return fallback
	}
	return parsed
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}
