package main

import (
	"testing"

	"github.com/pion/webrtc/v4"
)

func TestSelectedICECandidateRouteFromStatsClassifiesTURNRelay(t *testing.T) {
	report := webrtc.StatsReport{
		"transport-1": webrtc.TransportStats{
			Type:                    webrtc.StatsTypeTransport,
			ID:                      "transport-1",
			SelectedCandidatePairID: "pair-1",
		},
		"pair-1": webrtc.ICECandidatePairStats{
			Type:                 webrtc.StatsTypeCandidatePair,
			ID:                   "pair-1",
			LocalCandidateID:     "local-1",
			RemoteCandidateID:    "remote-1",
			State:                webrtc.StatsICECandidatePairStateSucceeded,
			Nominated:            true,
			CurrentRoundTripTime: 0.018,
			BytesSent:            1234,
			BytesReceived:        5678,
		},
		"local-1": webrtc.ICECandidateStats{
			Type:          webrtc.StatsTypeLocalCandidate,
			ID:            "local-1",
			IP:            "10.0.8.13",
			Port:          49195,
			Protocol:      "udp",
			CandidateType: webrtc.ICECandidateTypeRelay,
			RelayProtocol: "udp",
			URL:           "turn:frp.wzk.icu:3478?transport=udp",
		},
		"remote-1": webrtc.ICECandidateStats{
			Type:          webrtc.StatsTypeRemoteCandidate,
			ID:            "remote-1",
			IP:            "203.0.113.10",
			Port:          53000,
			Protocol:      "udp",
			CandidateType: webrtc.ICECandidateTypeSrflx,
		},
	}

	snapshot, ok := selectedICECandidateRouteFromStats(report)
	if !ok {
		t.Fatal("expected selected candidate pair")
	}
	if snapshot.Route != iceRouteTURN {
		t.Fatalf("route = %q, want %q", snapshot.Route, iceRouteTURN)
	}
	if snapshot.LocalCandidateType != "relay" || snapshot.LocalRelayProtocol != "udp" {
		t.Fatalf("unexpected local candidate: %+v", snapshot)
	}
	if snapshot.CurrentRTTMS != 18 {
		t.Fatalf("current RTT ms = %v, want 18", snapshot.CurrentRTTMS)
	}
}

func TestSelectedICECandidateRouteFromStatsClassifiesSTUNOrDirectFallbackPair(t *testing.T) {
	report := webrtc.StatsReport{
		"pair-1": webrtc.ICECandidatePairStats{
			Type:              webrtc.StatsTypeCandidatePair,
			ID:                "pair-1",
			LocalCandidateID:  "local-1",
			RemoteCandidateID: "remote-1",
			State:             webrtc.StatsICECandidatePairStateSucceeded,
			Nominated:         true,
			BytesSent:         100,
			BytesReceived:     200,
		},
		"local-1": webrtc.ICECandidateStats{
			Type:          webrtc.StatsTypeLocalCandidate,
			ID:            "local-1",
			IP:            "192.168.1.20",
			Port:          55555,
			Protocol:      "udp",
			CandidateType: webrtc.ICECandidateTypeHost,
		},
		"remote-1": webrtc.ICECandidateStats{
			Type:          webrtc.StatsTypeRemoteCandidate,
			ID:            "remote-1",
			IP:            "198.51.100.8",
			Port:          44000,
			Protocol:      "udp",
			CandidateType: webrtc.ICECandidateTypeSrflx,
		},
	}

	snapshot, ok := selectedICECandidateRouteFromStats(report)
	if !ok {
		t.Fatal("expected fallback selected candidate pair")
	}
	if snapshot.Route != iceRouteSTUNOrDirect {
		t.Fatalf("route = %q, want %q", snapshot.Route, iceRouteSTUNOrDirect)
	}
	if snapshot.Detail != "host-srflx" {
		t.Fatalf("detail = %q, want host-srflx", snapshot.Detail)
	}
}

func TestClassifyICERouteUnknownWithoutCandidateTypes(t *testing.T) {
	if got := classifyICERoute("", ""); got != iceRouteUnknown {
		t.Fatalf("route = %q, want %q", got, iceRouteUnknown)
	}
}
