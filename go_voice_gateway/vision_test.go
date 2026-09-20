package main

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/jpeg"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestParseVisionChunkRejectsInvalidHeader(t *testing.T) {
	if _, err := parseVisionChunk([]byte("not-a-vision-frame")); err == nil {
		t.Fatal("expected short header to fail")
	}

	data := make([]byte, visionChunkHeaderBytes+1)
	copy(data[:4], []byte("NOPE"))
	if _, err := parseVisionChunk(data); err == nil || err.Error() != "bad_magic" {
		t.Fatalf("expected bad_magic, got %v", err)
	}
}

func TestParseVisionChunkRejectsProtocolBoundaries(t *testing.T) {
	valid := func() []byte {
		data := make([]byte, visionChunkHeaderBytes+1)
		copy(data[:4], visionChunkMagic[:])
		data[4] = visionProtocolVersion
		binary.BigEndian.PutUint16(data[6:8], visionChunkHeaderBytes)
		binary.BigEndian.PutUint64(data[8:16], 1)
		binary.BigEndian.PutUint64(data[16:24], 1_900_000_000_000)
		binary.BigEndian.PutUint32(data[24:28], 1)
		binary.BigEndian.PutUint16(data[28:30], 0)
		binary.BigEndian.PutUint16(data[30:32], 1)
		data[visionChunkHeaderBytes] = 0xff
		return data
	}
	tests := []struct {
		name   string
		mutate func([]byte) []byte
		reason string
	}{
		{"unsupported_version", func(data []byte) []byte { data[4]++; return data }, "unsupported_version"},
		{"unsupported_flags", func(data []byte) []byte { data[5] = 1; return data }, "unsupported_flags"},
		{"bad_header_length", func(data []byte) []byte {
			binary.BigEndian.PutUint16(data[6:8], visionChunkHeaderBytes-1)
			return data
		}, "bad_header_length"},
		{"missing_frame_id", func(data []byte) []byte {
			binary.BigEndian.PutUint64(data[8:16], 0)
			return data
		}, "missing_frame_id"},
		{"bad_capture_time", func(data []byte) []byte {
			binary.BigEndian.PutUint64(data[16:24], ^uint64(0))
			return data
		}, "bad_capture_time"},
		{"zero_total_bytes", func(data []byte) []byte {
			binary.BigEndian.PutUint32(data[24:28], 0)
			return data
		}, "image_size_out_of_range"},
		{"oversized_total_bytes", func(data []byte) []byte {
			binary.BigEndian.PutUint32(data[24:28], visionMaxImageBytes+1)
			return data
		}, "image_size_out_of_range"},
		{"zero_chunk_count", func(data []byte) []byte {
			binary.BigEndian.PutUint16(data[30:32], 0)
			return data
		}, "chunk_count_out_of_range"},
		{"too_many_chunks", func(data []byte) []byte {
			binary.BigEndian.PutUint16(data[30:32], visionMaxChunks+1)
			return data
		}, "chunk_count_out_of_range"},
		{"chunk_index_out_of_range", func(data []byte) []byte {
			binary.BigEndian.PutUint16(data[28:30], 1)
			return data
		}, "chunk_index_out_of_range"},
		{"empty_payload", func(data []byte) []byte {
			return data[:visionChunkHeaderBytes]
		}, "chunk_size_out_of_range"},
		{"oversized_payload", func(data []byte) []byte {
			return append(data[:visionChunkHeaderBytes], make([]byte, visionChunkPayloadBytes+1)...)
		}, "chunk_size_out_of_range"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := parseVisionChunk(test.mutate(valid()))
			if err == nil || err.Error() != test.reason {
				t.Fatalf("error = %v, want %q", err, test.reason)
			}
		})
	}
}

