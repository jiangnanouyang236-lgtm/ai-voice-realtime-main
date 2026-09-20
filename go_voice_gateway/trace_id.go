package main

import (
	"fmt"
	"strings"
	"time"
)

func traceIDForVoiceTurn(existing, sessionID, turnID string) string {
	if traceID := strings.TrimSpace(existing); traceID != "" {
		return traceID
	}
	sessionID = strings.TrimSpace(sessionID)
	turnID = strings.TrimSpace(turnID)
	if sessionID != "" && turnID != "" {
		return fmt.Sprintf("%s:%s", sessionID, turnID)
	}
	if sessionID != "" {
		return fmt.Sprintf("%s:%d", sessionID, time.Now().UnixNano())
	}
	return fmt.Sprintf("trace_%d", time.Now().UnixNano())
}
