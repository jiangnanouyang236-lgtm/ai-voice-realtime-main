package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/pion/webrtc/v4"
	"golang.org/x/net/websocket"
)

const (
	defaultWebSocketURL = "ws://127.0.0.1:8282/ws"
	defaultRobotID      = "companion_01"
	defaultClientType   = "go_rtc_route_probe"
	defaultTimeout      = 30 * time.Second
	defaultSampleCount  = 3
	defaultSampleGap    = 3 * time.Second

	iceRouteTURN         = "turn"
	iceRouteSTUNOrDirect = "stun_or_direct"
	iceRouteUnknown      = "unknown"
)

type config struct {
	WebSocketURL  string
	Origin        string
	RobotID       string
	RobotSecret   string
	ClientType    string
	Timeout       time.Duration
	ConnectWait   time.Duration
	SampleCount   int
	SampleGap     time.Duration
	JSONOutput    bool
	NoAudioTrack  bool
	ForceRelay    bool
	NoOrigin      bool
	PrintMessages bool
}

type connectedMessage struct {
	Type      string `json:"type"`
	SessionID string `json:"session_id"`
}

type registeredMessage struct {
	Type       string `json:"type"`
	SessionID  string `json:"session_id"`
	RobotID    string `json:"robot_id"`
	BotID      string `json:"bot_id"`
	BotName    string `json:"bot_name"`
	ClientType string `json:"client_type"`
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
	Codec             string `json:"codec"`
	SampleRate        uint32 `json:"sample_rate"`
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

type rtcIceCandidateMessage struct {
	Type          string  `json:"type"`
	SessionID     string  `json:"session_id"`
	Candidate     string  `json:"candidate"`
	SDPMid        *string `json:"sdp_mid,omitempty"`
	SDPMLineIndex *uint16 `json:"sdp_mline_index,omitempty"`
}

type genericJSONMessage struct {
	Type    string `json:"type"`
	Code    string `json:"code,omitempty"`
	Message string `json:"message,omitempty"`
}

type routeProbeResult struct {
	WebSocketURL     string                  `json:"websocket_url,omitempty"`
	Origin           string                  `json:"origin,omitempty"`
	OriginDisabled   bool                    `json:"origin_disabled,omitempty"`
	SessionID        string                  `json:"session_id"`
	RobotID          string                  `json:"robot_id"`
	BotID            string                  `json:"bot_id,omitempty"`
	IceState         string                  `json:"ice_state"`
	PeerState        string                  `json:"peer_state"`
	DataChannelOpen  bool                    `json:"datachannel_open"`
	ICEServerCount   int                     `json:"ice_server_count"`
	ForceRelay       bool                    `json:"force_relay"`
	Route            string                  `json:"route"`
	RouteDetail      string                  `json:"route_detail,omitempty"`
	SelectedPair     candidatePairSnapshot   `json:"selected_pair"`
	CandidateSummary candidateSummary        `json:"candidate_summary"`
	NATHint          natHint                 `json:"nat_hint"`
	Samples          []candidatePairSnapshot `json:"samples,omitempty"`
}

type candidatePairSnapshot struct {
	Route                  string  `json:"route"`
	Detail                 string  `json:"detail,omitempty"`
	PairID                 string  `json:"pair_id,omitempty"`
	PairState              string  `json:"pair_state,omitempty"`
	Nominated              bool    `json:"nominated"`
	LocalCandidateID       string  `json:"local_candidate_id,omitempty"`
	LocalCandidateType     string  `json:"local_candidate_type,omitempty"`
	LocalCandidateIP       string  `json:"local_candidate_ip,omitempty"`
	LocalCandidatePort     int32   `json:"local_candidate_port,omitempty"`
	LocalProtocol          string  `json:"local_candidate_protocol,omitempty"`
	LocalRelayProtocol     string  `json:"local_candidate_relay_protocol,omitempty"`
	LocalCandidateURL      string  `json:"local_candidate_url,omitempty"`
	RemoteCandidateID      string  `json:"remote_candidate_id,omitempty"`
	RemoteCandidateType    string  `json:"remote_candidate_type,omitempty"`
	RemoteCandidateIP      string  `json:"remote_candidate_ip,omitempty"`
	RemoteCandidatePort    int32   `json:"remote_candidate_port,omitempty"`
	RemoteProtocol         string  `json:"remote_candidate_protocol,omitempty"`
	RemoteRelayProtocol    string  `json:"remote_candidate_relay_protocol,omitempty"`
	RemoteCandidateURL     string  `json:"remote_candidate_url,omitempty"`
	CurrentRTTMS           float64 `json:"candidate_pair_rtt_ms,omitempty"`
	TotalRTTMS             float64 `json:"candidate_pair_total_rtt_ms,omitempty"`
	PacketsSent            uint32  `json:"candidate_pair_packets_sent,omitempty"`
	PacketsReceived        uint32  `json:"candidate_pair_packets_received,omitempty"`
	BytesSent              uint64  `json:"candidate_pair_bytes_sent,omitempty"`
	BytesReceived          uint64  `json:"candidate_pair_bytes_received,omitempty"`
	LocalCandidatePresent  bool    `json:"local_candidate_present"`
	RemoteCandidatePresent bool    `json:"remote_candidate_present"`
}

type candidateSummary struct {
	LocalByType       map[string]int `json:"local_by_type"`
	RemoteByType      map[string]int `json:"remote_by_type"`
	LocalHostIPs      []string       `json:"local_host_ips,omitempty"`
	LocalSrflxAddrs   []string       `json:"local_srflx_addrs,omitempty"`
	LocalRelayAddrs   []string       `json:"local_relay_addrs,omitempty"`
	RemoteSrflxAddrs  []string       `json:"remote_srflx_addrs,omitempty"`
	RemoteRelayAddrs  []string       `json:"remote_relay_addrs,omitempty"`
	HasPrivateHostIP  bool           `json:"has_private_host_ip"`
	HasPublicHostIP   bool           `json:"has_public_host_ip"`
	HasLocalSrflx     bool           `json:"has_local_srflx"`
	HasLocalRelay     bool           `json:"has_local_relay"`
	HasRemoteRelay    bool           `json:"has_remote_relay"`
	HasRemoteSrflx    bool           `json:"has_remote_srflx"`
	UniqueSrflxIPOnly []string       `json:"unique_srflx_ip_only,omitempty"`
}

type natHint struct {
	Category   string `json:"category"`
	Confidence string `json:"confidence"`
	Detail     string `json:"detail"`
}

func main() {
	cfg := parseConfig()
	result, err := run(cfg)
	if err != nil {
		if cfg.JSONOutput {
			_ = json.NewEncoder(os.Stdout).Encode(map[string]any{
				"ok":    false,
				"error": err.Error(),
			})
		}
		fmt.Fprintf(os.Stderr, "rtc_route_probe error: %v\n", err)
		os.Exit(1)
	}
	if cfg.JSONOutput {
		encoder := json.NewEncoder(os.Stdout)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(result); err != nil {
			fmt.Fprintf(os.Stderr, "encode JSON result: %v\n", err)
			os.Exit(1)
		}
		return
	}
	printTextResult(result)
}

func parseConfig() config {
	cfg := config{
		WebSocketURL: env("RTC_ROUTE_PROBE_WS_URL", defaultWebSocketURL),
		RobotID:      env("RTC_ROUTE_PROBE_ROBOT_ID", defaultRobotID),
		RobotSecret:  os.Getenv("RTC_ROUTE_PROBE_ROBOT_SECRET"),
		ClientType:   env("RTC_ROUTE_PROBE_CLIENT_TYPE", defaultClientType),
		Timeout:      envDuration("RTC_ROUTE_PROBE_TIMEOUT", defaultTimeout),
		ConnectWait:  envDuration("RTC_ROUTE_PROBE_CONNECT_TIMEOUT", defaultTimeout),
		SampleCount:  envInt("RTC_ROUTE_PROBE_SAMPLES", defaultSampleCount),
		SampleGap:    envDuration("RTC_ROUTE_PROBE_SAMPLE_INTERVAL", defaultSampleGap),
		JSONOutput:   envBool("RTC_ROUTE_PROBE_JSON", false),
		NoAudioTrack: envBool("RTC_ROUTE_PROBE_NO_AUDIO_TRACK", false),
		ForceRelay:   envBool("RTC_ROUTE_PROBE_FORCE_RELAY", false),
		NoOrigin:     envBool("RTC_ROUTE_PROBE_NO_ORIGIN", false),
	}
	cfg.Origin = env("RTC_ROUTE_PROBE_ORIGIN", defaultOriginForWS(cfg.WebSocketURL))

	flag.StringVar(&cfg.WebSocketURL, "ws-url", cfg.WebSocketURL, "Go Gateway WebSocket URL")
	flag.StringVar(&cfg.Origin, "origin", cfg.Origin, "WebSocket Origin header")
	flag.StringVar(&cfg.RobotID, "robot-id", cfg.RobotID, "Robot ID used for gateway registration")
	flag.StringVar(&cfg.RobotSecret, "robot-secret", cfg.RobotSecret, "Robot secret used for gateway registration")
	flag.StringVar(&cfg.ClientType, "client-type", cfg.ClientType, "Client type reported to gateway")
	flag.DurationVar(&cfg.Timeout, "timeout", cfg.Timeout, "Overall probe timeout")
	flag.DurationVar(&cfg.ConnectWait, "connect-timeout", cfg.ConnectWait, "WebSocket/WebRTC connection timeout")
	flag.IntVar(&cfg.SampleCount, "samples", cfg.SampleCount, "Number of selected candidate-pair samples; 0 means sample until timeout")
	flag.DurationVar(&cfg.SampleGap, "sample-interval", cfg.SampleGap, "Interval between selected candidate-pair samples")
	flag.BoolVar(&cfg.JSONOutput, "json", cfg.JSONOutput, "Print JSON output")
	flag.BoolVar(&cfg.NoAudioTrack, "no-audio-track", cfg.NoAudioTrack, "Create a DataChannel-only offer")
	flag.BoolVar(&cfg.ForceRelay, "force-relay", cfg.ForceRelay, "Force ICETransportPolicyRelay to test TURN-only path")
	flag.BoolVar(&cfg.NoOrigin, "no-origin", cfg.NoOrigin, "Omit the WebSocket Origin header")
	flag.BoolVar(&cfg.PrintMessages, "print-messages", cfg.PrintMessages, "Print unexpected gateway JSON message types while probing")
	flag.Parse()
	if cfg.SampleCount < 0 {
		cfg.SampleCount = 0
	}
	if cfg.SampleGap <= 0 {
		cfg.SampleGap = defaultSampleGap
	}
	if cfg.ConnectWait <= 0 {
		cfg.ConnectWait = cfg.Timeout
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = defaultTimeout
	}
	return cfg
}

func run(cfg config) (routeProbeResult, error) {
	deadline := time.Now().Add(cfg.Timeout)
	conn, err := dialWebSocket(cfg)
	if err != nil {
		return routeProbeResult{}, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(deadline)

	connected, err := receiveTyped[connectedMessage](conn, "connected", cfg.PrintMessages)
	if err != nil {
		return routeProbeResult{}, err
	}
	if connected.SessionID == "" {
		return routeProbeResult{}, fmt.Errorf("connected missing session_id: %+v", connected)
	}

	registerPayload := map[string]any{
		"type":        "register",
		"robot_id":    cfg.RobotID,
		"client_type": cfg.ClientType,
	}
	if strings.TrimSpace(cfg.RobotSecret) != "" {
		registerPayload["robot_secret"] = cfg.RobotSecret
	}
	if err := websocket.JSON.Send(conn, registerPayload); err != nil {
		return routeProbeResult{}, fmt.Errorf("send register: %w", err)
	}
	registered, err := receiveTyped[registeredMessage](conn, "registered", cfg.PrintMessages)
	if err != nil {
		return routeProbeResult{}, err
	}
	if registered.SessionID != connected.SessionID {
		return routeProbeResult{}, fmt.Errorf("registered session mismatch: connected=%s registered=%s", connected.SessionID, registered.SessionID)
	}

	rtcCfg, err := receiveTyped[rtcConfigMessage](conn, "rtc_config", cfg.PrintMessages)
	if err != nil {
		return routeProbeResult{}, err
	}
	if rtcCfg.SessionID != connected.SessionID {
		return routeProbeResult{}, fmt.Errorf("rtc_config session mismatch: connected=%s rtc_config=%s", connected.SessionID, rtcCfg.SessionID)
	}

	pc, dc, err := createPeer(rtcCfg, cfg)
	if err != nil {
		return routeProbeResult{}, err
	}
	defer pc.Close()

	iceStateCh := make(chan webrtc.ICEConnectionState, 8)
	peerStateCh := make(chan webrtc.PeerConnectionState, 8)
	dataChannelOpenCh := make(chan struct{})
	failedCh := make(chan error, 1)
	pc.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		select {
		case iceStateCh <- state:
		default:
		}
		if state == webrtc.ICEConnectionStateFailed {
			select {
			case failedCh <- errors.New("ICE state failed"):
			default:
			}
		}
	})
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		select {
		case peerStateCh <- state:
		default:
		}
		if state == webrtc.PeerConnectionStateFailed {
			select {
			case failedCh <- errors.New("peer connection failed"):
			default:
			}
		}
	})
	dc.OnOpen(func() {
		closeChannelOnce(dataChannelOpenCh)
	})

	offer, err := pc.CreateOffer(nil)
	if err != nil {
		return routeProbeResult{}, fmt.Errorf("create offer: %w", err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		return routeProbeResult{}, fmt.Errorf("set local offer: %w", err)
	}
	select {
	case <-gatherComplete:
	case <-time.After(time.Until(deadline)):
		return routeProbeResult{}, errors.New("timeout waiting for local ICE gathering")
	}
	local := pc.LocalDescription()
	if local == nil {
		return routeProbeResult{}, errors.New("missing local description")
	}
	if err := websocket.JSON.Send(conn, map[string]any{
		"type":       "rtc_offer",
		"session_id": connected.SessionID,
		"sdp":        local.SDP,
	}); err != nil {
		return routeProbeResult{}, fmt.Errorf("send rtc_offer: %w", err)
	}

	var pendingCandidates []rtcIceCandidateMessage
	answer, err := receiveTypedWithCandidates[rtcAnswerMessage](conn, "rtc_answer", cfg.PrintMessages, func(candidate rtcIceCandidateMessage) {
		if candidate.SessionID == connected.SessionID {
			pendingCandidates = append(pendingCandidates, candidate)
		}
	})
	if err != nil {
		return routeProbeResult{}, err
	}
	if answer.SessionID != connected.SessionID {
		return routeProbeResult{}, fmt.Errorf("rtc_answer session mismatch: connected=%s rtc_answer=%s", connected.SessionID, answer.SessionID)
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{
		Type: webrtc.SDPTypeAnswer,
		SDP:  answer.SDP,
	}); err != nil {
		return routeProbeResult{}, fmt.Errorf("set remote answer: %w", err)
	}
	for _, candidate := range pendingCandidates {
		if err := addRemoteCandidate(pc, connected.SessionID, candidate); err != nil {
			return routeProbeResult{}, err
		}
	}

	candidatePumpCtx, cancelCandidatePump := context.WithCancel(context.Background())
	defer cancelCandidatePump()
	candidateErrCh := make(chan error, 1)
	go pumpRemoteCandidates(candidatePumpCtx, conn, pc, connected.SessionID, cfg.PrintMessages, candidateErrCh)

	connectedState, peerState, dataOpen, err := waitConnected(pc, dataChannelOpenCh, iceStateCh, peerStateCh, failedCh, candidateErrCh, deadline)
	if err != nil {
		return routeProbeResult{}, err
	}
	_ = conn.SetDeadline(time.Time{})

	samples, err := sampleSelectedPairs(pc, cfg, deadline)
	if err != nil {
		return routeProbeResult{}, err
	}
	if len(samples) == 0 {
		return routeProbeResult{}, errors.New("selected ICE candidate pair unavailable")
	}
	selected := samples[len(samples)-1]
	summary := summarizeCandidates(pc.GetStats())
	return routeProbeResult{
		WebSocketURL:     cfg.WebSocketURL,
		Origin:           cfg.Origin,
		OriginDisabled:   cfg.NoOrigin,
		SessionID:        connected.SessionID,
		RobotID:          firstNonEmpty(registered.RobotID, cfg.RobotID),
		BotID:            registered.BotID,
		IceState:         connectedState.String(),
		PeerState:        peerState.String(),
		DataChannelOpen:  dataOpen,
		ICEServerCount:   len(rtcCfg.ICEServers),
		ForceRelay:       cfg.ForceRelay,
		Route:            selected.Route,
		RouteDetail:      selected.Detail,
		SelectedPair:     selected,
		CandidateSummary: summary,
		NATHint:          inferNATHint(summary, selected),
		Samples:          samples,
	}, nil
}

