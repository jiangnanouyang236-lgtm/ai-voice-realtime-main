package main

import (
	"bytes"
	"crypto/subtle"
	"encoding/binary"
	"fmt"
	"image/jpeg"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/pion/webrtc/v4"
)

const (
	visionDataChannelLabel   = "vision-v1"
	visionProtocolVersion    = 1
	visionChunkHeaderBytes   = 32
	visionChunkPayloadBytes  = 32 * 1024
	visionMaxImageBytes      = 100 * 1024
	visionMaxChunks          = 4
	visionMaxInflightFrames  = 2
	visionUsableAge          = 5 * time.Second
	visionCacheTTL           = 15 * time.Second
	visionAssemblyTTL        = 5 * time.Second
	visionMaxFutureClockSkew = 2 * time.Second
	visionExpectedWidth      = 640
	visionExpectedHeight     = 360
)

var visionChunkMagic = [4]byte{'V', 'I', 'S', '1'}

const visionInternalAuthScheme = "Bearer "

type visionChunk struct {
	frameID      uint64
	capturedAtMS int64
	totalBytes   int
	chunkIndex   int
	chunkCount   int
	payload      []byte
}

type visionFrameAssembly struct {
	frameID      uint64
	capturedAtMS int64
	totalBytes   int
	chunks       [][]byte
	received     int
	startedAt    time.Time
}

type visionSnapshot struct {
	frameID      uint64
	capturedAtMS int64
	receivedAt   time.Time
	jpeg         []byte
	width        int
	height       int
}

type visionSnapshotAck struct {
	Type         string `json:"type"`
	FrameID      string `json:"frame_id,omitempty"`
	Status       string `json:"status"`
	Reason       string `json:"reason,omitempty"`
	ReceivedAtMS int64  `json:"received_at_ms"`
}

type visionSessionStatus struct {
	Cached         bool   `json:"cached"`
	Usable         bool   `json:"usable"`
	FrameID        string `json:"frame_id,omitempty"`
	AgeMS          int64  `json:"age_ms,omitempty"`
	Bytes          int    `json:"bytes,omitempty"`
	InflightFrames int    `json:"inflight_frames"`
	Accepted       uint64 `json:"accepted"`
	Dropped        uint64 `json:"dropped"`
}

type visionSessionState struct {
	mu       sync.Mutex
	inflight map[uint64]*visionFrameAssembly
	latest   *visionSnapshot
	accepted uint64
	dropped  uint64
}

func (s *Server) handleVisionSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method_not_allowed"})
		return
	}

	expectedToken := strings.TrimSpace(s.cfg.VisionInternalToken)
	if expectedToken == "" {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "vision_internal_auth_not_configured"})
		return
	}
	providedToken := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), visionInternalAuthScheme))
	if !strings.HasPrefix(r.Header.Get("Authorization"), visionInternalAuthScheme) ||
		len(providedToken) != len(expectedToken) ||
		subtle.ConstantTimeCompare([]byte(providedToken), []byte(expectedToken)) != 1 {
		w.Header().Set("WWW-Authenticate", "Bearer")
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		return
	}

	sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))
	if sessionID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "session_id_required"})
		return
	}
	session := s.sessions.Lookup(sessionID)
	if session == nil && s.pythonGateway != nil {
		session = s.sessions.Lookup(s.pythonGateway.resolveClientSessionAlias(sessionID))
	}
	if session == nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "session_not_found"})
		return
	}

	now := time.Now()
	snapshot, ok := session.latestUsableVisionSnapshot(now)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "snapshot_unavailable"})
		return
	}

	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Content-Length", strconv.Itoa(len(snapshot.jpeg)))
	w.Header().Set("X-Vision-Frame-ID", strconv.FormatUint(snapshot.frameID, 10))
	w.Header().Set("X-Vision-Captured-At-Ms", strconv.FormatInt(snapshot.capturedAtMS, 10))
	w.Header().Set("X-Vision-Age-Ms", strconv.FormatInt(now.UnixMilli()-snapshot.capturedAtMS, 10))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(snapshot.jpeg)
}

