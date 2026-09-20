package main

import (
	"log"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	defaultASRPrototypeQueueSize     = 256
	defaultASRPrototypeEndGraceMS    = 120
	maxCompletedASRPrototypeSegments = 32
	maxTurnCandidateObservations     = 64
	asrPrototypeRetainedAudioSeconds = 15
	asrPrototypeRetainedOpusFrameMS  = 20
	maxASRPrototypeEncodedPackets    = asrPrototypeRetainedAudioSeconds * 1000 / asrPrototypeRetainedOpusFrameMS
	asrPrototypeStateRecording       = "recording"
	asrPrototypeStateEnding          = "ending"
	asrPrototypeStateCompleted       = "completed"
	asrPrototypeStateCancelled       = "cancelled"
)

type ASRPrototypeSink struct {
	queue                  chan CanonicalVoiceEvent
	handoffs               ASREncodedAudioHandoffSink
	candidateHandoffs      ASREncodedAudioHandoffSink
	bargeInHandoffs        ASREncodedAudioHandoffSink
	candidateSnapshotGrace time.Duration
	endGrace               time.Duration
	logger                 *log.Logger

	enqueueMu sync.Mutex
	closed    bool

	mu                       sync.Mutex
	active                   map[asrPrototypeSegmentKey]*ASRPrototypeSegment
	activeBySession          map[string]asrPrototypeSegmentKey
	pendingEnds              map[asrPrototypeSegmentKey]asrPrototypePendingEnd
	pendingEndSeq            uint64
	completed                []ASRPrototypeSegment
	droppedEventCount        uint64
	handoffBuildErrorCount   uint64
	candidateBuildErrorCount uint64
	turnCandidates           []TurnCandidateObservation

	wg sync.WaitGroup
}

type asrPrototypeSegmentKey struct {
	sessionID   string
	utteranceID string
}

type asrPrototypePendingEnd struct {
	timer *time.Timer
	token uint64
}

type ASRPrototypeSegment struct {
	SessionID             string
	BotID                 string
	TraceID               string
	UtteranceID           string
	State                 string
	StartedAt             time.Time
	EndedAt               time.Time
	AudioPackets          uint64
	AudioPayloadBytes     uint64
	LastPacketCount       uint64
	TrackID               string
	StreamID              string
	Reason                string
	RoundID               string
	PlaybackID            string
	EncodedPackets        []ASRPrototypeEncodedPacket
	EncodedPayloadBytes   uint64
	DroppedEncodedPackets uint64
	DuplicatePackets      uint64
	SequenceGaps          uint64
	MissingPackets        uint64
	TimestampRegressions  uint64

	seenSequenceNumbers map[uint16]struct{}
}

type ASRPrototypeEncodedPacket struct {
	PacketCount    uint64
	Payload        []byte
	PayloadBytes   int
	TrackID        string
	StreamID       string
	HasRTPMetadata bool
	SequenceNumber uint16
	Timestamp      uint32
}

type ASRPrototypeSnapshot struct {
	Active                   []ASRPrototypeSegment
	Completed                []ASRPrototypeSegment
	DroppedEventCount        uint64
	HandoffBuildErrorCount   uint64
	CandidateBuildErrorCount uint64
	TurnCandidates           []TurnCandidateObservation
}

type TurnCandidateObservation struct {
	SessionID            string
	TraceID              string
	UtteranceID          string
	CandidateSeq         uint64
	SpeechEpoch          uint64
	AudioWatermark       uint64
	SilenceMS            uint64
	Shadow               bool
	SegmentActive        bool
	ObservedAudioPackets uint64
	RetainedPackets      int
}

func NewASRPrototypeSink(queueSize int) *ASRPrototypeSink {
	return NewASRPrototypeSinkWithHandoffSink(queueSize, nil)
}

func NewASRPrototypeSinkWithHandoffSink(queueSize int, handoffs ASREncodedAudioHandoffSink) *ASRPrototypeSink {
	return NewASRPrototypeSinkWithOptions(queueSize, handoffs, 0)
}

