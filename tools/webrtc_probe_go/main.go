package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/pion/ice/v4"
	"github.com/pion/webrtc/v4"
)

const defaultICEServers = "stun:stun.l.google.com:19302"

type probeConfig struct {
	iceServersRaw              string
	timeout                    time.Duration
	payload                    string
	localInterfaceName         string
	localIP                    string
	offererICETransportPolicy  webrtc.ICETransportPolicy
	answererICETransportPolicy webrtc.ICETransportPolicy
}

type candidateSummary struct {
	mu      sync.Mutex
	total   int
	host    int
	srflx   int
	prflx   int
	relay   int
	unknown int
	lines   []string
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "summary verdict=fail error=%v\n", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := parseArgs()
	if err != nil {
		return err
	}
	iceServers, err := parseICEServers(cfg.iceServersRaw)
	if err != nil {
		return err
	}

	fmt.Printf(
		"webrtc_probe runtime=go engine=pion ice_servers=%d timeout_ms=%d interface=%s ip=%s offerer_policy=%s answerer_policy=%s\n",
		len(iceServers),
		cfg.timeout.Milliseconds(),
		printable(cfg.localInterfaceName),
		printable(cfg.localIP),
		cfg.offererICETransportPolicy.String(),
		cfg.answererICETransportPolicy.String(),
	)

	ctx, cancel := context.WithTimeout(context.Background(), cfg.timeout)
	defer cancel()

	api, err := newWebRTCAPI(cfg)
	if err != nil {
		return err
	}
	offererConfig := webrtc.Configuration{
		ICEServers:         iceServers,
		ICETransportPolicy: cfg.offererICETransportPolicy,
	}
	offerer, err := api.NewPeerConnection(offererConfig)
	if err != nil {
		return err
	}
	defer offerer.Close()
	answererConfig := webrtc.Configuration{
		ICEServers:         iceServers,
		ICETransportPolicy: cfg.answererICETransportPolicy,
	}
	answerer, err := api.NewPeerConnection(answererConfig)
	if err != nil {
		return err
	}
	defer answerer.Close()

	offererCandidates := &candidateSummary{}
	answererCandidates := &candidateSummary{}
	offerer.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		offererCandidates.add(candidate)
	})
	answerer.OnICECandidate(func(candidate *webrtc.ICECandidate) {
		answererCandidates.add(candidate)
	})

	pongCh := make(chan string, 1)
	stateCh := make(chan error, 4)
	offerer.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		fmt.Printf("event peer=offerer ice_state=%s\n", state.String())
		reportTerminalICEState("offerer", state, stateCh)
	})
	answerer.OnICEConnectionStateChange(func(state webrtc.ICEConnectionState) {
		fmt.Printf("event peer=answerer ice_state=%s\n", state.String())
		reportTerminalICEState("answerer", state, stateCh)
	})

	answerer.OnDataChannel(func(dc *webrtc.DataChannel) {
		fmt.Printf("event peer=answerer datachannel=%s\n", dc.Label())
		dc.OnMessage(func(message webrtc.DataChannelMessage) {
			reply := "pong:" + string(message.Data)
			if err := dc.SendText(reply); err != nil {
				select {
				case stateCh <- fmt.Errorf("answerer datachannel send failed: %w", err):
				default:
				}
			}
		})
	})

	dc, err := offerer.CreateDataChannel("probe", nil)
	if err != nil {
		return err
	}
	dc.OnOpen(func() {
		fmt.Printf("event peer=offerer datachannel=open label=%s\n", dc.Label())
		if err := dc.SendText(cfg.payload); err != nil {
			select {
			case stateCh <- fmt.Errorf("offerer datachannel send failed: %w", err):
			default:
			}
		}
	})
	dc.OnMessage(func(message webrtc.DataChannelMessage) {
		select {
		case pongCh <- string(message.Data):
		default:
		}
	})

	if err := negotiate(ctx, offerer, answerer); err != nil {
		return err
	}

	select {
	case pong := <-pongCh:
		if pong != "pong:"+cfg.payload {
			printCandidateSummaries(offererCandidates, answererCandidates)
			return fmt.Errorf("unexpected datachannel reply: %q", pong)
		}
	case err := <-stateCh:
		printCandidateSummaries(offererCandidates, answererCandidates)
		return err
	case <-ctx.Done():
		printCandidateSummaries(offererCandidates, answererCandidates)
		return fmt.Errorf("probe timeout waiting for datachannel pong: %w", ctx.Err())
	}

	printCandidateSummaries(offererCandidates, answererCandidates)
	fmt.Printf("summary verdict=ok datachannel_pong=true offerer_ice=%s answerer_ice=%s\n",
		offerer.ICEConnectionState().String(),
		answerer.ICEConnectionState().String(),
	)
	return nil
}

func printCandidateSummaries(offererCandidates, answererCandidates *candidateSummary) {
	fmt.Printf("candidates peer=offerer %s\n", offererCandidates.summary())
	fmt.Printf("candidates peer=answerer %s\n", answererCandidates.summary())
}