func TestVisionSnapshotHTTPRejectsInvalidRequests(t *testing.T) {
	tests := []struct {
		name       string
		token      string
		method     string
		target     string
		auth       string
		wantStatus int
	}{
		{"token_not_configured", "", http.MethodGet, "/internal/vision/snapshot?session_id=s1", "", http.StatusServiceUnavailable},
		{"wrong_token", "vision-secret", http.MethodGet, "/internal/vision/snapshot?session_id=s1", "Bearer wrong", http.StatusUnauthorized},
		{"wrong_method", "vision-secret", http.MethodPost, "/internal/vision/snapshot?session_id=s1", "Bearer vision-secret", http.StatusMethodNotAllowed},
		{"missing_session_id", "vision-secret", http.MethodGet, "/internal/vision/snapshot", "Bearer vision-secret", http.StatusBadRequest},
		{"unknown_session", "vision-secret", http.MethodGet, "/internal/vision/snapshot?session_id=missing", "Bearer vision-secret", http.StatusNotFound},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := &Server{
				cfg:      Config{VisionInternalToken: test.token},
				sessions: newGatewaySessionRegistry(),
			}
			request := httptest.NewRequest(test.method, test.target, nil)
			if test.auth != "" {
				request.Header.Set("Authorization", test.auth)
			}
			recorder := httptest.NewRecorder()
			server.handleVisionSnapshot(recorder, request)
			if recorder.Code != test.wantStatus {
				t.Fatalf("status = %d, want %d, body=%s", recorder.Code, test.wantStatus, recorder.Body.String())
			}
		})
	}
}

func TestVisionChunksAssembleOutOfOrderAndCacheLatest(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	chunks := buildVisionChunks(t, 7, now.UnixMilli()-250, jpegBytes, 3)
	session := &gatewaySession{id: "vision_test"}

	for _, index := range []int{2, 0} {
		ack, snapshot := session.acceptVisionChunk(chunks[index], now)
		if ack != nil || snapshot != nil {
			t.Fatalf("chunk %d completed frame too early: ack=%+v snapshot=%+v", index, ack, snapshot)
		}
	}
	ack, snapshot := session.acceptVisionChunk(chunks[1], now)
	if ack == nil || ack.Status != "accepted" || ack.FrameID != "7" {
		t.Fatalf("unexpected ack: %+v", ack)
	}
	if snapshot == nil || !bytes.Equal(snapshot.jpeg, jpegBytes) {
		t.Fatal("accepted snapshot does not match source JPEG")
	}

	latest, ok := session.latestUsableVisionSnapshot(now)
	if !ok || !bytes.Equal(latest.jpeg, jpegBytes) {
		t.Fatal("latest usable snapshot missing")
	}
	latest.jpeg[0] ^= 0xff
	again, ok := session.latestUsableVisionSnapshot(now)
	if !ok || bytes.Equal(latest.jpeg, again.jpeg) {
		t.Fatal("latest snapshot must be returned as a defensive copy")
	}

	status := session.visionStatusSnapshot(now)
	if !status.Cached || !status.Usable || status.Accepted != 1 || status.Dropped != 0 {
		t.Fatalf("unexpected vision status: %+v", status)
	}
}

func TestVisionSnapshotHTTPRequiresInternalBearerToken(t *testing.T) {
	server := &Server{
		cfg:      Config{VisionInternalToken: "vision-secret"},
		sessions: newGatewaySessionRegistry(),
	}
	request := httptest.NewRequest(http.MethodGet, "/internal/vision/snapshot?session_id=s1", nil)
	recorder := httptest.NewRecorder()

	server.handleVisionSnapshot(recorder, request)

	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusUnauthorized)
	}
}

func TestVisionSnapshotHTTPReturnsLatestUsableJPEG(t *testing.T) {
	now := time.Now()
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	session := &gatewaySession{id: "vision-http"}
	session.vision.latest = &visionSnapshot{
		frameID:      42,
		capturedAtMS: now.Add(-250 * time.Millisecond).UnixMilli(),
		receivedAt:   now,
		jpeg:         jpegBytes,
		width:        visionExpectedWidth,
		height:       visionExpectedHeight,
	}
	server := &Server{
		cfg:      Config{VisionInternalToken: "vision-secret"},
		sessions: newGatewaySessionRegistry(),
	}
	server.sessions.Add(session)
	request := httptest.NewRequest(
		http.MethodGet,
		"/internal/vision/snapshot?session_id=vision-http",
		nil,
	)
	request.Header.Set("Authorization", "Bearer vision-secret")
	recorder := httptest.NewRecorder()

	server.handleVisionSnapshot(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", recorder.Code, recorder.Body.String())
	}
	if recorder.Header().Get("Content-Type") != "image/jpeg" {
		t.Fatalf("content type = %q", recorder.Header().Get("Content-Type"))
	}
	if recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("cache control = %q", recorder.Header().Get("Cache-Control"))
	}
	if recorder.Header().Get("X-Vision-Frame-ID") != "42" {
		t.Fatalf("frame id = %q", recorder.Header().Get("X-Vision-Frame-ID"))
	}
	if !bytes.Equal(recorder.Body.Bytes(), jpegBytes) {
		t.Fatal("response JPEG does not match cached snapshot")
	}
}

