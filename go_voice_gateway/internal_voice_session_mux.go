package main

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"golang.org/x/net/websocket"
)

const (
	internalVoiceWaiterStatus = "status"
	internalVoiceWaiterType   = "type"
	internalVoiceWaiterTurn   = "turn"
)

type internalVoiceWaiter struct {
	id           uint64
	kind         string
	expectedType string
	request      internalVoiceEnvelope
	messages     chan pythonGatewayBridgeMessage
	errors       chan error
}

func newInternalVoiceWaiter(
	id uint64,
	kind string,
	expectedType string,
	request internalVoiceEnvelope,
) *internalVoiceWaiter {
	bufferSize := 1
	if kind == internalVoiceWaiterTurn {
		bufferSize = 64
	}
	return &internalVoiceWaiter{
		id:           id,
		kind:         kind,
		expectedType: expectedType,
		request:      request,
		messages:     make(chan pythonGatewayBridgeMessage, bufferSize),
		errors:       make(chan error, 1),
	}
}

func (s *InternalVoiceSession) startReader() {
	if s == nil || s.conn == nil {
		return
	}
	go s.readerLoop()
}

func (s *InternalVoiceSession) readerLoop() {
	defer close(s.readerDone)
	defer s.heartbeatOnce.Do(func() {
		close(s.heartbeatStop)
	})
	for {
		message, err := receivePythonGatewayBridgeMessage(s.conn)
		if err != nil {
			s.setReaderError(fmt.Errorf("receive internal voice message: %w", err))
			return
		}
		s.lastReceive.Store(time.Now().UnixMilli())
		if err := s.dispatchIncoming(message); err != nil {
			s.setReaderError(err)
			return
		}
	}
}

func (s *InternalVoiceSession) dispatchIncoming(message pythonGatewayBridgeMessage) error {
	var envelope *internalVoiceEnvelope
	if message.IsJSON && isInternalVoiceControlMessageType(message.Type) {
		decoded, err := internalVoiceEnvelopeFromBridgeMessage(message)
		if err != nil {
			return err
		}
		if strings.TrimSpace(decoded.SessionID) != strings.TrimSpace(s.sessionID) {
			return fmt.Errorf(
				"internal voice session mismatch: expected=%s actual=%s",
				s.sessionID,
				decoded.SessionID,
			)
		}
		envelope = &decoded
	}

	s.pendingMu.Lock()
	ids := make([]uint64, 0, len(s.pending))
	for id := range s.pending {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })

	var matched *internalVoiceWaiter
	for _, id := range ids {
		waiter := s.pending[id]
		if internalVoiceWaiterMatches(waiter, message, envelope) {
			matched = waiter
			break
		}
	}
	if matched == nil &&
		envelope != nil &&
		envelope.Type == internalVoiceTypeProtocolError &&
		len(ids) == 1 {
		matched = s.pending[ids[0]]
	}
	if matched == nil {
		s.pendingMu.Unlock()
		if s.client != nil && s.client.logger != nil {
			s.client.logger.Printf(
				"internal_voice_unmatched_message session=%s type=%s json=%t binary=%t payload_bytes=%d",
				printable(s.sessionID),
				printable(message.Type),
				message.IsJSON,
				len(message.Binary) > 0,
				len(message.Payload),
			)
		}
		return nil
	}

	if envelope != nil && envelope.Type == internalVoiceTypeProtocolError {
		delete(s.pending, matched.id)
		s.pendingMu.Unlock()
		matched.fail(internalVoiceProtocolError(*envelope))
		return nil
	}

	terminal := matched.kind != internalVoiceWaiterTurn
	if envelope != nil {
		switch envelope.Type {
		case internalVoiceTypeResponseDone,
			internalVoiceTypeResponseCancelled,
			internalVoiceTypeResponseError:
			terminal = true
		}
	}
	if terminal {
		delete(s.pending, matched.id)
	}
	select {
	case matched.messages <- message:
		s.pendingMu.Unlock()
		return nil
	default:
		delete(s.pending, matched.id)
		s.pendingMu.Unlock()
		matched.fail(fmt.Errorf(
			"internal voice %s receive queue is full",
			matched.kind,
		))
		return nil
	}
}

func internalVoiceWaiterMatches(
	waiter *internalVoiceWaiter,
	message pythonGatewayBridgeMessage,
	envelope *internalVoiceEnvelope,
) bool {
	if waiter == nil {
		return false
	}
	if envelope != nil && envelope.Type == internalVoiceTypeProtocolError {
		return internalVoiceEnvelopesCorrelate(waiter.request, *envelope)
	}
	switch waiter.kind {
	case internalVoiceWaiterStatus:
		return envelope != nil &&
			envelope.Type == internalVoiceTypeOrchestratorStatus &&
			internalVoiceEnvelopesCorrelate(waiter.request, *envelope)
	case internalVoiceWaiterType:
		return envelope != nil &&
			envelope.Type == waiter.expectedType &&
			internalVoiceEnvelopesCorrelate(waiter.request, *envelope)
	case internalVoiceWaiterTurn:
		if envelope != nil {
			switch envelope.Type {
			case internalVoiceTypeResponseASR:
				return internalVoiceEnvelopesCorrelate(waiter.request, *envelope)
			case internalVoiceTypeResponseDone,
				internalVoiceTypeResponseCancelled,
				internalVoiceTypeResponseError:
				return internalVoiceEnvelopesCorrelate(waiter.request, *envelope)
			default:
				return false
			}
		}
		if !message.IsJSON {
			return true
		}
		switch message.Type {
		case "playback_start", "audio_frame", "done", "error":
			return internalVoiceLegacyMessageCorrelates(waiter.request, message)
		default:
			return false
		}
	default:
		return false
	}
}