func dialWebSocket(cfg config) (*websocket.Conn, error) {
	location, err := url.Parse(cfg.WebSocketURL)
	if err != nil {
		return nil, fmt.Errorf("parse ws-url: %w", err)
	}
	wsCfg, err := websocket.NewConfig(location.String(), cfg.Origin)
	if err != nil {
		return nil, fmt.Errorf("create websocket config: %w", err)
	}
	if cfg.NoOrigin {
		wsCfg.Origin = nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), cfg.ConnectWait)
	defer cancel()
	return wsCfg.DialContext(ctx)
}

func createPeer(rtcCfg rtcConfigMessage, cfg config) (*webrtc.PeerConnection, *webrtc.DataChannel, error) {
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
	configuration := webrtc.Configuration{ICEServers: iceServers}
	if cfg.ForceRelay {
		configuration.ICETransportPolicy = webrtc.ICETransportPolicyRelay
	}
	api := webrtc.NewAPI()
	pc, err := api.NewPeerConnection(configuration)
	if err != nil {
		return nil, nil, fmt.Errorf("create peer connection: %w", err)
	}
	if !cfg.NoAudioTrack {
		track, err := webrtc.NewTrackLocalStaticRTP(
			webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus},
			"route-probe",
			"probe",
		)
		if err != nil {
			_ = pc.Close()
			return nil, nil, fmt.Errorf("create audio track: %w", err)
		}
		if _, err := pc.AddTrack(track); err != nil {
			_ = pc.Close()
			return nil, nil, fmt.Errorf("add audio track: %w", err)
		}
	}
	dc, err := pc.CreateDataChannel("control", nil)
	if err != nil {
		_ = pc.Close()
		return nil, nil, fmt.Errorf("create data channel: %w", err)
	}
	return pc, dc, nil
}