func TestVisionSnapshotHTTPResolvesPythonGatewaySessionAlias(t *testing.T) {
	now := time.Now()
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	session := &gatewaySession{id: "rtc-client-session"}
	session.vision.latest = &visionSnapshot{
		frameID:      7,
		capturedAtMS: now.Add(-100 * time.Millisecond).UnixMilli(),
		receivedAt:   now,
		jpeg:         jpegBytes,
		width:        visionExpectedWidth,
		height:       visionExpectedHeight,
	}
	bridge := &PythonGatewayBridgeProcessor{}
	bridge.bindPythonSessionAlias("rtc-client-session", "python-session")
	server := &Server{
		cfg:           Config{VisionInternalToken: "vision-secret"},
		sessions:      newGatewaySessionRegistry(),
		pythonGateway: bridge,
	}
	server.sessions.Add(session)
	request := httptest.NewRequest(
		http.MethodGet,
		"/internal/vision/snapshot?session_id=python-session",
		nil,
	)
	request.Header.Set("Authorization", "Bearer vision-secret")
	recorder := httptest.NewRecorder()

	server.handleVisionSnapshot(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", recorder.Code, recorder.Body.String())
	}
	if !bytes.Equal(recorder.Body.Bytes(), jpegBytes) {
		t.Fatal("aliased response does not match cached JPEG")
	}
}

func TestVisionSnapshotHTTPRejectsStaleSnapshot(t *testing.T) {
	now := time.Now()
	session := &gatewaySession{id: "vision-stale"}
	session.vision.latest = &visionSnapshot{
		frameID:      9,
		capturedAtMS: now.Add(-visionUsableAge - time.Second).UnixMilli(),
		receivedAt:   now,
		jpeg:         testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight),
	}
	server := &Server{
		cfg:      Config{VisionInternalToken: "vision-secret"},
		sessions: newGatewaySessionRegistry(),
	}
	server.sessions.Add(session)
	request := httptest.NewRequest(
		http.MethodGet,
		"/internal/vision/snapshot?session_id=vision-stale",
		nil,
	)
	request.Header.Set("Authorization", "Bearer vision-secret")
	recorder := httptest.NewRecorder()

	server.handleVisionSnapshot(recorder, request)

	if recorder.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want %d", recorder.Code, http.StatusNotFound)
	}
}

func TestVisionRejectsStaleSnapshot(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	chunks := buildVisionChunks(
		t,
		9,
		now.Add(-visionUsableAge-time.Millisecond).UnixMilli(),
		jpegBytes,
		2,
	)
	session := &gatewaySession{id: "vision_test"}

	ack, snapshot := acceptTestVisionFrame(t, session, chunks, now)

	if snapshot != nil || ack == nil || ack.Status != "dropped" || ack.Reason != "snapshot_too_old" {
		t.Fatalf("unexpected stale snapshot result: ack=%+v snapshot=%+v", ack, snapshot)
	}
	if session.visionStatusSnapshot(now).Dropped != 1 {
		t.Fatal("stale snapshot should increment dropped counter")
	}
}

func TestVisionRejectsUnexpectedDimensions(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, 320, 180)
	chunk := buildVisionChunks(t, 11, now.UnixMilli(), jpegBytes, 1)[0]
	session := &gatewaySession{id: "vision_test"}

	ack, snapshot := session.acceptVisionChunk(chunk, now)

	if snapshot != nil || ack == nil || ack.Reason != "unexpected_dimensions" {
		t.Fatalf("unexpected dimension result: ack=%+v snapshot=%+v", ack, snapshot)
	}
}

