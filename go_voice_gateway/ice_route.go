package main

import (
	"fmt"
	"strings"

	"github.com/pion/webrtc/v4"
)

const (
	iceRouteTURN         = "turn"
	iceRouteSTUNOrDirect = "stun_or_direct"
	iceRouteUnknown      = "unknown"
)

type iceCandidateRouteSnapshot struct {
	Route                  string
	Detail                 string
	PairID                 string
	PairState              string
	Nominated              bool
	LocalCandidateID       string
	LocalCandidateType     string
	LocalCandidateIP       string
	LocalCandidatePort     int32
	LocalProtocol          string
	LocalRelayProtocol     string
	LocalCandidateURL      string
	RemoteCandidateID      string
	RemoteCandidateType    string
	RemoteCandidateIP      string
	RemoteCandidatePort    int32
	RemoteProtocol         string
	RemoteRelayProtocol    string
	RemoteCandidateURL     string
	CurrentRTTMS           float64
	TotalRTTMS             float64
	PacketsSent            uint32
	PacketsReceived        uint32
	BytesSent              uint64
	BytesReceived          uint64
	RequestsSent           uint64
	ResponsesReceived      uint64
	LocalCandidatePresent  bool
	RemoteCandidatePresent bool
}

func selectedICECandidateRouteFromStats(report webrtc.StatsReport) (iceCandidateRouteSnapshot, bool) {
	if len(report) == 0 {
		return iceCandidateRouteSnapshot{}, false
	}

	candidates := make(map[string]webrtc.ICECandidateStats)
	for _, stat := range report {
		if candidate, ok := asICECandidateStats(stat); ok {
			candidates[candidate.ID] = candidate
		}
	}

	pair, ok := selectedICECandidatePairStats(report)
	if !ok {
		return iceCandidateRouteSnapshot{}, false
	}

	local, localOK := candidates[pair.LocalCandidateID]
	remote, remoteOK := candidates[pair.RemoteCandidateID]
	snapshot := iceCandidateRouteSnapshot{
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
		RequestsSent:           pair.RequestsSent,
		ResponsesReceived:      pair.ResponsesReceived,
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

func selectedICECandidatePairStats(report webrtc.StatsReport) (webrtc.ICECandidatePairStats, bool) {
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

func (s iceCandidateRouteSnapshot) logAttrs() []any {
	return []any{
		"ice_route", s.Route,
		"ice_route_detail", printable(s.Detail),
		"candidate_pair_id", printable(s.PairID),
		"candidate_pair_state", printable(s.PairState),
		"candidate_pair_nominated", s.Nominated,
		"local_candidate_id", printable(s.LocalCandidateID),
		"local_candidate_type", printable(s.LocalCandidateType),
		"local_candidate_ip", printable(s.LocalCandidateIP),
		"local_candidate_port", s.LocalCandidatePort,
		"local_candidate_protocol", printable(s.LocalProtocol),
		"local_candidate_relay_protocol", printable(s.LocalRelayProtocol),
		"local_candidate_url", printable(s.LocalCandidateURL),
		"remote_candidate_id", printable(s.RemoteCandidateID),
		"remote_candidate_type", printable(s.RemoteCandidateType),
		"remote_candidate_ip", printable(s.RemoteCandidateIP),
		"remote_candidate_port", s.RemoteCandidatePort,
		"remote_candidate_protocol", printable(s.RemoteProtocol),
		"remote_candidate_relay_protocol", printable(s.RemoteRelayProtocol),
		"remote_candidate_url", printable(s.RemoteCandidateURL),
		"candidate_pair_rtt_ms", s.CurrentRTTMS,
		"candidate_pair_total_rtt_ms", s.TotalRTTMS,
		"candidate_pair_packets_sent", s.PacketsSent,
		"candidate_pair_packets_received", s.PacketsReceived,
		"candidate_pair_bytes_sent", s.BytesSent,
		"candidate_pair_bytes_received", s.BytesReceived,
		"candidate_pair_requests_sent", s.RequestsSent,
		"candidate_pair_responses_received", s.ResponsesReceived,
		"local_candidate_present", s.LocalCandidatePresent,
		"remote_candidate_present", s.RemoteCandidatePresent,
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