func parseVisionChunk(data []byte) (visionChunk, error) {
	if len(data) < visionChunkHeaderBytes {
		return visionChunk{}, fmt.Errorf("header_too_short")
	}
	if !bytes.Equal(data[:4], visionChunkMagic[:]) {
		return visionChunk{}, fmt.Errorf("bad_magic")
	}
	if data[4] != visionProtocolVersion {
		return visionChunk{}, fmt.Errorf("unsupported_version")
	}
	if data[5] != 0 {
		return visionChunk{}, fmt.Errorf("unsupported_flags")
	}
	if int(binary.BigEndian.Uint16(data[6:8])) != visionChunkHeaderBytes {
		return visionChunk{}, fmt.Errorf("bad_header_length")
	}

	frameID := binary.BigEndian.Uint64(data[8:16])
	capturedAtRaw := binary.BigEndian.Uint64(data[16:24])
	totalBytes := int(binary.BigEndian.Uint32(data[24:28]))
	chunkIndex := int(binary.BigEndian.Uint16(data[28:30]))
	chunkCount := int(binary.BigEndian.Uint16(data[30:32]))
	payload := data[visionChunkHeaderBytes:]

	switch {
	case frameID == 0:
		return visionChunk{}, fmt.Errorf("missing_frame_id")
	case capturedAtRaw > uint64(^uint64(0)>>1):
		return visionChunk{}, fmt.Errorf("bad_capture_time")
	case totalBytes <= 0 || totalBytes > visionMaxImageBytes:
		return visionChunk{}, fmt.Errorf("image_size_out_of_range")
	case chunkCount <= 0 || chunkCount > visionMaxChunks:
		return visionChunk{}, fmt.Errorf("chunk_count_out_of_range")
	case chunkIndex < 0 || chunkIndex >= chunkCount:
		return visionChunk{}, fmt.Errorf("chunk_index_out_of_range")
	case len(payload) == 0 || len(payload) > visionChunkPayloadBytes:
		return visionChunk{}, fmt.Errorf("chunk_size_out_of_range")
	}

	return visionChunk{
		frameID:      frameID,
		capturedAtMS: int64(capturedAtRaw),
		totalBytes:   totalBytes,
		chunkIndex:   chunkIndex,
		chunkCount:   chunkCount,
		payload:      append([]byte(nil), payload...),
	}, nil
}

func (s *gatewaySession) handleVisionDataChannelBinary(dc *webrtc.DataChannel, data []byte) {
	now := time.Now()
	ack, snapshot := s.acceptVisionChunk(data, now)
	if ack == nil {
		return
	}
	if snapshot != nil {
		s.logInfo(
			"vision_snapshot_accepted",
			"frame_id", ack.FrameID,
			"bytes", len(snapshot.jpeg),
			"width", snapshot.width,
			"height", snapshot.height,
			"capture_age_ms", now.UnixMilli()-snapshot.capturedAtMS,
		)
	} else {
		s.logWarn(
			"vision_snapshot_dropped",
			"frame_id", printable(ack.FrameID),
			"reason", printable(ack.Reason),
		)
	}
	if dc != nil {
		if err := sendDataChannelJSON(dc, ack); err != nil {
			s.logWarn("vision_snapshot_ack_failed", "frame_id", printable(ack.FrameID), "error", err)
		}
	}
}

func (s *gatewaySession) acceptVisionChunk(data []byte, now time.Time) (*visionSnapshotAck, *visionSnapshot) {
	if now.IsZero() {
		now = time.Now()
	}
	chunk, err := parseVisionChunk(data)
	if err != nil {
		s.vision.mu.Lock()
		s.vision.dropped++
		s.vision.pruneLocked(now)
		s.vision.mu.Unlock()
		return newVisionAck(0, "dropped", err.Error(), now), nil
	}

	s.vision.mu.Lock()
	s.vision.pruneLocked(now)
	if s.vision.inflight == nil {
		s.vision.inflight = make(map[uint64]*visionFrameAssembly)
	}
	assembly := s.vision.inflight[chunk.frameID]
	if assembly == nil {
		if len(s.vision.inflight) >= visionMaxInflightFrames {
			s.vision.dropOldestAssemblyLocked()
			s.vision.dropped++
		}
		assembly = &visionFrameAssembly{
			frameID:      chunk.frameID,
			capturedAtMS: chunk.capturedAtMS,
			totalBytes:   chunk.totalBytes,
			chunks:       make([][]byte, chunk.chunkCount),
			startedAt:    now,
		}
		s.vision.inflight[chunk.frameID] = assembly
	}
	if assembly.capturedAtMS != chunk.capturedAtMS ||
		assembly.totalBytes != chunk.totalBytes ||
		len(assembly.chunks) != chunk.chunkCount {
		delete(s.vision.inflight, chunk.frameID)
		s.vision.dropped++
		s.vision.mu.Unlock()
		return newVisionAck(chunk.frameID, "dropped", "inconsistent_frame_metadata", now), nil
	}
	if existing := assembly.chunks[chunk.chunkIndex]; existing != nil {
		if !bytes.Equal(existing, chunk.payload) {
			delete(s.vision.inflight, chunk.frameID)
			s.vision.dropped++
			s.vision.mu.Unlock()
			return newVisionAck(chunk.frameID, "dropped", "conflicting_duplicate_chunk", now), nil
		}
		s.vision.mu.Unlock()
		return nil, nil
	}
	assembly.chunks[chunk.chunkIndex] = chunk.payload
	assembly.received++
	if assembly.received < len(assembly.chunks) {
		s.vision.mu.Unlock()
		return nil, nil
	}
	delete(s.vision.inflight, chunk.frameID)
	s.vision.mu.Unlock()

	jpegBytes := make([]byte, 0, assembly.totalBytes)
	for _, payload := range assembly.chunks {
		jpegBytes = append(jpegBytes, payload...)
	}
	if len(jpegBytes) != assembly.totalBytes {
		s.markVisionDropped()
		return newVisionAck(chunk.frameID, "dropped", "assembled_size_mismatch", now), nil
	}
	if reason := validateVisionJPEG(jpegBytes, assembly.capturedAtMS, now); reason != "" {
		s.markVisionDropped()
		return newVisionAck(chunk.frameID, "dropped", reason, now), nil
	}

	snapshot := &visionSnapshot{
		frameID:      assembly.frameID,
		capturedAtMS: assembly.capturedAtMS,
		receivedAt:   now,
		jpeg:         jpegBytes,
		width:        visionExpectedWidth,
		height:       visionExpectedHeight,
	}
	s.vision.mu.Lock()
	s.vision.pruneLocked(now)
	if s.vision.latest != nil && s.vision.latest.capturedAtMS > snapshot.capturedAtMS {
		s.vision.dropped++
		s.vision.mu.Unlock()
		return newVisionAck(chunk.frameID, "dropped", "superseded_by_newer_snapshot", now), nil
	}
	s.vision.latest = snapshot
	s.vision.accepted++
	s.vision.mu.Unlock()
	return newVisionAck(chunk.frameID, "accepted", "", now), snapshot
}