func waitConnected(
	pc *webrtc.PeerConnection,
	dataChannelOpenCh <-chan struct{},
	iceStateCh <-chan webrtc.ICEConnectionState,
	peerStateCh <-chan webrtc.PeerConnectionState,
	failedCh <-chan error,
	candidateErrCh <-chan error,
	deadline time.Time,
) (webrtc.ICEConnectionState, webrtc.PeerConnectionState, bool, error) {
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	iceState := pc.ICEConnectionState()
	peerState := pc.ConnectionState()
	dataOpen := false
	for {
		if (iceState == webrtc.ICEConnectionStateConnected || iceState == webrtc.ICEConnectionStateCompleted) && dataOpen {
			return iceState, peerState, dataOpen, nil
		}
		select {
		case state := <-iceStateCh:
			iceState = state
		case state := <-peerStateCh:
			peerState = state
		case <-dataChannelOpenCh:
			dataOpen = true
		case err := <-failedCh:
			return iceState, peerState, dataOpen, err
		case err := <-candidateErrCh:
			if err != nil {
				return iceState, peerState, dataOpen, err
			}
		case <-timer.C:
			return iceState, peerState, dataOpen, fmt.Errorf("timeout waiting for WebRTC connection: ice=%s peer=%s datachannel_open=%t", iceState, peerState, dataOpen)
		}
	}
}