func NewASRPrototypeSinkWithOptions(queueSize int, handoffs ASREncodedAudioHandoffSink, endGrace time.Duration) *ASRPrototypeSink {
	if queueSize <= 0 {
		queueSize = defaultASRPrototypeQueueSize
	}
	if endGrace < 0 {
		endGrace = 0
	}
	sink := &ASRPrototypeSink{
		queue:           make(chan CanonicalVoiceEvent, queueSize),
		handoffs:        handoffs,
		endGrace:        endGrace,
		active:          make(map[asrPrototypeSegmentKey]*ASRPrototypeSegment),
		activeBySession: make(map[string]asrPrototypeSegmentKey),
		pendingEnds:     make(map[asrPrototypeSegmentKey]asrPrototypePendingEnd),
	}
	sink.wg.Add(1)
	go sink.run()
	return sink
}

func (s *ASRPrototypeSink) SetLogger(logger *log.Logger) {
	if s == nil {
		return
	}
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	s.logger = logger
}

func (s *ASRPrototypeSink) SetTurnCandidateHandoffSink(handoffs ASREncodedAudioHandoffSink) {
	if s == nil {
		return
	}
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	s.candidateHandoffs = handoffs
}

func (s *ASRPrototypeSink) SetBargeInHandoffSink(handoffs ASREncodedAudioHandoffSink) {
	if s == nil {
		return
	}
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	s.bargeInHandoffs = handoffs
}

func (s *ASRPrototypeSink) SetTurnCandidateSnapshotGrace(grace time.Duration) {
	if s == nil {
		return
	}
	if grace < 0 {
		grace = 0
	}
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	s.candidateSnapshotGrace = grace
}

func (s *ASRPrototypeSink) HandleVoiceEvent(event CanonicalVoiceEvent) {
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	if s.closed {
		return
	}
	event = cloneCanonicalVoiceEventPayload(event)
	select {
	case s.queue <- event:
	default:
		s.mu.Lock()
		s.droppedEventCount++
		droppedTotal := s.droppedEventCount
		s.mu.Unlock()
		if s.logger != nil {
			s.logger.Printf(
				"asr_prototype_event_drop reason=queue_full session=%s trace_id=%s type=%s utterance_id=%s queued=%d capacity=%d dropped_total=%d",
				printable(event.SessionID),
				printable(event.TraceID),
				printable(event.Type),
				printable(event.UtteranceID),
				len(s.queue),
				cap(s.queue),
				droppedTotal,
			)
		}
	}
}

func (s *ASRPrototypeSink) Close() {
	s.enqueueMu.Lock()
	if !s.closed {
		s.closed = true
		close(s.queue)
	}
	s.enqueueMu.Unlock()
	s.wg.Wait()
	s.stopPendingEndTimers()
}

func (s *ASRPrototypeSink) Snapshot() ASRPrototypeSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()

	active := make([]ASRPrototypeSegment, 0, len(s.active))
	for _, segment := range s.active {
		active = append(active, copyASRPrototypeSegment(*segment))
	}
	sort.Slice(active, func(i, j int) bool {
		if active[i].SessionID != active[j].SessionID {
			return active[i].SessionID < active[j].SessionID
		}
		if !active[i].StartedAt.Equal(active[j].StartedAt) {
			return active[i].StartedAt.Before(active[j].StartedAt)
		}
		return active[i].UtteranceID < active[j].UtteranceID
	})
	completed := make([]ASRPrototypeSegment, len(s.completed))
	for i, segment := range s.completed {
		completed[i] = copyASRPrototypeSegment(segment)
	}
	return ASRPrototypeSnapshot{
		Active:                   active,
		Completed:                completed,
		DroppedEventCount:        s.droppedEventCount,
		HandoffBuildErrorCount:   s.handoffBuildErrorCount,
		CandidateBuildErrorCount: s.candidateBuildErrorCount,
		TurnCandidates:           append([]TurnCandidateObservation(nil), s.turnCandidates...),
	}
}

func (s *ASRPrototypeSink) run() {
	defer s.wg.Done()
	for event := range s.queue {
		s.handle(event)
	}
}