func internalVoiceEnvelopesCorrelate(
	request internalVoiceEnvelope,
	response internalVoiceEnvelope,
) bool {
	if strings.TrimSpace(request.SessionID) != strings.TrimSpace(response.SessionID) {
		return false
	}
	for _, pair := range [][2]string{
		{request.TraceID, response.TraceID},
		{request.UtteranceID, response.UtteranceID},
		{request.RoundID, response.RoundID},
		{request.PlaybackID, response.PlaybackID},
	} {
		expected := strings.TrimSpace(pair[0])
		if expected == "" {
			continue
		}
		if strings.TrimSpace(pair[1]) != expected {
			return false
		}
	}
	return true
}

func internalVoiceLegacyMessageCorrelates(
	request internalVoiceEnvelope,
	message pythonGatewayBridgeMessage,
) bool {
	if !message.IsJSON {
		return true
	}
	for key, expected := range map[string]string{
		"trace_id":    request.TraceID,
		"round_id":    request.RoundID,
		"playback_id": request.PlaybackID,
	} {
		expected = strings.TrimSpace(expected)
		if expected == "" {
			continue
		}
		actual := strings.TrimSpace(stringValue(message.JSON[key]))
		if actual != "" && actual != expected {
			return false
		}
	}
	return true
}

func (s *InternalVoiceSession) registerWaiter(
	kind string,
	expectedType string,
	request internalVoiceEnvelope,
) (*internalVoiceWaiter, error) {
	id := s.nextWaiterID.Add(1)
	waiter := newInternalVoiceWaiter(id, kind, expectedType, request)
	s.readerErrMu.Lock()
	defer s.readerErrMu.Unlock()
	if s.readerErr != nil {
		return nil, s.readerErr
	}
	s.pendingMu.Lock()
	defer s.pendingMu.Unlock()
	if kind == internalVoiceWaiterTurn {
		for _, existing := range s.pending {
			if existing.kind == internalVoiceWaiterTurn {
				return nil, fmt.Errorf("internal voice response turn is already active")
			}
		}
	}
	s.pending[id] = waiter
	return waiter, nil
}

func (s *InternalVoiceSession) unregisterWaiter(waiter *internalVoiceWaiter) {
	if s == nil || waiter == nil {
		return
	}
	s.pendingMu.Lock()
	delete(s.pending, waiter.id)
	s.pendingMu.Unlock()
}

func (s *InternalVoiceSession) setReaderError(err error) {
	if err == nil {
		return
	}
	s.readerErrMu.Lock()
	if s.readerErr == nil {
		s.readerErr = err
	}
	current := s.readerErr
	s.readerErrMu.Unlock()

	s.pendingMu.Lock()
	waiters := make([]*internalVoiceWaiter, 0, len(s.pending))
	for id, waiter := range s.pending {
		waiters = append(waiters, waiter)
		delete(s.pending, id)
	}
	s.pendingMu.Unlock()
	for _, waiter := range waiters {
		waiter.fail(current)
	}
}

func (s *InternalVoiceSession) currentReaderError() error {
	if s == nil {
		return fmt.Errorf("internal voice session is nil")
	}
	s.readerErrMu.Lock()
	defer s.readerErrMu.Unlock()
	return s.readerErr
}

func (w *internalVoiceWaiter) fail(err error) {
	if w == nil || err == nil {
		return
	}
	select {
	case w.errors <- err:
	default:
	}
}

func (s *InternalVoiceSession) sendEnvelope(envelope internalVoiceEnvelope, action string) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	if err := s.currentReaderError(); err != nil {
		return err
	}
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	s.client.setWriteDeadline(s.conn)
	defer s.client.clearWriteDeadline(s.conn)
	if err := websocket.JSON.Send(s.conn, envelope); err != nil {
		return fmt.Errorf("send internal voice %s: %w", action, err)
	}
	return nil
}

func (s *InternalVoiceSession) sendBinary(payload []byte, action string) error {
	if s == nil || s.client == nil || s.conn == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	if len(payload) == 0 {
		return fmt.Errorf("internal voice %s payload is empty", action)
	}
	if err := s.currentReaderError(); err != nil {
		return err
	}
	s.writeMu.Lock()
	defer s.writeMu.Unlock()
	s.client.setWriteDeadline(s.conn)
	defer s.client.clearWriteDeadline(s.conn)
	if err := websocket.Message.Send(s.conn, payload); err != nil {
		return fmt.Errorf("send internal voice %s: %w", action, err)
	}
	return nil
}