func pumpRemoteCandidates(
	ctx context.Context,
	conn *websocket.Conn,
	pc *webrtc.PeerConnection,
	sessionID string,
	printMessages bool,
	errCh chan<- error,
) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		var payload []byte
		if err := websocket.Message.Receive(conn, &payload); err != nil {
			select {
			case <-ctx.Done():
			case errCh <- fmt.Errorf("receive gateway message after rtc_answer: %w", err):
			default:
			}
			return
		}
		var generic genericJSONMessage
		if err := json.Unmarshal(payload, &generic); err != nil {
			select {
			case errCh <- fmt.Errorf("decode gateway message after rtc_answer: %w payload=%q", err, string(payload)):
			default:
			}
			return
		}
		switch generic.Type {
		case "rtc_ice_candidate":
			var candidate rtcIceCandidateMessage
			if err := json.Unmarshal(payload, &candidate); err != nil {
				select {
				case errCh <- fmt.Errorf("decode rtc_ice_candidate JSON: %w payload=%q", err, string(payload)):
				default:
				}
				return
			}
			if err := addRemoteCandidate(pc, sessionID, candidate); err != nil {
				select {
				case errCh <- err:
				default:
				}
				return
			}
		case "error":
			select {
			case errCh <- fmt.Errorf("gateway error code=%s message=%s", generic.Code, generic.Message):
			default:
			}
			return
		default:
			if printMessages {
				fmt.Fprintf(os.Stderr, "ignored gateway message after rtc_answer: type=%s payload=%s\n", generic.Type, string(payload))
			}
		}
	}
}