func (s *ASRPrototypeSink) handle(event CanonicalVoiceEvent) {
	var handoffs []ASREncodedAudioHandoff
	var candidateHandoffs []ASREncodedAudioHandoff
	var bargeInHandoffs []ASREncodedAudioHandoff

	s.mu.Lock()

	switch event.Type {
	case "audio_start", "barge_in_start":
		key := asrPrototypeKey(event.SessionID, event.UtteranceID)
		s.stopPendingEndLocked(key)
		s.active[key] = &ASRPrototypeSegment{
			SessionID:   event.SessionID,
			BotID:       event.BotID,
			TraceID:     traceIDForVoiceTurn(event.TraceID, event.SessionID, event.UtteranceID),
			UtteranceID: key.utteranceID,
			State:       asrPrototypeStateRecording,
			StartedAt:   canonicalEventTime(event),
			RoundID:     event.RoundID,
			PlaybackID:  event.PlaybackID,
		}
		s.activeBySession[event.SessionID] = key
	case canonicalEventAudioPacket:
		key, ok := s.activeKeyForPacketLocked(event)
		segment := s.active[key]
		if segment == nil {
			key = asrPrototypeKey(event.SessionID, event.UtteranceID)
			segment = &ASRPrototypeSegment{
				SessionID:   event.SessionID,
				BotID:       event.BotID,
				TraceID:     traceIDForVoiceTurn(event.TraceID, event.SessionID, event.UtteranceID),
				UtteranceID: key.utteranceID,
				State:       asrPrototypeStateRecording,
				StartedAt:   canonicalEventTime(event),
			}
			s.active[key] = segment
			_, hasSessionActive := s.activeBySession[event.SessionID]
			if !ok || key.utteranceID == "" || !hasSessionActive {
				s.activeBySession[event.SessionID] = key
			}
		}
		segment.AudioPackets++
		segment.AudioPayloadBytes += uint64(nonNegativeInt(event.LastPayloadBytes))
		segment.LastPacketCount = event.PacketCount
		if event.TrackID != "" {
			segment.TrackID = event.TrackID
		}
		if event.StreamID != "" {
			segment.StreamID = event.StreamID
		}
		if event.BotID != "" {
			segment.BotID = event.BotID
		}
		if event.TraceID != "" {
			segment.TraceID = traceIDForVoiceTurn(event.TraceID, event.SessionID, event.UtteranceID)
		}
		if len(event.rtpPayload) > 0 {
			segment.appendEncodedPacket(event)
		}
	case "audio_end":
		key, segment := s.activeKeyAndSegmentForControlLocked(event)
		if segment != nil && event.BotID != "" {
			segment.BotID = event.BotID
		}
		if segment != nil && event.TraceID != "" {
			segment.TraceID = traceIDForVoiceTurn(event.TraceID, event.SessionID, event.UtteranceID)
		}
		if segment != nil && s.endGrace > 0 {
			s.scheduleSegmentFinishLocked(key, segment, event.UtteranceID, event.Reason, canonicalEventTime(event))
		} else if handoff, ok := s.finishSegmentLocked(key, segment, event.UtteranceID, asrPrototypeStateCompleted, event.Reason, canonicalEventTime(event)); ok {
			handoffs = append(handoffs, handoff)
		}
	case "turn_candidate":
		_, segment := s.activeKeyAndSegmentForControlLocked(event)
		if segment != nil && s.shouldDeferTurnCandidateSnapshotLocked(segment, event) {
			deferredEvent := event
			deferredEvent.candidateSnapshotReady = true
			grace := s.candidateSnapshotGrace
			time.AfterFunc(grace, func() {
				s.HandleVoiceEvent(deferredEvent)
			})
			break
		}
		var candidateSegment ASRPrototypeSegment
		if segment != nil {
			candidateSegment = candidateSegmentAtWatermark(*segment, event.AudioWatermark)
		}
		observation := TurnCandidateObservation{
			SessionID:      event.SessionID,
			TraceID:        event.TraceID,
			UtteranceID:    event.UtteranceID,
			CandidateSeq:   event.CandidateSeq,
			SpeechEpoch:    event.SpeechEpoch,
			AudioWatermark: event.AudioWatermark,
			SilenceMS:      event.SilenceMS,
			Shadow:         event.Shadow,
			SegmentActive:  segment != nil && segment.State == asrPrototypeStateRecording,
		}
		if segment != nil {
			observation.ObservedAudioPackets = candidateSegment.AudioPackets
			observation.RetainedPackets = len(candidateSegment.EncodedPackets)
		}
		s.turnCandidates = append(s.turnCandidates, observation)
		if len(s.turnCandidates) > maxTurnCandidateObservations {
			s.turnCandidates = s.turnCandidates[len(s.turnCandidates)-maxTurnCandidateObservations:]
		}
		if observation.SegmentActive && s.candidateHandoffs != nil {
			handoff, err := NewTurnCandidateAudioHandoffFromSegment(candidateSegment, event)
			if err != nil {
				s.candidateBuildErrorCount++
			} else {
				candidateHandoffs = append(candidateHandoffs, handoff)
			}
		}
	case "barge_in_probe":
		_, segment := s.activeKeyAndSegmentForControlLocked(event)
		var candidateSegment ASRPrototypeSegment
		if segment != nil {
			candidateSegment = candidateSegmentAtWatermark(*segment, event.AudioWatermark)
		}
		if segment != nil && segment.State == asrPrototypeStateRecording && s.bargeInHandoffs != nil {
			handoff, err := NewBargeInAudioHandoffFromSegment(candidateSegment, event)
			if err != nil {
				s.candidateBuildErrorCount++
			} else {
				bargeInHandoffs = append(bargeInHandoffs, handoff)
			}
		}
	case "turn_commit":
		key, segment := s.activeKeyAndSegmentForControlLocked(event)
		if segment != nil {
			s.stopPendingEndLocked(key)
			_, _ = s.finishSegmentLocked(
				key,
				segment,
				event.UtteranceID,
				asrPrototypeStateCompleted,
				firstNonEmpty(event.Reason, "turn_gate_active_commit"),
				canonicalEventTime(event),
			)
		}
	case "audio_cancel":
		key, segment := s.activeKeyAndSegmentForControlLocked(event)
		s.finishSegmentLocked(key, segment, event.UtteranceID, asrPrototypeStateCancelled, event.Reason, canonicalEventTime(event))
	case "barge_in_cancel":
		key, segment := s.activeKeyAndSegmentForControlLocked(event)
		s.finishSegmentLocked(key, segment, event.UtteranceID, asrPrototypeStateCancelled, firstNonEmpty(event.Reason, "barge_in_cancel"), canonicalEventTime(event))
	case "barge_in_commit_ack":
		key, segment := s.activeKeyAndSegmentForControlLocked(event)
		s.finishSegmentLocked(key, segment, event.UtteranceID, asrPrototypeStateCancelled, firstNonEmpty(event.Reason, "barge_in_commit_ack"), canonicalEventTime(event))
	case "interrupt":
		for key, segment := range s.active {
			if event.SessionID != "" && event.SessionID != key.sessionID {
				continue
			}
			s.finishExistingSegmentLocked(key, segment, asrPrototypeStateCancelled, event.Reason, canonicalEventTime(event))
		}
	}
	s.mu.Unlock()

	for _, handoff := range handoffs {
		s.handoffs.HandleASREncodedAudioHandoff(handoff)
	}
	for _, handoff := range candidateHandoffs {
		s.candidateHandoffs.HandleASREncodedAudioHandoff(handoff)
	}
	for _, handoff := range bargeInHandoffs {
		s.bargeInHandoffs.HandleASREncodedAudioHandoff(handoff)
	}
}