func TestVisionUsableAgeAndCacheTTLHaveSeparateBoundaries(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	chunks := buildVisionChunks(t, 13, now.UnixMilli(), jpegBytes, 2)
	session := &gatewaySession{id: "vision_test"}
	ack, snapshot := acceptTestVisionFrame(t, session, chunks, now)
	if ack == nil || ack.Status != "accepted" || snapshot == nil {
		t.Fatalf("snapshot setup failed: ack=%+v snapshot=%+v", ack, snapshot)
	}

	notUsableAt := now.Add(visionUsableAge + time.Millisecond)
	if _, ok := session.latestUsableVisionSnapshot(notUsableAt); ok {
		t.Fatal("snapshot older than usable age must not be returned")
	}
	status := session.visionStatusSnapshot(notUsableAt)
	if !status.Cached || status.Usable {
		t.Fatalf("snapshot should remain cached but unusable: %+v", status)
	}

	expiredAt := now.Add(visionCacheTTL + time.Millisecond)
	status = session.visionStatusSnapshot(expiredAt)
	if status.Cached || status.Usable || status.Bytes != 0 {
		t.Fatalf("snapshot should be removed after cache TTL: %+v", status)
	}
}

func TestVisionNewerSnapshotCannotBeReplacedByOlderFrame(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	session := &gatewaySession{id: "vision_test"}

	newer := buildVisionChunks(t, 15, now.UnixMilli(), jpegBytes, 2)
	ack, _ := acceptTestVisionFrame(t, session, newer, now)
	if ack == nil || ack.Status != "accepted" {
		t.Fatalf("newer snapshot setup failed: %+v", ack)
	}

	older := buildVisionChunks(t, 14, now.Add(-time.Second).UnixMilli(), jpegBytes, 2)
	ack, snapshot := acceptTestVisionFrame(t, session, older, now)
	if snapshot != nil || ack == nil || ack.Reason != "superseded_by_newer_snapshot" {
		t.Fatalf("older snapshot should be superseded: ack=%+v snapshot=%+v", ack, snapshot)
	}
	latest, ok := session.latestUsableVisionSnapshot(now)
	if !ok || latest.frameID != 15 {
		t.Fatalf("newer snapshot should remain cached: %+v", latest)
	}
}

func TestVisionDuplicateChunkIsIdempotent(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	chunks := buildVisionChunks(t, 17, now.UnixMilli(), jpegBytes, 2)
	session := &gatewaySession{id: "vision_test"}

	if ack, _ := session.acceptVisionChunk(chunks[0], now); ack != nil {
		t.Fatalf("first chunk should not ack: %+v", ack)
	}
	if ack, _ := session.acceptVisionChunk(chunks[0], now); ack != nil {
		t.Fatalf("identical duplicate should not ack: %+v", ack)
	}
	ack, snapshot := session.acceptVisionChunk(chunks[1], now)
	if ack == nil || ack.Status != "accepted" || snapshot == nil {
		t.Fatalf("frame should complete after unique second chunk: ack=%+v", ack)
	}
}

func TestVisionRejectsInvalidCompletedFrames(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	validJPEG := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)
	tests := []struct {
		name         string
		frameID      uint64
		capturedAtMS int64
		payload      []byte
		reason       string
	}{
		{"future_capture", 21, now.Add(visionMaxFutureClockSkew + time.Millisecond).UnixMilli(), validJPEG, "capture_time_in_future"},
		{"invalid_jpeg", 22, now.UnixMilli(), []byte("not-a-jpeg"), "invalid_jpeg"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			session := &gatewaySession{id: "vision_test"}
			chunkCount := 1
			if len(test.payload) > visionChunkPayloadBytes {
				chunkCount = 2
			}
			chunks := buildVisionChunks(t, test.frameID, test.capturedAtMS, test.payload, chunkCount)
			ack, snapshot := acceptTestVisionFrame(t, session, chunks, now)
			if snapshot != nil || ack == nil || ack.Status != "dropped" || ack.Reason != test.reason {
				t.Fatalf("ack=%+v snapshot=%+v, want reason=%s", ack, snapshot, test.reason)
			}
		})
	}
}