func addRemoteCandidate(pc *webrtc.PeerConnection, sessionID string, candidate rtcIceCandidateMessage) error {
	if candidate.SessionID != "" && candidate.SessionID != sessionID {
		return fmt.Errorf("rtc_ice_candidate session mismatch: connected=%s candidate=%s", sessionID, candidate.SessionID)
	}
	if strings.TrimSpace(candidate.Candidate) == "" {
		return nil
	}
	if err := pc.AddICECandidate(webrtc.ICECandidateInit{
		Candidate:     candidate.Candidate,
		SDPMid:        candidate.SDPMid,
		SDPMLineIndex: candidate.SDPMLineIndex,
	}); err != nil {
		return fmt.Errorf("add remote ICE candidate: %w", err)
	}
	return nil
}

func sampleSelectedPairs(pc *webrtc.PeerConnection, cfg config, deadline time.Time) ([]candidatePairSnapshot, error) {
	var samples []candidatePairSnapshot
	sampleLimit := cfg.SampleCount
	if sampleLimit == 0 {
		sampleLimit = int(time.Until(deadline) / cfg.SampleGap)
		if sampleLimit < 1 {
			sampleLimit = 1
		}
	}
	for i := 0; i < sampleLimit; i++ {
		snapshot, ok := selectedCandidateRouteFromStats(pc.GetStats())
		if ok {
			samples = append(samples, snapshot)
		}
		if i == sampleLimit-1 {
			break
		}
		if wait := cfg.SampleGap; time.Now().Add(wait).Before(deadline) {
			time.Sleep(wait)
		} else {
			break
		}
	}
	return samples, nil
}

func selectedCandidateRouteFromStats(report webrtc.StatsReport) (candidatePairSnapshot, bool) {
	if len(report) == 0 {
		return candidatePairSnapshot{}, false
	}
	candidates := make(map[string]webrtc.ICECandidateStats)
	for _, stat := range report {
		if candidate, ok := asICECandidateStats(stat); ok {
			candidates[candidate.ID] = candidate
		}
	}
	pair, ok := selectedCandidatePairStats(report)
	if !ok {
		return candidatePairSnapshot{}, false
	}
	local, localOK := candidates[pair.LocalCandidateID]
	remote, remoteOK := candidates[pair.RemoteCandidateID]
	snapshot := candidatePairSnapshot{
		Route:                  iceRouteUnknown,
		PairID:                 pair.ID,
		PairState:              string(pair.State),
		Nominated:              pair.Nominated,
		LocalCandidateID:       pair.LocalCandidateID,
		RemoteCandidateID:      pair.RemoteCandidateID,
		CurrentRTTMS:           pair.CurrentRoundTripTime * 1000.0,
		TotalRTTMS:             pair.TotalRoundTripTime * 1000.0,
		PacketsSent:            pair.PacketsSent,
		PacketsReceived:        pair.PacketsReceived,
		BytesSent:              pair.BytesSent,
		BytesReceived:          pair.BytesReceived,
		LocalCandidatePresent:  localOK,
		RemoteCandidatePresent: remoteOK,
	}
	if localOK {
		snapshot.LocalCandidateType = local.CandidateType.String()
		snapshot.LocalCandidateIP = local.IP
		snapshot.LocalCandidatePort = local.Port
		snapshot.LocalProtocol = local.Protocol
		snapshot.LocalRelayProtocol = local.RelayProtocol
		snapshot.LocalCandidateURL = local.URL
	}
	if remoteOK {
		snapshot.RemoteCandidateType = remote.CandidateType.String()
		snapshot.RemoteCandidateIP = remote.IP
		snapshot.RemoteCandidatePort = remote.Port
		snapshot.RemoteProtocol = remote.Protocol
		snapshot.RemoteRelayProtocol = remote.RelayProtocol
		snapshot.RemoteCandidateURL = remote.URL
	}
	snapshot.Detail = strings.Trim(
		fmt.Sprintf("%s-%s", printable(snapshot.LocalCandidateType), printable(snapshot.RemoteCandidateType)),
		"-",
	)
	snapshot.Route = classifyICERoute(snapshot.LocalCandidateType, snapshot.RemoteCandidateType)
	return snapshot, true
}

func selectedCandidatePairStats(report webrtc.StatsReport) (webrtc.ICECandidatePairStats, bool) {
	for _, stat := range report {
		transport, ok := asTransportStats(stat)
		if !ok || strings.TrimSpace(transport.SelectedCandidatePairID) == "" {
			continue
		}
		if pairStat, exists := report[transport.SelectedCandidatePairID]; exists {
			if pair, ok := asICECandidatePairStats(pairStat); ok {
				return pair, true
			}
		}
	}
	var fallback webrtc.ICECandidatePairStats
	found := false
	for _, stat := range report {
		pair, ok := asICECandidatePairStats(stat)
		if !ok {
			continue
		}
		if pair.Nominated && pair.State == webrtc.StatsICECandidatePairStateSucceeded {
			if !found || pair.BytesSent+pair.BytesReceived > fallback.BytesSent+fallback.BytesReceived {
				fallback = pair
				found = true
			}
		}
	}
	return fallback, found
}

func classifyICERoute(localType, remoteType string) string {
	localType = strings.ToLower(strings.TrimSpace(localType))
	remoteType = strings.ToLower(strings.TrimSpace(remoteType))
	if localType == "relay" || remoteType == "relay" {
		return iceRouteTURN
	}
	if localType == "" && remoteType == "" {
		return iceRouteUnknown
	}
	return iceRouteSTUNOrDirect
}