func candidateSegmentAtWatermark(segment ASRPrototypeSegment, audioWatermark uint64) ASRPrototypeSegment {
	candidate := copyASRPrototypeSegment(segment)
	if audioWatermark == 0 {
		return candidate
	}
	const samplesPerOpusPacket = asrAudioRequestDefaultRate * asrAudioRequestDefaultFrameMS / 1000
	expectedPackets := int((audioWatermark + samplesPerOpusPacket - 1) / samplesPerOpusPacket)
	if expectedPackets >= len(candidate.EncodedPackets) {
		return candidate
	}
	candidate.EncodedPackets = candidate.EncodedPackets[:expectedPackets]
	candidate.AudioPackets = uint64(expectedPackets)
	candidate.EncodedPayloadBytes = 0
	for _, packet := range candidate.EncodedPackets {
		candidate.EncodedPayloadBytes += uint64(packet.PayloadBytes)
	}
	if expectedPackets == 0 {
		candidate.LastPacketCount = 0
	} else {
		candidate.LastPacketCount = candidate.EncodedPackets[expectedPackets-1].PacketCount
	}
	return candidate
}

func (s *ASRPrototypeSink) shouldDeferTurnCandidateSnapshotLocked(segment *ASRPrototypeSegment, event CanonicalVoiceEvent) bool {
	if event.candidateSnapshotReady || s.candidateSnapshotGrace <= 0 || event.AudioWatermark == 0 {
		return false
	}
	const samplesPerOpusPacket = asrAudioRequestDefaultRate * asrAudioRequestDefaultFrameMS / 1000
	expectedPackets := (event.AudioWatermark + samplesPerOpusPacket - 1) / samplesPerOpusPacket
	return uint64(len(segment.EncodedPackets)) < expectedPackets
}

