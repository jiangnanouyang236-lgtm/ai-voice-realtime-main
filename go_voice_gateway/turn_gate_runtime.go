package main

import (
	"errors"
	"strings"
	"sync"
	"time"
)

type TurnCandidateResult struct {
	TraceID        string
	UtteranceID    string
	CandidateSeq   uint64
	SpeechEpoch    uint64
	AudioWatermark uint64
	Status         string
	ASRText        string
	ASRTimeMS      float64
	PolicyPreview  string
	Shadow         bool
}

type turnGateRuntimeCandidate struct {
	traceID        string
	utteranceID    string
	candidateSeq   uint64
	speechEpoch    uint64
	audioWatermark uint64
	receivedAt     time.Time
}

type turnGateRuntimeState struct {
	utteranceID string
	candidate   *turnGateRuntimeCandidate
	pending     *TurnCandidateResult
	committed   bool
	active      bool
}

type TurnGateRuntimeController struct {
	mu       sync.Mutex
	deadline time.Duration
	states   map[string]*turnGateRuntimeState
}

func NewTurnGateRuntimeController(deadline time.Duration) *TurnGateRuntimeController {
	if deadline <= 0 {
		deadline = 200 * time.Millisecond
	}
	return &TurnGateRuntimeController{
		deadline: deadline,
		states:   make(map[string]*turnGateRuntimeState),
	}
}

// Observe records client facts before the ASR prototype consumes them. The
// return value suppresses events made obsolete by an accepted active commit.
func (c *TurnGateRuntimeController) Observe(event CanonicalVoiceEvent) bool {
	if c == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	sessionID := strings.TrimSpace(event.SessionID)
	if sessionID == "" {
		return false
	}
	state := c.states[sessionID]
	switch event.Type {
	case "audio_start":
		c.states[sessionID] = &turnGateRuntimeState{utteranceID: strings.TrimSpace(event.UtteranceID)}
		return false
	case "turn_candidate":
		if event.Shadow || event.CandidateSeq == 0 || event.AudioWatermark == 0 {
			return false
		}
		if state == nil || state.committed || state.utteranceID != strings.TrimSpace(event.UtteranceID) {
			return false
		}
		if state.candidate != nil && (event.CandidateSeq <= state.candidate.candidateSeq || event.AudioWatermark < state.candidate.audioWatermark) {
			return false
		}
		receivedAt := event.At
		if receivedAt.IsZero() {
			receivedAt = time.Now()
		}
		state.candidate = &turnGateRuntimeCandidate{
			traceID:        strings.TrimSpace(event.TraceID),
			utteranceID:    strings.TrimSpace(event.UtteranceID),
			candidateSeq:   event.CandidateSeq,
			speechEpoch:    event.SpeechEpoch,
			audioWatermark: event.AudioWatermark,
			receivedAt:     receivedAt,
		}
		return false
	case "turn_candidate_cancel":
		if state != nil && state.utteranceID == strings.TrimSpace(event.UtteranceID) && !state.committed {
			state.candidate = nil
			state.pending = nil
		}
		return false
	case "audio_end":
		if state != nil && state.utteranceID == strings.TrimSpace(event.UtteranceID) {
			if state.committed && state.active {
				return true
			}
			state.committed = true
			state.active = false
			state.candidate = nil
			state.pending = nil
		}
		return false
	case "audio_cancel":
		delete(c.states, sessionID)
		return false
	case "interrupt":
		delete(c.states, sessionID)
		return false
	case canonicalEventAudioPacket:
		return state != nil && state.committed && state.active
	default:
		return false
	}
}

// ProposeIfCurrent serializes cloud receipt order with client facts. The Rust
// client still validates its latest VAD epoch and replies with turn_commit_ack.
func (c *TurnGateRuntimeController) ProposeIfCurrent(
	sessionID string,
	result TurnCandidateResult,
	now time.Time,
	sendRequest func() error,
) (bool, string, error) {
	if c == nil {
		return false, "controller_disabled", nil
	}
	if sendRequest == nil {
		return false, "commit_sender_missing", errors.New("turn commit sender is required")
	}
	if now.IsZero() {
		now = time.Now()
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	state := c.states[strings.TrimSpace(sessionID)]
	if state == nil || state.committed || state.candidate == nil || state.pending != nil {
		return false, "no_active_candidate", nil
	}
	candidate := state.candidate
	if result.Shadow || result.Status != "observed" || result.PolicyPreview != "early_commit" || strings.TrimSpace(result.ASRText) == "" {
		return false, "result_not_eligible", nil
	}
	if candidate.utteranceID != strings.TrimSpace(result.UtteranceID) ||
		candidate.candidateSeq != result.CandidateSeq ||
		candidate.speechEpoch != result.SpeechEpoch ||
		candidate.audioWatermark != result.AudioWatermark {
		return false, "stale_candidate", nil
	}
	if now.Sub(candidate.receivedAt) > c.deadline {
		state.candidate = nil
		return false, "deadline_expired", nil
	}
	if err := sendRequest(); err != nil {
		return false, "commit_send_failed", err
	}
	resultCopy := result
	state.pending = &resultCopy
	return true, "commit_requested", nil
}

func (c *TurnGateRuntimeController) AcceptAck(event CanonicalVoiceEvent) (*TurnCandidateResult, string) {
	if c == nil {
		return nil, "controller_disabled"
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	state := c.states[strings.TrimSpace(event.SessionID)]
	if state == nil || state.committed || state.pending == nil {
		return nil, "no_pending_commit"
	}
	result := state.pending
	if result.UtteranceID != strings.TrimSpace(event.UtteranceID) ||
		result.CandidateSeq != event.CandidateSeq ||
		result.SpeechEpoch != event.SpeechEpoch ||
		result.AudioWatermark != event.AudioWatermark {
		return nil, "stale_commit_ack"
	}
	state.pending = nil
	if !event.Accepted {
		state.candidate = nil
		return nil, "client_rejected"
	}
	state.committed = true
	state.active = true
	state.candidate = nil
	resultCopy := *result
	return &resultCopy, "active_commit"
}
