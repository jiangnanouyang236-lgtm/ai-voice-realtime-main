package main

import (
	"strings"
	"testing"
)

func TestTraceIDForVoiceTurn(t *testing.T) {
	if got := traceIDForVoiceTurn(" trace_1 ", "session_1", "utt_1"); got != "trace_1" {
		t.Fatalf("explicit trace id = %q, want trace_1", got)
	}
	if got := traceIDForVoiceTurn("", "session_1", "utt_1"); got != "session_1:utt_1" {
		t.Fatalf("session turn trace id = %q, want session_1:utt_1", got)
	}
	got := traceIDForVoiceTurn("", "session_1", "")
	if !strings.HasPrefix(got, "session_1:") || got == "session_1:" {
		t.Fatalf("generated session trace id = %q", got)
	}
	if got := traceIDForVoiceTurn("", "", ""); !strings.HasPrefix(got, "trace_") {
		t.Fatalf("generated anonymous trace id = %q", got)
	}
}
