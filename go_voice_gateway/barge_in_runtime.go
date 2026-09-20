package main

import (
	"strings"
	"sync"
)

type BargeInResult struct {
	TraceID        string
	UtteranceID    string
	RoundID        string
	PlaybackID     string
	CandidateSeq   uint64
	SpeechEpoch    uint64
	AudioWatermark uint64
	Status         string
	Decision       string
	Reason         string
	ASRText        string
	ASRTimeMS      float64
}

type bargeInPending struct {
	result BargeInResult
}

type bargeInActive struct {
	utteranceID string
	roundID     string
	playbackID  string
	speechEpoch uint64
}

type BargeInRuntimeController struct {
	mu      sync.Mutex
	pending map[string]bargeInPending
	active  map[string]bargeInActive
}

func NewBargeInRuntimeController() *BargeInRuntimeController {
	return &BargeInRuntimeController{
		pending: make(map[string]bargeInPending),
		active:  make(map[string]bargeInActive),
	}
}

func (c *BargeInRuntimeController) Start(sessionID string, event CanonicalVoiceEvent) bool {
	if c == nil || strings.TrimSpace(sessionID) == "" || strings.TrimSpace(event.UtteranceID) == "" ||
		strings.TrimSpace(event.RoundID) == "" || strings.TrimSpace(event.PlaybackID) == "" || event.SpeechEpoch == 0 {
		return false
	}
	c.mu.Lock()
	c.active[sessionID] = bargeInActive{
		utteranceID: strings.TrimSpace(event.UtteranceID),
		roundID:     strings.TrimSpace(event.RoundID),
		playbackID:  strings.TrimSpace(event.PlaybackID),
		speechEpoch: event.SpeechEpoch,
	}
	delete(c.pending, sessionID)
	c.mu.Unlock()
	return true
}

func (c *BargeInRuntimeController) Remember(sessionID string, result BargeInResult) bool {
	if c == nil || strings.TrimSpace(sessionID) == "" || result.Decision == "ignore" {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	active, ok := c.active[sessionID]
	if !ok || active.utteranceID != strings.TrimSpace(result.UtteranceID) ||
		active.roundID != strings.TrimSpace(result.RoundID) ||
		active.playbackID != strings.TrimSpace(result.PlaybackID) || active.speechEpoch != result.SpeechEpoch {
		return false
	}
	current, ok := c.pending[sessionID]
	if ok && (result.SpeechEpoch < current.result.SpeechEpoch ||
		(result.SpeechEpoch == current.result.SpeechEpoch && result.CandidateSeq <= current.result.CandidateSeq)) {
		return false
	}
	c.pending[sessionID] = bargeInPending{result: result}
	return true
}

func (c *BargeInRuntimeController) Accept(sessionID string, event CanonicalVoiceEvent) (*BargeInResult, string) {
	if c == nil {
		return nil, "disabled"
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	pending, ok := c.pending[sessionID]
	if !ok {
		return nil, "missing_pending"
	}
	result := pending.result
	if result.UtteranceID != strings.TrimSpace(event.UtteranceID) ||
		result.RoundID != strings.TrimSpace(event.RoundID) ||
		result.PlaybackID != strings.TrimSpace(event.PlaybackID) ||
		result.CandidateSeq != event.CandidateSeq ||
		result.SpeechEpoch != event.SpeechEpoch ||
		result.AudioWatermark != event.AudioWatermark {
		return nil, "identity_mismatch"
	}
	delete(c.pending, sessionID)
	delete(c.active, sessionID)
	if !event.Accepted {
		return nil, firstNonEmpty(strings.TrimSpace(event.Reason), "client_rejected")
	}
	return &result, "accepted"
}

func (c *BargeInRuntimeController) Clear(sessionID string) {
	if c == nil {
		return
	}
	c.mu.Lock()
	delete(c.pending, sessionID)
	delete(c.active, sessionID)
	c.mu.Unlock()
}