func parseArgs() (probeConfig, error) {
	timeoutMS := flag.Int("timeout-ms", envInt("WEBRTC_PROBE_TIMEOUT_MS", 10000), "overall probe timeout in milliseconds")
	iceServers := flag.String("ice-servers", envFirstNonEmpty(defaultICEServers, "WEBRTC_ICE_SERVERS", "GATEWAY_RTC_ICE_SERVERS"), "ICE servers as JSON, JSON object, or comma-separated URLs")
	payload := flag.String("payload", envString("WEBRTC_PROBE_PAYLOAD", "ping"), "datachannel ping payload")
	localInterfaceName := flag.String("interface", envString("WEBRTC_PROBE_INTERFACE", ""), "optional local interface filter, for example utun5")
	localIP := flag.String("ip", envString("WEBRTC_PROBE_IP", ""), "optional local candidate IP filter")
	policyRaw := flag.String("ice-transport-policy", envString("WEBRTC_ICE_TRANSPORT_POLICY", "all"), "ICE transport policy: all or relay")
	offererPolicyRaw := flag.String("offerer-ice-transport-policy", envString("WEBRTC_OFFERER_ICE_TRANSPORT_POLICY", ""), "offerer ICE transport policy override: all or relay")
	answererPolicyRaw := flag.String("answerer-ice-transport-policy", envString("WEBRTC_ANSWERER_ICE_TRANSPORT_POLICY", ""), "answerer ICE transport policy override: all or relay")
	flag.Parse()

	policy, err := parseICETransportPolicy(*policyRaw)
	if err != nil {
		return probeConfig{}, err
	}
	offererPolicy, err := parseOptionalICETransportPolicy(*offererPolicyRaw, policy)
	if err != nil {
		return probeConfig{}, err
	}
	answererPolicy, err := parseOptionalICETransportPolicy(*answererPolicyRaw, policy)
	if err != nil {
		return probeConfig{}, err
	}
	timeout := time.Duration(maxInt(*timeoutMS, 1)) * time.Millisecond
	return probeConfig{
		iceServersRaw:              strings.TrimSpace(*iceServers),
		timeout:                    timeout,
		payload:                    *payload,
		localInterfaceName:         strings.TrimSpace(*localInterfaceName),
		localIP:                    strings.TrimSpace(*localIP),
		offererICETransportPolicy:  offererPolicy,
		answererICETransportPolicy: answererPolicy,
	}, nil
}

func parseICETransportPolicy(raw string) (webrtc.ICETransportPolicy, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", "all":
		return webrtc.ICETransportPolicyAll, nil
	case "relay", "turn":
		return webrtc.ICETransportPolicyRelay, nil
	default:
		return webrtc.ICETransportPolicyAll, fmt.Errorf("unsupported ICE transport policy: %s", raw)
	}
}

func parseOptionalICETransportPolicy(raw string, fallback webrtc.ICETransportPolicy) (webrtc.ICETransportPolicy, error) {
	if strings.TrimSpace(raw) == "" {
		return fallback, nil
	}
	return parseICETransportPolicy(raw)
}

func newWebRTCAPI(cfg probeConfig) (*webrtc.API, error) {
	settingEngine := webrtc.SettingEngine{}
	settingEngine.SetICEMulticastDNSMode(ice.MulticastDNSModeDisabled)
	settingEngine.SetIncludeLoopbackCandidate(true)
	if cfg.localInterfaceName != "" {
		settingEngine.SetInterfaceFilter(func(interfaceName string) bool {
			return interfaceName == cfg.localInterfaceName
		})
	}
	if cfg.localIP != "" {
		wanted := net.ParseIP(cfg.localIP)
		if wanted == nil {
			return nil, fmt.Errorf("invalid local IP filter: %s", cfg.localIP)
		}
		settingEngine.SetIPFilter(func(ip net.IP) bool {
			return ip.Equal(wanted)
		})
	}
	return webrtc.NewAPI(webrtc.WithSettingEngine(settingEngine)), nil
}

func negotiate(ctx context.Context, offerer, answerer *webrtc.PeerConnection) error {
	offer, err := offerer.CreateOffer(nil)
	if err != nil {
		return err
	}
	offerGatherComplete := webrtc.GatheringCompletePromise(offerer)
	if err := offerer.SetLocalDescription(offer); err != nil {
		return err
	}
	if err := waitGatheringComplete(ctx, offerGatherComplete, "offerer"); err != nil {
		return err
	}
	if err := answerer.SetRemoteDescription(*offerer.LocalDescription()); err != nil {
		return err
	}

	answer, err := answerer.CreateAnswer(nil)
	if err != nil {
		return err
	}
	answerGatherComplete := webrtc.GatheringCompletePromise(answerer)
	if err := answerer.SetLocalDescription(answer); err != nil {
		return err
	}
	if err := waitGatheringComplete(ctx, answerGatherComplete, "answerer"); err != nil {
		return err
	}
	if err := offerer.SetRemoteDescription(*answerer.LocalDescription()); err != nil {
		return err
	}
	return nil
}