func summarizeCandidates(report webrtc.StatsReport) candidateSummary {
	summary := candidateSummary{
		LocalByType:  make(map[string]int),
		RemoteByType: make(map[string]int),
	}
	for _, stat := range report {
		candidate, ok := asICECandidateStats(stat)
		if !ok {
			continue
		}
		candidateType := candidate.CandidateType.String()
		endpoint := candidateEndpoint(candidate.IP, candidate.Port)
		isLocal := candidate.Type == webrtc.StatsTypeLocalCandidate
		if isLocal {
			summary.LocalByType[candidateType]++
			switch candidateType {
			case "host":
				summary.LocalHostIPs = appendUnique(summary.LocalHostIPs, candidate.IP)
				if isPrivateOrLoopbackIP(candidate.IP) {
					summary.HasPrivateHostIP = true
				} else if candidate.IP != "" {
					summary.HasPublicHostIP = true
				}
			case "srflx":
				summary.HasLocalSrflx = true
				summary.LocalSrflxAddrs = appendUnique(summary.LocalSrflxAddrs, endpoint)
				summary.UniqueSrflxIPOnly = appendUnique(summary.UniqueSrflxIPOnly, candidate.IP)
			case "relay":
				summary.HasLocalRelay = true
				summary.LocalRelayAddrs = appendUnique(summary.LocalRelayAddrs, endpoint)
			}
			continue
		}
		summary.RemoteByType[candidateType]++
		switch candidateType {
		case "srflx":
			summary.HasRemoteSrflx = true
			summary.RemoteSrflxAddrs = appendUnique(summary.RemoteSrflxAddrs, endpoint)
		case "relay":
			summary.HasRemoteRelay = true
			summary.RemoteRelayAddrs = appendUnique(summary.RemoteRelayAddrs, endpoint)
		}
	}
	sort.Strings(summary.LocalHostIPs)
	sort.Strings(summary.LocalSrflxAddrs)
	sort.Strings(summary.LocalRelayAddrs)
	sort.Strings(summary.RemoteSrflxAddrs)
	sort.Strings(summary.RemoteRelayAddrs)
	sort.Strings(summary.UniqueSrflxIPOnly)
	return summary
}

func inferNATHint(summary candidateSummary, selected candidatePairSnapshot) natHint {
	if selected.Route == iceRouteTURN {
		return natHint{
			Category:   "turn_relayed",
			Confidence: "medium",
			Detail:     "Selected ICE pair uses a relay candidate. Direct UDP either failed, was not reachable, or relay was forced/preferred.",
		}
	}
	if summary.HasLocalSrflx && summary.HasPrivateHostIP {
		return natHint{
			Category:   "behind_nat",
			Confidence: "medium",
			Detail:     "Local host candidates include private IPs and STUN produced server-reflexive public mappings.",
		}
	}
	if summary.HasLocalSrflx && !summary.HasPrivateHostIP {
		return natHint{
			Category:   "public_or_1to1_nat",
			Confidence: "low",
			Detail:     "STUN produced server-reflexive candidates, but no private local host candidate was observed.",
		}
	}
	if summary.HasPublicHostIP && !summary.HasLocalSrflx {
		return natHint{
			Category:   "public_ip_or_no_stun",
			Confidence: "low",
			Detail:     "Only public-looking host candidates were observed. NAT type cannot be determined from this probe alone.",
		}
	}
	if summary.HasPrivateHostIP && !summary.HasLocalSrflx {
		return natHint{
			Category:   "private_network_no_srflx",
			Confidence: "low",
			Detail:     "Private host candidates were observed, but no server-reflexive candidate appeared. STUN may be unavailable or blocked.",
		}
	}
	return natHint{
		Category:   "unknown",
		Confidence: "low",
		Detail:     "Candidate stats are insufficient for NAT inference. Symmetric/full-cone NAT requires dedicated multi-STUN probing.",
	}
}

func receiveTyped[T any](conn *websocket.Conn, wantType string, printMessages bool) (T, error) {
	return receiveTypedWithCandidates[T](conn, wantType, printMessages, nil)
}

func receiveTypedWithCandidates[T any](
	conn *websocket.Conn,
	wantType string,
	printMessages bool,
	candidateHandler func(rtcIceCandidateMessage),
) (T, error) {
	var zero T
	for {
		var payload []byte
		if err := websocket.Message.Receive(conn, &payload); err != nil {
			return zero, fmt.Errorf("receive %s: %w", wantType, err)
		}
		var generic genericJSONMessage
		if err := json.Unmarshal(payload, &generic); err != nil {
			return zero, fmt.Errorf("decode generic JSON: %w payload=%q", err, string(payload))
		}
		switch generic.Type {
		case wantType:
			var typed T
			if err := json.Unmarshal(payload, &typed); err != nil {
				return zero, fmt.Errorf("decode %s JSON: %w payload=%q", wantType, err, string(payload))
			}
			return typed, nil
		case "rtc_ice_candidate":
			if candidateHandler != nil {
				var candidate rtcIceCandidateMessage
				if err := json.Unmarshal(payload, &candidate); err != nil {
					return zero, fmt.Errorf("decode rtc_ice_candidate JSON: %w payload=%q", err, string(payload))
				}
				candidateHandler(candidate)
			}
			continue
		case "error":
			return zero, fmt.Errorf("gateway error code=%s message=%s", generic.Code, generic.Message)
		default:
			if printMessages {
				fmt.Fprintf(os.Stderr, "ignored gateway message while waiting for %s: type=%s payload=%s\n", wantType, generic.Type, string(payload))
			}
		}
	}
}

