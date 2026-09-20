package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestClassifyICERoute(t *testing.T) {
	tests := []struct {
		name       string
		localType  string
		remoteType string
		want       string
	}{
		{name: "turn-local", localType: "relay", remoteType: "srflx", want: iceRouteTURN},
		{name: "turn-remote", localType: "host", remoteType: "relay", want: iceRouteTURN},
		{name: "stun-or-direct", localType: "srflx", remoteType: "host", want: iceRouteSTUNOrDirect},
		{name: "unknown", localType: "", remoteType: "", want: iceRouteUnknown},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := classifyICERoute(tt.localType, tt.remoteType); got != tt.want {
				t.Fatalf("classifyICERoute(%q, %q) = %q, want %q", tt.localType, tt.remoteType, got, tt.want)
			}
		})
	}
}

func TestInferNATHint(t *testing.T) {
	turn := inferNATHint(candidateSummary{}, candidatePairSnapshot{Route: iceRouteTURN})
	if turn.Category != "turn_relayed" {
		t.Fatalf("TURN hint = %q, want turn_relayed", turn.Category)
	}

	behindNAT := inferNATHint(candidateSummary{
		HasPrivateHostIP: true,
		HasLocalSrflx:    true,
	}, candidatePairSnapshot{Route: iceRouteSTUNOrDirect})
	if behindNAT.Category != "behind_nat" {
		t.Fatalf("behind NAT hint = %q, want behind_nat", behindNAT.Category)
	}

	noSTUN := inferNATHint(candidateSummary{
		HasPrivateHostIP: true,
	}, candidatePairSnapshot{Route: iceRouteSTUNOrDirect})
	if noSTUN.Category != "private_network_no_srflx" {
		t.Fatalf("no STUN hint = %q, want private_network_no_srflx", noSTUN.Category)
	}
}

func TestDefaultOriginForWS(t *testing.T) {
	if got := defaultOriginForWS("ws://example.com:8282/ws"); got != "http://example.com:8282" {
		t.Fatalf("ws origin = %q", got)
	}
	if got := defaultOriginForWS("wss://voice.example.com/ws"); got != "https://voice.example.com" {
		t.Fatalf("wss origin = %q", got)
	}
}

func TestWriteTextResultIsReadable(t *testing.T) {
	result := routeProbeResult{
		WebSocketURL:    "wss://frp.wzk.icu:15827/ws",
		OriginDisabled:  true,
		SessionID:       "rtc_test",
		RobotID:         "test_01",
		BotID:           "xiaowen",
		IceState:        "connected",
		PeerState:       "connected",
		DataChannelOpen: true,
		ICEServerCount:  2,
		Route:           iceRouteTURN,
		RouteDetail:     "host-relay",
		SelectedPair: candidatePairSnapshot{
			Route:               iceRouteTURN,
			Detail:              "host-relay",
			PairID:              "pair_1",
			PairState:           "succeeded",
			Nominated:           true,
			LocalCandidateType:  "host",
			LocalCandidateIP:    "192.168.31.97",
			LocalCandidatePort:  54119,
			LocalProtocol:       "udp",
			RemoteCandidateType: "relay",
			RemoteCandidateIP:   "49.233.169.107",
			RemoteCandidatePort: 49251,
			RemoteProtocol:      "udp",
			CurrentRTTMS:        45.5,
			BytesSent:           1827,
			BytesReceived:       2038,
		},
		CandidateSummary: candidateSummary{
			LocalByType:       map[string]int{"host": 16, "srflx": 2, "relay": 2},
			RemoteByType:      map[string]int{"relay": 2, "srflx": 2},
			LocalHostIPs:      []string{"192.168.31.97"},
			LocalSrflxAddrs:   []string{"124.129.68.75:35382"},
			LocalRelayAddrs:   []string{"49.233.169.107:49180"},
			RemoteRelayAddrs:  []string{"49.233.169.107:49251"},
			HasPrivateHostIP:  true,
			HasLocalSrflx:     true,
			HasLocalRelay:     true,
			HasRemoteRelay:    true,
			UniqueSrflxIPOnly: []string{"124.129.68.75"},
		},
		NATHint: natHint{
			Category:   "turn_relayed",
			Confidence: "medium",
			Detail:     "Selected ICE pair uses a relay candidate.",
		},
	}
	var out bytes.Buffer
	writeTextResult(&out, result)
	text := out.String()
	for _, want := range []string{
		"RTC Route Probe",
		"Result: TURN relay selected",
		"Session",
		"Signaling",
		"Selected ICE Pair",
		"Candidates",
		"Diagnosis",
		"origin:        disabled (--no-origin)",
		"remote:        relay / 49.233.169.107:49251 / udp",
		"Gateway 侧公网 UDP 直达不可用",
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("pretty output missing %q in:\n%s", want, text)
		}
	}
}