func (s *ASRPrototypeSink) scheduleSegmentFinishLocked(key asrPrototypeSegmentKey, segment *ASRPrototypeSegment, utteranceID, reason string, endedAt time.Time) {
	if utteranceID != "" {
		segment.UtteranceID = strings.TrimSpace(utteranceID)
	}
	segment.State = asrPrototypeStateEnding
	segment.EndedAt = endedAt
	segment.Reason = reason
	s.stopPendingEndLocked(key)
	s.pendingEndSeq++
	token := s.pendingEndSeq
	timer := time.AfterFunc(s.endGrace, func() {
		s.finishSegmentAfterGrace(key, utteranceID, reason, endedAt, token)
	})
	s.pendingEnds[key] = asrPrototypePendingEnd{
		timer: timer,
		token: token,
	}
}

func (s *ASRPrototypeSink) finishSegmentAfterGrace(key asrPrototypeSegmentKey, utteranceID, reason string, endedAt time.Time, token uint64) {
	if s.isClosed() {
		return
	}

	var handoff ASREncodedAudioHandoff
	var ok bool

	s.mu.Lock()
	pending, pendingOK := s.pendingEnds[key]
	if !pendingOK || pending.token != token {
		s.mu.Unlock()
		return
	}
	delete(s.pendingEnds, key)
	segment := s.active[key]
	handoff, ok = s.finishSegmentLocked(key, segment, utteranceID, asrPrototypeStateCompleted, reason, endedAt)
	s.mu.Unlock()

	if ok && s.handoffs != nil {
		s.handoffs.HandleASREncodedAudioHandoff(handoff)
	}
}

func (s *ASRPrototypeSink) isClosed() bool {
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	return s.closed
}

func (s *ASRPrototypeSink) stopPendingEndTimers() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key := range s.pendingEnds {
		s.stopPendingEndLocked(key)
	}
}

func (s *ASRPrototypeSink) stopPendingEndLocked(key asrPrototypeSegmentKey) {
	if pending, ok := s.pendingEnds[key]; ok {
		pending.timer.Stop()
		delete(s.pendingEnds, key)
	}
}

func asrPrototypeKey(sessionID, utteranceID string) asrPrototypeSegmentKey {
	return asrPrototypeSegmentKey{
		sessionID:   sessionID,
		utteranceID: strings.TrimSpace(utteranceID),
	}
}

func (s *ASRPrototypeSink) activeKeyForPacketLocked(event CanonicalVoiceEvent) (asrPrototypeSegmentKey, bool) {
	if strings.TrimSpace(event.UtteranceID) != "" {
		return asrPrototypeKey(event.SessionID, event.UtteranceID), true
	}
	if key, ok := s.activeBySession[event.SessionID]; ok {
		return key, true
	}
	return asrPrototypeKey(event.SessionID, ""), false
}