func asTransportStats(stat webrtc.Stats) (webrtc.TransportStats, bool) {
	switch typed := stat.(type) {
	case webrtc.TransportStats:
		return typed, true
	case *webrtc.TransportStats:
		if typed != nil {
			return *typed, true
		}
	}
	return webrtc.TransportStats{}, false
}

func asICECandidatePairStats(stat webrtc.Stats) (webrtc.ICECandidatePairStats, bool) {
	switch typed := stat.(type) {
	case webrtc.ICECandidatePairStats:
		return typed, true
	case *webrtc.ICECandidatePairStats:
		if typed != nil {
			return *typed, true
		}
	}
	return webrtc.ICECandidatePairStats{}, false
}

func asICECandidateStats(stat webrtc.Stats) (webrtc.ICECandidateStats, bool) {
	switch typed := stat.(type) {
	case webrtc.ICECandidateStats:
		return typed, true
	case *webrtc.ICECandidateStats:
		if typed != nil {
			return *typed, true
		}
	}
	return webrtc.ICECandidateStats{}, false
}

func printTextResult(result routeProbeResult) {
	writeTextResult(os.Stdout, result)
}

func writeTextResult(w io.Writer, result routeProbeResult) {
	fmt.Fprintln(w, "RTC Route Probe")
	fmt.Fprintln(w, "===============")
	fmt.Fprintf(w, "Result: %s\n", routeHeadline(result.Route))
	fmt.Fprintf(w, "Route : %s (%s)\n", result.Route, printable(result.RouteDetail))
	fmt.Fprintf(w, "Note  : %s\n\n", routeSummary(result))

	fmt.Fprintln(w, "Session")
	writeKV(w, "session_id", result.SessionID)
	writeKV(w, "robot", fmt.Sprintf("%s / bot=%s", result.RobotID, printable(result.BotID)))
	writeKV(w, "ice", fmt.Sprintf("%s / peer=%s / datachannel_open=%t", result.IceState, result.PeerState, result.DataChannelOpen))
	writeKV(w, "ice_servers", fmt.Sprintf("%d / force_relay=%t", result.ICEServerCount, result.ForceRelay))
	fmt.Fprintln(w)

	fmt.Fprintln(w, "Signaling")
	writeKV(w, "ws_url", printable(result.WebSocketURL))
	if result.OriginDisabled {
		writeKV(w, "origin", "disabled (--no-origin)")
	} else {
		writeKV(w, "origin", printable(result.Origin))
	}
	fmt.Fprintln(w)

	fmt.Fprintln(w, "Selected ICE Pair")
	writeKV(w, "local", formatCandidateEndpoint(
		result.SelectedPair.LocalCandidateType,
		result.SelectedPair.LocalCandidateIP,
		result.SelectedPair.LocalCandidatePort,
		result.SelectedPair.LocalProtocol,
	))
	writeKV(w, "remote", formatCandidateEndpoint(
		result.SelectedPair.RemoteCandidateType,
		result.SelectedPair.RemoteCandidateIP,
		result.SelectedPair.RemoteCandidatePort,
		result.SelectedPair.RemoteProtocol,
	))
	writeKV(w, "pair", fmt.Sprintf("state=%s nominated=%t id=%s", printable(result.SelectedPair.PairState), result.SelectedPair.Nominated, printable(result.SelectedPair.PairID)))
	writeKV(w, "traffic", fmt.Sprintf("rtt=%.1fms bytes_sent=%d bytes_received=%d packets_sent=%d packets_received=%d",
		result.SelectedPair.CurrentRTTMS,
		result.SelectedPair.BytesSent,
		result.SelectedPair.BytesReceived,
		result.SelectedPair.PacketsSent,
		result.SelectedPair.PacketsReceived,
	))
	fmt.Fprintln(w)

	fmt.Fprintln(w, "Candidates")
	writeKV(w, "local_counts", formatTypeCounts(result.CandidateSummary.LocalByType))
	writeKV(w, "remote_counts", formatTypeCounts(result.CandidateSummary.RemoteByType))
	writeKV(w, "local_host_ips", joinOrDash(result.CandidateSummary.LocalHostIPs))
	writeKV(w, "local_srflx", joinOrDash(result.CandidateSummary.LocalSrflxAddrs))
	writeKV(w, "local_relay", joinOrDash(result.CandidateSummary.LocalRelayAddrs))
	writeKV(w, "remote_srflx", joinOrDash(result.CandidateSummary.RemoteSrflxAddrs))
	writeKV(w, "remote_relay", joinOrDash(result.CandidateSummary.RemoteRelayAddrs))
	fmt.Fprintln(w)

	fmt.Fprintln(w, "Diagnosis")
	writeKV(w, "nat_hint", fmt.Sprintf("%s / confidence=%s", result.NATHint.Category, result.NATHint.Confidence))
	writeKV(w, "nat_detail", result.NATHint.Detail)
	writeKV(w, "route_detail", routeDiagnosis(result))
	writeKV(w, "next_check", nextCheck(result))

	if len(result.Samples) > 1 {
		fmt.Fprintln(w)
		fmt.Fprintln(w, "Samples")
		for i, sample := range result.Samples {
			fmt.Fprintf(
				w,
				"  %02d. route=%s detail=%s local=%s remote=%s rtt=%.1fms\n",
				i+1,
				sample.Route,
				printable(sample.Detail),
				formatCandidateEndpoint(sample.LocalCandidateType, sample.LocalCandidateIP, sample.LocalCandidatePort, sample.LocalProtocol),
				formatCandidateEndpoint(sample.RemoteCandidateType, sample.RemoteCandidateIP, sample.RemoteCandidatePort, sample.RemoteProtocol),
				sample.CurrentRTTMS,
			)
		}
	}
}

