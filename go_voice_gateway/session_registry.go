package main

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

type gatewaySessionRegistry struct {
	mu       sync.RWMutex
	sessions map[string]*gatewaySession
}

func newGatewaySessionRegistry() *gatewaySessionRegistry {
	return &gatewaySessionRegistry{sessions: make(map[string]*gatewaySession)}
}

func (r *gatewaySessionRegistry) Add(session *gatewaySession) {
	if session == nil || session.id == "" {
		return
	}
	r.mu.Lock()
	r.sessions[session.id] = session
	r.mu.Unlock()
}

func (r *gatewaySessionRegistry) Remove(sessionID string, session *gatewaySession) {
	if sessionID == "" {
		return
	}
	r.mu.Lock()
	if current := r.sessions[sessionID]; current == session {
		delete(r.sessions, sessionID)
	}
	r.mu.Unlock()
}

func (r *gatewaySessionRegistry) CloseAll() int {
	if r == nil {
		return 0
	}
	r.mu.Lock()
	sessions := make([]*gatewaySession, 0, len(r.sessions))
	for _, session := range r.sessions {
		sessions = append(sessions, session)
	}
	r.sessions = make(map[string]*gatewaySession)
	r.mu.Unlock()

	for _, session := range sessions {
		if session != nil {
			session.close()
		}
	}
	return len(sessions)
}

func (r *gatewaySessionRegistry) Lookup(sessionID string) *gatewaySession {
	if sessionID == "" {
		return nil
	}
	r.mu.RLock()
	session := r.sessions[sessionID]
	r.mu.RUnlock()
	return session
}

func (r *gatewaySessionRegistry) Count() int {
	if r == nil {
		return 0
	}
	r.mu.RLock()
	defer r.mu.RUnlock()
	return len(r.sessions)
}

func (r *gatewaySessionRegistry) Snapshot(now time.Time, limit int) map[string]any {
	if r == nil {
		return map[string]any{
			"total":      0,
			"registered": 0,
			"limit":      limit,
			"items":      []gatewaySessionStatus{},
		}
	}
	if now.IsZero() {
		now = time.Now()
	}
	if limit <= 0 {
		limit = 16
	}

	r.mu.RLock()
	sessions := make([]*gatewaySession, 0, len(r.sessions))
	for _, session := range r.sessions {
		sessions = append(sessions, session)
	}
	r.mu.RUnlock()

	sort.Slice(sessions, func(i, j int) bool {
		return sessions[i].createdAt.After(sessions[j].createdAt)
	})

	registered := 0
	itemCapacity := len(sessions)
	if itemCapacity > limit {
		itemCapacity = limit
	}
	items := make([]gatewaySessionStatus, 0, itemCapacity)
	for _, session := range sessions {
		status := session.statusSnapshot(now)
		if status.Registered {
			registered++
		}
		if len(items) < limit {
			items = append(items, status)
		}
	}
	return map[string]any{
		"total":      len(sessions),
		"registered": registered,
		"limit":      limit,
		"items":      items,
	}
}

type gatewaySessionStatus struct {
	SessionID          string                  `json:"session_id"`
	CreatedAt          string                  `json:"created_at,omitempty"`
	AgeMS              int64                   `json:"age_ms"`
	SourceIP           string                  `json:"source_ip,omitempty"`
	RemoteIP           string                  `json:"remote_ip,omitempty"`
	RemoteAddr         string                  `json:"remote_addr,omitempty"`
	RobotID            string                  `json:"robot_id,omitempty"`
	ClientType         string                  `json:"client_type,omitempty"`
	BotID              string                  `json:"bot_id,omitempty"`
	Registered         bool                    `json:"registered"`
	PeerConnection     bool                    `json:"peer_connection"`
	ICEState           string                  `json:"ice_state,omitempty"`
	PeerState          string                  `json:"peer_state,omitempty"`
	DownlinkReady      bool                    `json:"downlink_ready"`
	DownlinkBlocked    bool                    `json:"downlink_blocked"`
	DownlinkGeneration uint64                  `json:"downlink_generation"`
	RTPTracks          []gatewayRTPTrackStatus `json:"rtp_tracks,omitempty"`
	RecentEvents       int                     `json:"recent_events"`
	Vision             visionSessionStatus     `json:"vision"`
}

type gatewayRTPTrackStatus struct {
	Kind                 string `json:"kind"`
	TrackID              string `json:"track_id"`
	StreamID             string `json:"stream_id,omitempty"`
	Packets              uint64 `json:"packets"`
	PayloadBytes         uint64 `json:"payload_bytes"`
	CachedPackets        int    `json:"cached_packets"`
	SequenceGaps         uint64 `json:"sequence_gaps"`
	TimestampRegressions uint64 `json:"timestamp_regressions"`
}