func (s *ASRPrototypeSink) activeKeyAndSegmentForControlLocked(event CanonicalVoiceEvent) (asrPrototypeSegmentKey, *ASRPrototypeSegment) {
	if strings.TrimSpace(event.UtteranceID) != "" {
		key := asrPrototypeKey(event.SessionID, event.UtteranceID)
		return key, s.active[key]
	}
	if key, ok := s.activeBySession[event.SessionID]; ok {
		return key, s.active[key]
	}
	key := asrPrototypeKey(event.SessionID, "")
	return key, s.active[key]
}

func (s *ASRPrototypeSink) finishSegmentLocked(key asrPrototypeSegmentKey, segment *ASRPrototypeSegment, utteranceID, state, reason string, endedAt time.Time) (ASREncodedAudioHandoff, bool) {
	if segment == nil {
		return ASREncodedAudioHandoff{}, false
	}
	if utteranceID != "" {
		segment.UtteranceID = strings.TrimSpace(utteranceID)
	}
	return s.finishExistingSegmentLocked(key, segment, state, reason, endedAt)
}

func (s *ASRPrototypeSink) finishExistingSegmentLocked(key asrPrototypeSegmentKey, segment *ASRPrototypeSegment, state, reason string, endedAt time.Time) (ASREncodedAudioHandoff, bool) {
	s.stopPendingEndLocked(key)
	segment.finalizePacketQuality()
	segment.State = state
	segment.EndedAt = endedAt
	segment.Reason = reason
	delete(s.active, key)
	if currentKey, ok := s.activeBySession[key.sessionID]; ok && currentKey == key {
		s.refreshActiveSessionKeyLocked(key.sessionID)
	}
	completedSegment := copyASRPrototypeSegment(*segment)
	s.completed = append(s.completed, completedSegment)
	if len(s.completed) > maxCompletedASRPrototypeSegments {
		s.completed = s.completed[len(s.completed)-maxCompletedASRPrototypeSegments:]
	}
	if state != asrPrototypeStateCompleted || s.handoffs == nil {
		return ASREncodedAudioHandoff{}, false
	}
	handoff, err := NewASREncodedAudioHandoffFromSegment(completedSegment)
	if err != nil {
		s.handoffBuildErrorCount++
		return ASREncodedAudioHandoff{}, false
	}
	return handoff, true
}

func (s *ASRPrototypeSink) refreshActiveSessionKeyLocked(sessionID string) {
	var latestKey asrPrototypeSegmentKey
	var latestStartedAt time.Time
	found := false
	for key, segment := range s.active {
		if key.sessionID != sessionID {
			continue
		}
		if !found ||
			segment.StartedAt.After(latestStartedAt) ||
			(segment.StartedAt.Equal(latestStartedAt) && key.utteranceID > latestKey.utteranceID) {
			latestKey = key
			latestStartedAt = segment.StartedAt
			found = true
		}
	}
	if found {
		s.activeBySession[sessionID] = latestKey
		return
	}
	delete(s.activeBySession, sessionID)
}

func (segment *ASRPrototypeSegment) appendEncodedPacket(event CanonicalVoiceEvent) {
	hasRTPMetadata := canonicalEventHasRTPMetadata(event)
	if hasRTPMetadata {
		if segment.seenSequenceNumbers == nil {
			segment.seenSequenceNumbers = make(map[uint16]struct{})
		}
		if _, ok := segment.seenSequenceNumbers[event.LastSequenceNumber]; ok {
			segment.DuplicatePackets++
			return
		}
		segment.seenSequenceNumbers[event.LastSequenceNumber] = struct{}{}
	}
	if len(segment.EncodedPackets) >= maxASRPrototypeEncodedPackets {
		copy(segment.EncodedPackets, segment.EncodedPackets[1:])
		last := len(segment.EncodedPackets) - 1
		segment.EncodedPackets[last] = ASRPrototypeEncodedPacket{}
		segment.EncodedPackets = segment.EncodedPackets[:last]
		segment.DroppedEncodedPackets++
	}
	payload := append([]byte(nil), event.rtpPayload...)
	segment.EncodedPackets = append(segment.EncodedPackets, ASRPrototypeEncodedPacket{
		PacketCount:    event.PacketCount,
		Payload:        payload,
		PayloadBytes:   len(payload),
		TrackID:        event.TrackID,
		StreamID:       event.StreamID,
		HasRTPMetadata: hasRTPMetadata,
		SequenceNumber: event.LastSequenceNumber,
		Timestamp:      event.LastTimestamp,
	})
	segment.EncodedPayloadBytes += uint64(len(payload))
}

