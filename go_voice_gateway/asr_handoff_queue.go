package main

import (
	"hash/fnv"
	"log"
	"sync"
)

const defaultASREncodedAudioHandoffQueueSize = 16

type ASREncodedAudioHandoffProcessor interface {
	ProcessASREncodedAudioHandoff(ASREncodedAudioHandoff) error
}

type ASREncodedAudioHandoffProcessorFunc func(ASREncodedAudioHandoff) error

func (f ASREncodedAudioHandoffProcessorFunc) ProcessASREncodedAudioHandoff(handoff ASREncodedAudioHandoff) error {
	return f(handoff)
}

type QueuedASREncodedAudioHandoffSink struct {
	queues    []chan ASREncodedAudioHandoff
	processor ASREncodedAudioHandoffProcessor
	logger    *log.Logger
	workers   int

	enqueueMu sync.Mutex
	closed    bool

	mu             sync.Mutex
	processedCount uint64
	droppedCount   uint64
	errorCount     uint64
	lastError      string

	wg sync.WaitGroup
}

type ASREncodedAudioHandoffQueueSnapshot struct {
	QueuedCount    int
	QueueCapacity  int
	ProcessedCount uint64
	DroppedCount   uint64
	ErrorCount     uint64
	LastError      string
	Workers        int
}

func NewQueuedASREncodedAudioHandoffSink(queueSize int, processor ASREncodedAudioHandoffProcessor) *QueuedASREncodedAudioHandoffSink {
	return NewConcurrentASREncodedAudioHandoffSink(queueSize, 1, processor)
}

func NewConcurrentASREncodedAudioHandoffSink(queueSize int, workers int, processor ASREncodedAudioHandoffProcessor) *QueuedASREncodedAudioHandoffSink {
	if queueSize <= 0 {
		queueSize = defaultASREncodedAudioHandoffQueueSize
	}
	if workers <= 0 {
		workers = 1
	}
	if processor == nil {
		processor = noopASREncodedAudioHandoffProcessor{}
	}
	perWorkerCapacity := (queueSize + workers - 1) / workers
	sink := &QueuedASREncodedAudioHandoffSink{
		queues:    make([]chan ASREncodedAudioHandoff, workers),
		processor: processor,
		workers:   workers,
	}
	sink.wg.Add(workers)
	for i := 0; i < workers; i++ {
		sink.queues[i] = make(chan ASREncodedAudioHandoff, perWorkerCapacity)
		go sink.run(sink.queues[i])
	}
	return sink
}

func (s *QueuedASREncodedAudioHandoffSink) SetLogger(logger *log.Logger) {
	if s == nil {
		return
	}
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	s.logger = logger
}

func (s *QueuedASREncodedAudioHandoffSink) HandleASREncodedAudioHandoff(handoff ASREncodedAudioHandoff) {
	s.enqueueMu.Lock()
	defer s.enqueueMu.Unlock()
	if s.closed {
		return
	}
	handoff = cloneASREncodedAudioHandoff(handoff)
	queue := s.queueForSession(handoff.SessionID)
	select {
	case queue <- handoff:
	default:
		s.mu.Lock()
		s.droppedCount++
		droppedTotal := s.droppedCount
		s.mu.Unlock()
		if s.logger != nil {
			s.logger.Printf(
				"asr_handoff_drop reason=queue_full session=%s trace_id=%s utterance_id=%s retained_packets=%d observed_packets=%d payload_bytes=%d queued=%d capacity=%d dropped_total=%d lossy=%t",
				printable(handoff.SessionID),
				printable(handoff.TraceID),
				printable(handoff.UtteranceID),
				handoff.RetainedPacketCount,
				handoff.ObservedPacketCount,
				handoff.PacketStreamBytes,
				len(queue),
				cap(queue),
				droppedTotal,
				handoff.Lossy,
			)
		}
	}
}

func (s *QueuedASREncodedAudioHandoffSink) Close() {
	s.enqueueMu.Lock()
	if !s.closed {
		s.closed = true
		for _, queue := range s.queues {
			close(queue)
		}
	}
	s.enqueueMu.Unlock()
	s.wg.Wait()
}

func (s *QueuedASREncodedAudioHandoffSink) Snapshot() ASREncodedAudioHandoffQueueSnapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	queuedCount := 0
	queueCapacity := 0
	for _, queue := range s.queues {
		queuedCount += len(queue)
		queueCapacity += cap(queue)
	}
	return ASREncodedAudioHandoffQueueSnapshot{
		QueuedCount:    queuedCount,
		QueueCapacity:  queueCapacity,
		ProcessedCount: s.processedCount,
		DroppedCount:   s.droppedCount,
		ErrorCount:     s.errorCount,
		LastError:      s.lastError,
		Workers:        s.workers,
	}
}

func (s *QueuedASREncodedAudioHandoffSink) run(queue <-chan ASREncodedAudioHandoff) {
	defer s.wg.Done()
	for handoff := range queue {
		err := s.processor.ProcessASREncodedAudioHandoff(handoff)
		s.mu.Lock()
		s.processedCount++
		if err != nil {
			s.errorCount++
			s.lastError = err.Error()
		}
		s.mu.Unlock()
	}
}

func (s *QueuedASREncodedAudioHandoffSink) queueForSession(sessionID string) chan ASREncodedAudioHandoff {
	if len(s.queues) == 1 {
		return s.queues[0]
	}
	hash := fnv.New32a()
	_, _ = hash.Write([]byte(sessionID))
	return s.queues[int(hash.Sum32()%uint32(len(s.queues)))]
}

type noopASREncodedAudioHandoffProcessor struct{}

func (noopASREncodedAudioHandoffProcessor) ProcessASREncodedAudioHandoff(ASREncodedAudioHandoff) error {
	return nil
}

func cloneASREncodedAudioHandoff(handoff ASREncodedAudioHandoff) ASREncodedAudioHandoff {
	if len(handoff.Payload) > 0 {
		handoff.Payload = append([]byte(nil), handoff.Payload...)
	}
	return handoff
}