func (s *gatewaySession) statusSnapshot(now time.Time) gatewaySessionStatus {
	if s == nil {
		return gatewaySessionStatus{}
	}
	createdAt := s.createdAt
	createdAtText := ""
	ageMS := int64(0)
	if !createdAt.IsZero() {
		createdAtText = createdAt.Format(time.RFC3339Nano)
		if now.After(createdAt) {
			ageMS = now.Sub(createdAt).Milliseconds()
		}
	}

	s.identityMu.RLock()
	robotID := s.robotID
	clientType := s.clientType
	botID := s.botID
	registered := s.robotID != "" && s.botID != ""
	s.identityMu.RUnlock()

	peerConnection := false
	iceState := ""
	peerState := ""
	s.pcMu.Lock()
	if s.pc != nil {
		peerConnection = true
		iceState = s.pc.ICEConnectionState().String()
		peerState = s.pc.ConnectionState().String()
	}
	s.pcMu.Unlock()

	s.downMu.Lock()
	downlinkReady := s.downlink != nil
	s.downMu.Unlock()

	s.eventMu.Lock()
	recentEvents := len(s.events)
	s.eventMu.Unlock()

	return gatewaySessionStatus{
		SessionID:          s.id,
		CreatedAt:          createdAtText,
		AgeMS:              ageMS,
		SourceIP:           s.connInfo.SourceIP,
		RemoteIP:           s.connInfo.RemoteIP,
		RemoteAddr:         s.connInfo.RemoteAddr,
		RobotID:            robotID,
		ClientType:         clientType,
		BotID:              botID,
		Registered:         registered,
		PeerConnection:     peerConnection,
		ICEState:           iceState,
		PeerState:          peerState,
		DownlinkReady:      downlinkReady,
		DownlinkBlocked:    s.downBlocked.Load(),
		DownlinkGeneration: s.downGen.Load(),
		RTPTracks:          s.rtpTrackStatusSnapshots(),
		RecentEvents:       recentEvents,
		Vision:             s.visionStatusSnapshot(now),
	}
}

func (s *gatewaySession) rtpTrackStatusSnapshots() []gatewayRTPTrackStatus {
	s.rtpMu.Lock()
	defer s.rtpMu.Unlock()
	if len(s.rtp) == 0 {
		return nil
	}
	tracks := make([]gatewayRTPTrackStatus, 0, len(s.rtp))
	for _, stats := range s.rtp {
		snapshot := stats.snapshot()
		tracks = append(tracks, gatewayRTPTrackStatus{
			Kind:                 snapshot.key.kind,
			TrackID:              snapshot.key.id,
			StreamID:             snapshot.key.streamID,
			Packets:              snapshot.packetCount,
			PayloadBytes:         snapshot.payloadBytes,
			CachedPackets:        snapshot.cachedPackets,
			SequenceGaps:         snapshot.sequenceGaps,
			TimestampRegressions: snapshot.timestampRegressions,
		})
	}
	sort.Slice(tracks, func(i, j int) bool {
		if tracks[i].Kind != tracks[j].Kind {
			return tracks[i].Kind < tracks[j].Kind
		}
		if tracks[i].TrackID != tracks[j].TrackID {
			return tracks[i].TrackID < tracks[j].TrackID
		}
		return tracks[i].StreamID < tracks[j].StreamID
	})
	return tracks
}

func (r *gatewaySessionRegistry) PythonGatewayCredentialsForSession(sessionID string) (pythonGatewaySessionCredentials, bool) {
	session := r.Lookup(sessionID)
	if session == nil {
		return pythonGatewaySessionCredentials{}, false
	}
	return session.pythonGatewayCredentials()
}

func (r *gatewaySessionRegistry) ForwardPythonGatewayDownlink(sessionID string, message pythonGatewayBridgeMessage) error {
	session := r.Lookup(sessionID)
	if session == nil {
		return fmt.Errorf("python gateway downlink session not found: %s", sessionID)
	}
	if message.IsJSON {
		if message.JSON == nil {
			return fmt.Errorf("python gateway downlink JSON payload is empty")
		}
		if message.Type == "playback_start" {
			rtpStartSequence := session.startDownlinkPlayback(
				stringValue(message.JSON["trace_id"]),
				stringValue(message.JSON["round_id"]),
				stringValue(message.JSON["playback_id"]),
				"playback_start",
			)
			message.JSON["rtp_start_sequence"] = rtpStartSequence
		}
		if session.shouldQueuePythonGatewayDownlink(message) {
			return session.queuePythonGatewayDownlink(message)
		}
		return session.sendJSON(message.JSON)
	}
	if len(message.Binary) == 0 {
		return fmt.Errorf("python gateway downlink binary payload is empty")
	}
	return session.forwardPythonGatewayBinaryDownlink(message.Binary)
}