func TestVisionRejectsConflictingAndInconsistentChunks(t *testing.T) {
	now := time.UnixMilli(1_900_000_000_000)
	jpegBytes := testVisionJPEG(t, visionExpectedWidth, visionExpectedHeight)

	t.Run("conflicting_duplicate", func(t *testing.T) {
		session := &gatewaySession{id: "vision_test"}
		chunks := buildVisionChunks(t, 23, now.UnixMilli(), jpegBytes, 2)
		if ack, _ := session.acceptVisionChunk(chunks[0], now); ack != nil {
			t.Fatalf("first chunk should remain inflight: %+v", ack)
		}
		conflicting := append([]byte(nil), chunks[0]...)
		conflicting[len(conflicting)-1] ^= 0xff
		ack, snapshot := session.acceptVisionChunk(conflicting, now)
		if snapshot != nil || ack == nil || ack.Reason != "conflicting_duplicate_chunk" {
			t.Fatalf("unexpected result: ack=%+v snapshot=%+v", ack, snapshot)
		}
	})

	t.Run("inconsistent_metadata", func(t *testing.T) {
		session := &gatewaySession{id: "vision_test"}
		chunks := buildVisionChunks(t, 24, now.UnixMilli(), jpegBytes, 2)
		if ack, _ := session.acceptVisionChunk(chunks[0], now); ack != nil {
			t.Fatalf("first chunk should remain inflight: %+v", ack)
		}
		inconsistent := append([]byte(nil), chunks[1]...)
		binary.BigEndian.PutUint64(inconsistent[16:24], uint64(now.Add(-time.Second).UnixMilli()))
		ack, snapshot := session.acceptVisionChunk(inconsistent, now)
		if snapshot != nil || ack == nil || ack.Reason != "inconsistent_frame_metadata" {
			t.Fatalf("unexpected result: ack=%+v snapshot=%+v", ack, snapshot)
		}
	})
}

func testVisionJPEG(t *testing.T, width int, height int) []byte {
	t.Helper()
	source := image.NewRGBA(image.Rect(0, 0, width, height))
	for y := 0; y < height; y++ {
		for x := 0; x < width; x++ {
			source.SetRGBA(x, y, color.RGBA{
				R: uint8((x * 3) % 256),
				G: uint8((y * 5) % 256),
				B: uint8((x + y) % 256),
				A: 255,
			})
		}
	}
	var output bytes.Buffer
	if err := jpeg.Encode(&output, source, &jpeg.Options{Quality: 80}); err != nil {
		t.Fatalf("encode test JPEG: %v", err)
	}
	if output.Len() > visionMaxImageBytes {
		t.Fatalf("test JPEG exceeds protocol limit: %d", output.Len())
	}
	return output.Bytes()
}

func buildVisionChunks(
	t *testing.T,
	frameID uint64,
	capturedAtMS int64,
	payload []byte,
	chunkCount int,
) [][]byte {
	t.Helper()
	if chunkCount <= 0 || chunkCount > visionMaxChunks || len(payload) < chunkCount {
		t.Fatalf("invalid test chunk count %d for %d bytes", chunkCount, len(payload))
	}
	chunks := make([][]byte, 0, chunkCount)
	offset := 0
	for index := 0; index < chunkCount; index++ {
		remaining := len(payload) - offset
		partSize := (remaining + (chunkCount - index) - 1) / (chunkCount - index)
		part := payload[offset : offset+partSize]
		offset += partSize

		message := make([]byte, visionChunkHeaderBytes+len(part))
		copy(message[:4], visionChunkMagic[:])
		message[4] = visionProtocolVersion
		binary.BigEndian.PutUint16(message[6:8], visionChunkHeaderBytes)
		binary.BigEndian.PutUint64(message[8:16], frameID)
		binary.BigEndian.PutUint64(message[16:24], uint64(capturedAtMS))
		binary.BigEndian.PutUint32(message[24:28], uint32(len(payload)))
		binary.BigEndian.PutUint16(message[28:30], uint16(index))
		binary.BigEndian.PutUint16(message[30:32], uint16(chunkCount))
		copy(message[visionChunkHeaderBytes:], part)
		chunks = append(chunks, message)
	}
	return chunks
}

func acceptTestVisionFrame(
	t *testing.T,
	session *gatewaySession,
	chunks [][]byte,
	now time.Time,
) (*visionSnapshotAck, *visionSnapshot) {
	t.Helper()
	var ack *visionSnapshotAck
	var snapshot *visionSnapshot
	for index, chunk := range chunks {
		ack, snapshot = session.acceptVisionChunk(chunk, now)
		if index < len(chunks)-1 && (ack != nil || snapshot != nil) {
			t.Fatalf("frame completed at chunk %d of %d", index+1, len(chunks))
		}
	}
	return ack, snapshot
}