func validateVisionJPEG(data []byte, capturedAtMS int64, now time.Time) string {
	if len(data) == 0 || len(data) > visionMaxImageBytes {
		return "image_size_out_of_range"
	}
	captureTime := time.UnixMilli(capturedAtMS)
	if captureTime.After(now.Add(visionMaxFutureClockSkew)) {
		return "capture_time_in_future"
	}
	if now.Sub(captureTime) > visionUsableAge {
		return "snapshot_too_old"
	}
	decoded, err := jpeg.Decode(bytes.NewReader(data))
	if err != nil {
		return "invalid_jpeg"
	}
	bounds := decoded.Bounds()
	if bounds.Dx() != visionExpectedWidth || bounds.Dy() != visionExpectedHeight {
		return "unexpected_dimensions"
	}
	return ""
}

func (s *gatewaySession) latestUsableVisionSnapshot(now time.Time) (visionSnapshot, bool) {
	if now.IsZero() {
		now = time.Now()
	}
	s.vision.mu.Lock()
	defer s.vision.mu.Unlock()
	s.vision.pruneLocked(now)
	if s.vision.latest == nil {
		return visionSnapshot{}, false
	}
	captureTime := time.UnixMilli(s.vision.latest.capturedAtMS)
	if captureTime.After(now.Add(visionMaxFutureClockSkew)) || now.Sub(captureTime) > visionUsableAge {
		return visionSnapshot{}, false
	}
	snapshot := *s.vision.latest
	snapshot.jpeg = append([]byte(nil), snapshot.jpeg...)
	return snapshot, true
}

func (s *gatewaySession) visionStatusSnapshot(now time.Time) visionSessionStatus {
	if now.IsZero() {
		now = time.Now()
	}
	s.vision.mu.Lock()
	defer s.vision.mu.Unlock()
	s.vision.pruneLocked(now)
	status := visionSessionStatus{
		InflightFrames: len(s.vision.inflight),
		Accepted:       s.vision.accepted,
		Dropped:        s.vision.dropped,
	}
	if s.vision.latest == nil {
		return status
	}
	ageMS := now.UnixMilli() - s.vision.latest.capturedAtMS
	status.Cached = true
	status.Usable = ageMS >= -visionMaxFutureClockSkew.Milliseconds() &&
		ageMS <= visionUsableAge.Milliseconds()
	status.FrameID = strconv.FormatUint(s.vision.latest.frameID, 10)
	status.AgeMS = ageMS
	status.Bytes = len(s.vision.latest.jpeg)
	return status
}

func (s *gatewaySession) markVisionDropped() {
	s.vision.mu.Lock()
	s.vision.dropped++
	s.vision.mu.Unlock()
}

func (state *visionSessionState) pruneLocked(now time.Time) {
	for frameID, assembly := range state.inflight {
		if now.Sub(assembly.startedAt) > visionAssemblyTTL {
			delete(state.inflight, frameID)
			state.dropped++
		}
	}
	if state.latest != nil && now.Sub(state.latest.receivedAt) > visionCacheTTL {
		state.latest = nil
	}
}

func (state *visionSessionState) dropOldestAssemblyLocked() {
	var oldestID uint64
	var oldestAt time.Time
	for frameID, assembly := range state.inflight {
		if oldestAt.IsZero() || assembly.startedAt.Before(oldestAt) {
			oldestID = frameID
			oldestAt = assembly.startedAt
		}
	}
	if !oldestAt.IsZero() {
		delete(state.inflight, oldestID)
	}
}

func newVisionAck(frameID uint64, status string, reason string, now time.Time) *visionSnapshotAck {
	ack := &visionSnapshotAck{
		Type:         "vision_snapshot_ack",
		Status:       status,
		Reason:       reason,
		ReceivedAtMS: now.UnixMilli(),
	}
	if frameID != 0 {
		ack.FrameID = strconv.FormatUint(frameID, 10)
	}
	return ack
}