func waitGatheringComplete(ctx context.Context, done <-chan struct{}, peer string) error {
	select {
	case <-done:
		fmt.Printf("event peer=%s gathering=complete\n", peer)
		return nil
	case <-ctx.Done():
		return fmt.Errorf("%s ICE gathering timeout: %w", peer, ctx.Err())
	}
}

func reportTerminalICEState(peer string, state webrtc.ICEConnectionState, stateCh chan<- error) {
	switch state {
	case webrtc.ICEConnectionStateFailed:
		select {
		case stateCh <- fmt.Errorf("%s ICE failed", peer):
		default:
		}
	case webrtc.ICEConnectionStateClosed:
		select {
		case stateCh <- fmt.Errorf("%s ICE closed", peer):
		default:
		}
	}
}

func parseICEServers(raw string) ([]webrtc.ICEServer, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}

	if strings.HasPrefix(raw, "[") || strings.HasPrefix(raw, "{") {
		var value any
		if err := json.Unmarshal([]byte(raw), &value); err != nil {
			return nil, fmt.Errorf("parse ICE server JSON: %w", err)
		}
		return normalizeICEServerValue(value)
	}

	parts := strings.Split(raw, ",")
	servers := make([]webrtc.ICEServer, 0, len(parts))
	for _, part := range parts {
		url := strings.TrimSpace(part)
		if url != "" {
			servers = append(servers, webrtc.ICEServer{URLs: []string{url}})
		}
	}
	return servers, nil
}

func normalizeICEServerValue(value any) ([]webrtc.ICEServer, error) {
	switch typed := value.(type) {
	case []any:
		servers := make([]webrtc.ICEServer, 0, len(typed))
		for _, item := range typed {
			normalized, err := normalizeICEServerValue(item)
			if err != nil {
				return nil, err
			}
			servers = append(servers, normalized...)
		}
		return servers, nil
	case map[string]any:
		server, ok := normalizeICEServerObject(typed)
		if !ok {
			return nil, nil
		}
		return []webrtc.ICEServer{server}, nil
	case string:
		url := strings.TrimSpace(typed)
		if url == "" {
			return nil, nil
		}
		return []webrtc.ICEServer{{URLs: []string{url}}}, nil
	default:
		return nil, fmt.Errorf("unsupported ICE server value: %T", value)
	}
}

func normalizeICEServerObject(value map[string]any) (webrtc.ICEServer, bool) {
	urls := normalizeStringList(value["urls"])
	if len(urls) == 0 {
		return webrtc.ICEServer{}, false
	}
	server := webrtc.ICEServer{URLs: urls}
	if username := stringValue(value["username"]); username != "" {
		server.Username = username
	}
	if credential := stringValue(value["credential"]); credential != "" {
		server.Credential = credential
	}
	return server, true
}

func normalizeStringList(value any) []string {
	switch typed := value.(type) {
	case string:
		trimmed := strings.TrimSpace(typed)
		if trimmed == "" {
			return nil
		}
		return []string{trimmed}
	case []any:
		values := make([]string, 0, len(typed))
		for _, item := range typed {
			if text := stringValue(item); text != "" {
				values = append(values, text)
			}
		}
		return values
	default:
		return nil
	}
}

func stringValue(value any) string {
	switch typed := value.(type) {
	case nil:
		return ""
	case string:
		return strings.TrimSpace(typed)
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func (summary *candidateSummary) add(candidate *webrtc.ICECandidate) {
	if candidate == nil {
		return
	}
	line := candidate.ToJSON().Candidate
	summary.mu.Lock()
	defer summary.mu.Unlock()
	summary.total++
	switch {
	case strings.Contains(line, " typ host"):
		summary.host++
	case strings.Contains(line, " typ srflx"):
		summary.srflx++
	case strings.Contains(line, " typ prflx"):
		summary.prflx++
	case strings.Contains(line, " typ relay"):
		summary.relay++
	default:
		summary.unknown++
	}
	if len(summary.lines) < 12 {
		summary.lines = append(summary.lines, line)
	}
}

func (summary *candidateSummary) summary() string {
	summary.mu.Lock()
	defer summary.mu.Unlock()
	return fmt.Sprintf(
		"total=%d host=%d srflx=%d prflx=%d relay=%d unknown=%d",
		summary.total,
		summary.host,
		summary.srflx,
		summary.prflx,
		summary.relay,
		summary.unknown,
	)
}

func envFirstNonEmpty(fallback string, names ...string) string {
	for _, name := range names {
		if value := strings.TrimSpace(os.Getenv(name)); value != "" {
			return value
		}
	}
	return fallback
}

func envString(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func maxInt(value, floor int) int {
	if value < floor {
		return floor
	}
	return value
}

func printable(value string) string {
	if strings.TrimSpace(value) == "" {
		return "-"
	}
	return value
}