func writeKV(w io.Writer, key, value string) {
	fmt.Fprintf(w, "  %-14s %s\n", key+":", printable(value))
}

func routeHeadline(route string) string {
	switch route {
	case iceRouteTURN:
		return "TURN relay selected"
	case iceRouteSTUNOrDirect:
		return "STUN/direct selected"
	default:
		return "Route unknown"
	}
}

func routeSummary(result routeProbeResult) string {
	switch result.Route {
	case iceRouteTURN:
		if result.ForceRelay {
			return "本次强制 relay，因此当前结果用于验证 TURN 可用性，不代表直连失败。"
		}
		return "当前选中的 ICE pair 包含 relay candidate，实际媒体路径经过 TURN 中继。"
	case iceRouteSTUNOrDirect:
		return "当前选中的 ICE pair 未包含 relay candidate，说明本次没有走 TURN 中继。"
	default:
		return "未能从 Pion stats 中稳定识别 selected ICE pair，请结合 JSON 和 Gateway 日志继续看。"
	}
}

func routeDiagnosis(result routeProbeResult) string {
	localType := strings.ToLower(strings.TrimSpace(result.SelectedPair.LocalCandidateType))
	remoteType := strings.ToLower(strings.TrimSpace(result.SelectedPair.RemoteCandidateType))
	if result.Route == iceRouteTURN {
		if result.ForceRelay {
			return "ICETransportPolicy=relay，本次按预期选择 TURN。"
		}
		if remoteType == "relay" && result.CandidateSummary.HasLocalSrflx {
			return "客户端 STUN 已拿到 srflx，但最终选择 remote relay；常见原因是 Gateway 侧公网 UDP 直达不可用或未映射到 Go Gateway。"
		}
		if localType == "relay" {
			return "本地侧选择 relay；常见原因是客户端当前网络无法对外建立可用 UDP 直连。"
		}
		return "selected ICE pair 中存在 relay candidate，直连候选没有成为最终 nominated pair。"
	}
	if result.Route == iceRouteSTUNOrDirect {
		if localType == "srflx" || remoteType == "srflx" || localType == "prflx" || remoteType == "prflx" {
			return "selected ICE pair 使用 STUN/reflexive 路径，当前网络具备 UDP 打洞可用性。"
		}
		return "selected ICE pair 未经过 relay，可能是 host 直连或同网段路径。"
	}
	return "selected ICE candidate 类型不完整，无法判断是否经过 relay。"
}

func nextCheck(result routeProbeResult) string {
	if result.Route == iceRouteTURN && !result.ForceRelay {
		return "若期望 STUN/direct，优先检查 Go Gateway 所在公网 UDP 端口段是否映射到业务机，并确认容器使用 host network 或正确暴露 UDP。"
	}
	if result.Route == iceRouteSTUNOrDirect {
		return "可继续用 --samples 120 --sample-interval 5s 观察长时间是否稳定保持非 relay。"
	}
	return "用 --json 输出完整 stats，同时查看 Go Gateway 的 ice_route 日志。"
}

func formatCandidateEndpoint(candidateType, ip string, port int32, protocol string) string {
	endpoint := candidateEndpoint(ip, port)
	parts := []string{printable(candidateType), printable(endpoint)}
	if strings.TrimSpace(protocol) != "" {
		parts = append(parts, strings.TrimSpace(protocol))
	}
	return strings.Join(parts, " / ")
}

func joinOrDash(items []string) string {
	if len(items) == 0 {
		return "-"
	}
	return strings.Join(items, ", ")
}

func formatTypeCounts(counts map[string]int) string {
	if len(counts) == 0 {
		return "-"
	}
	keys := make([]string, 0, len(counts))
	for key := range counts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, key := range keys {
		parts = append(parts, fmt.Sprintf("%s:%d", key, counts[key]))
	}
	return strings.Join(parts, ",")
}

func defaultOriginForWS(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" {
		return "http://127.0.0.1:8282"
	}
	scheme := "http"
	if parsed.Scheme == "wss" {
		scheme = "https"
	}
	return scheme + "://" + parsed.Host
}

func candidateEndpoint(ip string, port int32) string {
	if ip == "" || port <= 0 {
		return strings.TrimSpace(ip)
	}
	return fmt.Sprintf("%s:%d", ip, port)
}

func appendUnique(items []string, value string) []string {
	value = strings.TrimSpace(value)
	if value == "" {
		return items
	}
	for _, item := range items {
		if item == value {
			return items
		}
	}
	return append(items, value)
}

func isPrivateOrLoopbackIP(raw string) bool {
	ip := net.ParseIP(strings.TrimSpace(raw))
	if ip == nil {
		return false
	}
	return ip.IsPrivate() || ip.IsLoopback() || ip.IsLinkLocalUnicast()
}

func closeChannelOnce(ch chan struct{}) {
	select {
	case <-ch:
	default:
		close(ch)
	}
}

func printable(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return "-"
	}
	return value
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
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

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	var parsed int
	if _, err := fmt.Sscanf(value, "%d", &parsed); err != nil || parsed < 0 {
		return fallback
	}
	return parsed
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	duration, err := time.ParseDuration(value)
	if err == nil && duration > 0 {
		return duration
	}
	var ms int
	if _, err := fmt.Sscanf(value, "%d", &ms); err == nil && ms > 0 {
		return time.Duration(ms) * time.Millisecond
	}
	return fallback
}