func waitForInternalVoiceMessage(
	ctx context.Context,
	waiter *internalVoiceWaiter,
) (pythonGatewayBridgeMessage, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	select {
	case message := <-waiter.messages:
		return message, nil
	case err := <-waiter.errors:
		return pythonGatewayBridgeMessage{}, err
	case <-ctx.Done():
		return pythonGatewayBridgeMessage{}, ctx.Err()
	}
}

func (s *InternalVoiceSession) SendHeartbeat(ctx context.Context, nonce string) error {
	if s == nil || s.client == nil {
		return fmt.Errorf("internal voice session is not open")
	}
	s.client.metrics.heartbeatAttempts.Add(1)
	err := s.sendHeartbeat(ctx, nonce)
	if err != nil {
		s.client.metrics.heartbeatFailures.Add(1)
		return err
	}
	s.client.metrics.heartbeatSuccesses.Add(1)
	return nil
}

func (s *InternalVoiceSession) sendHeartbeat(ctx context.Context, nonce string) error {
	nonce = strings.TrimSpace(nonce)
	if nonce == "" {
		return fmt.Errorf("internal voice heartbeat nonce is required")
	}
	envelope, err := newInternalVoiceEnvelope(
		internalVoiceTypeHeartbeatPing,
		s.sessionID,
		internalVoiceHeartbeatPayload{Nonce: nonce},
	)
	if err != nil {
		return err
	}
	waiter, err := s.registerWaiter(
		internalVoiceWaiterType,
		internalVoiceTypeHeartbeatPong,
		envelope,
	)
	if err != nil {
		return err
	}
	defer s.unregisterWaiter(waiter)
	if err := s.sendEnvelope(envelope, "heartbeat.ping"); err != nil {
		return err
	}
	message, err := waitForInternalVoiceMessage(ctx, waiter)
	if err != nil {
		return fmt.Errorf("receive internal voice heartbeat.pong: %w", err)
	}
	response, err := internalVoiceEnvelopeFromBridgeMessage(message)
	if err != nil {
		return err
	}
	var payload internalVoiceHeartbeatPayload
	if err := response.PayloadInto(&payload); err != nil {
		return err
	}
	if payload.Nonce != nonce {
		return fmt.Errorf(
			"internal voice heartbeat nonce mismatch: expected=%s actual=%s",
			nonce,
			payload.Nonce,
		)
	}
	return nil
}

func (s *InternalVoiceSession) startHeartbeat() {
	if s == nil || s.client == nil || s.heartbeatDone == nil {
		return
	}
	if s.client.cfg.HeartbeatInterval <= 0 {
		close(s.heartbeatDone)
		return
	}
	go s.heartbeatLoop()
}

func (s *InternalVoiceSession) stopHeartbeat() {
	if s == nil || s.heartbeatStop == nil {
		return
	}
	s.heartbeatOnce.Do(func() {
		close(s.heartbeatStop)
	})
	if s.heartbeatDone == nil {
		return
	}
	timeout := time.Second
	if s.client != nil && s.client.cfg.HeartbeatTimeout > 0 {
		timeout = s.client.cfg.HeartbeatTimeout + time.Second
	}
	select {
	case <-s.heartbeatDone:
	case <-time.After(timeout):
	}
}

func (s *InternalVoiceSession) heartbeatLoop() {
	defer close(s.heartbeatDone)
	interval := s.client.cfg.HeartbeatInterval
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	missed := 0
	for {
		select {
		case <-s.heartbeatStop:
			return
		case <-ticker.C:
		}

		lastReceive := time.UnixMilli(s.lastReceive.Load())
		if time.Since(lastReceive) < interval {
			continue
		}
		nonce := fmt.Sprintf("%s:%d", s.sessionID, time.Now().UnixNano())
		ctx, cancel := context.WithTimeout(
			context.Background(),
			s.client.cfg.HeartbeatTimeout,
		)
		s.client.metrics.heartbeatAttempts.Add(1)
		done := make(chan error, 1)
		go func() {
			done <- s.sendHeartbeat(ctx, nonce)
		}()
		var err error
		select {
		case err = <-done:
		case <-s.heartbeatStop:
			cancel()
			<-done
			return
		case <-ctx.Done():
			err = ctx.Err()
		}
		cancel()

		if err == nil {
			s.client.metrics.heartbeatSuccesses.Add(1)
			missed = 0
			continue
		}
		s.client.metrics.heartbeatFailures.Add(1)
		missed++
		if s.client.logger != nil {
			s.client.logger.Printf(
				"internal_voice_heartbeat_failed session=%s missed=%d limit=%d error=%v",
				printable(s.sessionID),
				missed,
				s.client.cfg.HeartbeatMissLimit,
				err,
			)
		}
		if missed < s.client.cfg.HeartbeatMissLimit {
			continue
		}
		s.client.metrics.heartbeatDead.Add(1)
		s.setReaderError(fmt.Errorf(
			"internal voice heartbeat failed %d consecutive times: %w",
			missed,
			err,
		))
		_ = s.conn.Close()
		return
	}
}