func canonicalEventHasRTPMetadata(event CanonicalVoiceEvent) bool {
	return event.FirstSequenceNumber != 0 ||
		event.LastSequenceNumber != 0 ||
		event.FirstTimestamp != 0 ||
		event.LastTimestamp != 0
}

func (segment *ASRPrototypeSegment) finalizePacketQuality() {
	packets := orderedASRPrototypePackets(segment.EncodedPackets)
	segment.EncodedPackets = packets
	segment.SequenceGaps = 0
	segment.MissingPackets = 0
	segment.TimestampRegressions = 0
	if len(packets) < 2 || !packets[0].HasRTPMetadata {
		return
	}
	lastSequence := asrPacketSequenceOrderValue(packets[0].SequenceNumber, packets[0].SequenceNumber)
	lastTimestamp := packets[0].Timestamp
	for _, packet := range packets[1:] {
		if !packet.HasRTPMetadata {
			continue
		}
		currentSequence := asrPacketSequenceOrderValue(packets[0].SequenceNumber, packet.SequenceNumber)
		if currentSequence > lastSequence+1 {
			segment.SequenceGaps++
			segment.MissingPackets += uint64(currentSequence - lastSequence - 1)
		}
		if packet.Timestamp < lastTimestamp {
			segment.TimestampRegressions++
		}
		lastSequence = currentSequence
		lastTimestamp = packet.Timestamp
	}
}

func orderedASRPrototypePackets(packets []ASRPrototypeEncodedPacket) []ASRPrototypeEncodedPacket {
	if len(packets) <= 1 || !packetsHaveRTPMetadata(packets) {
		return copyASRPrototypeEncodedPackets(packets)
	}
	ordered := copyASRPrototypeEncodedPackets(packets)
	firstSequence := ordered[0].SequenceNumber
	sort.SliceStable(ordered, func(i, j int) bool {
		iSequence := asrPacketSequenceOrderValue(firstSequence, ordered[i].SequenceNumber)
		jSequence := asrPacketSequenceOrderValue(firstSequence, ordered[j].SequenceNumber)
		if iSequence != jSequence {
			return iSequence < jSequence
		}
		return ordered[i].PacketCount < ordered[j].PacketCount
	})
	return ordered
}

func packetsHaveRTPMetadata(packets []ASRPrototypeEncodedPacket) bool {
	for _, packet := range packets {
		if !packet.HasRTPMetadata {
			return false
		}
	}
	return len(packets) > 0
}

func copyASRPrototypeEncodedPackets(packets []ASRPrototypeEncodedPacket) []ASRPrototypeEncodedPacket {
	if len(packets) == 0 {
		return nil
	}
	copied := make([]ASRPrototypeEncodedPacket, len(packets))
	for i, packet := range packets {
		copied[i] = packet
		if len(packet.Payload) > 0 {
			copied[i].Payload = append([]byte(nil), packet.Payload...)
		}
	}
	return copied
}

func asrPacketSequenceOrderValue(firstSequence, sequence uint16) uint32 {
	if sequence < firstSequence && firstSequence-sequence > 32768 {
		return uint32(sequence) + 65536
	}
	return uint32(sequence)
}

func cloneCanonicalVoiceEventPayload(event CanonicalVoiceEvent) CanonicalVoiceEvent {
	if len(event.rtpPayload) > 0 {
		event.rtpPayload = append([]byte(nil), event.rtpPayload...)
	}
	return event
}

func copyASRPrototypeSegment(segment ASRPrototypeSegment) ASRPrototypeSegment {
	segment.seenSequenceNumbers = nil
	segment.EncodedPackets = copyASRPrototypeEncodedPackets(segment.EncodedPackets)
	return segment
}

func canonicalEventTime(event CanonicalVoiceEvent) time.Time {
	if event.At.IsZero() {
		return time.Now().UTC()
	}
	return event.At
}

func nonNegativeInt(value int) int {
	if value < 0 {
		return 0
	}
	return value
}
