package main

import (
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

const (
	defaultLogFormat      = "text"
	defaultLogLevel       = "info"
	defaultLogMaxBytes    = 10 * 1024 * 1024
	defaultLogBackupCount = 5
)

type connectionInfo struct {
	RemoteAddr    string
	RemoteIP      string
	SourceIP      string
	XForwardedFor string
	XRealIP       string
	UserAgent     string
	Origin        string
}

func newStructuredLogger(w io.Writer, format, level string) (*slog.Logger, error) {
	levelVar, err := parseLogLevel(level)
	if err != nil {
		return nil, err
	}
	opts := &slog.HandlerOptions{Level: levelVar}
	switch strings.ToLower(strings.TrimSpace(format)) {
	case "", defaultLogFormat, "logfmt":
		return slog.New(slog.NewTextHandler(w, opts)).With("service", "go_voice_gateway"), nil
	case "json":
		return slog.New(slog.NewJSONHandler(w, opts)).With("service", "go_voice_gateway"), nil
	default:
		return nil, fmt.Errorf("unsupported log format: %s", format)
	}
}

func newLogOutput(stdout io.Writer, cfg Config, service string) (io.Writer, func() error, error) {
	logPath := strings.TrimSpace(cfg.LogFile)
	if logPath == "" && strings.TrimSpace(cfg.LogDir) != "" {
		logPath = filepath.Join(strings.TrimSpace(cfg.LogDir), service+".log")
	}
	if logPath == "" {
		return stdout, func() error { return nil }, nil
	}

	fileWriter, err := newRotatingFileWriter(logPath, int64(cfg.LogMaxBytes), cfg.LogBackupCount)
	if err != nil {
		return nil, nil, err
	}
	return io.MultiWriter(stdout, fileWriter), fileWriter.Close, nil
}

type rotatingFileWriter struct {
	mu          sync.Mutex
	path        string
	maxBytes    int64
	backupCount int
	file        *os.File
	size        int64
}

func newRotatingFileWriter(path string, maxBytes int64, backupCount int) (*rotatingFileWriter, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, err
	}
	info, err := file.Stat()
	if err != nil {
		_ = file.Close()
		return nil, err
	}
	return &rotatingFileWriter{
		path:        path,
		maxBytes:    maxBytes,
		backupCount: backupCount,
		file:        file,
		size:        info.Size(),
	}, nil
}

func (w *rotatingFileWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w.maxBytes > 0 && w.size > 0 && w.size+int64(len(p)) > w.maxBytes {
		if err := w.rotateLocked(); err != nil {
			return 0, err
		}
	}
	n, err := w.file.Write(p)
	w.size += int64(n)
	return n, err
}

func (w *rotatingFileWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}

func (w *rotatingFileWriter) rotateLocked() error {
	if w.file != nil {
		if err := w.file.Close(); err != nil {
			return err
		}
		w.file = nil
	}
	if w.backupCount > 0 {
		oldest := fmt.Sprintf("%s.%d", w.path, w.backupCount)
		if err := os.Remove(oldest); err != nil && !os.IsNotExist(err) {
			return err
		}
		for i := w.backupCount - 1; i >= 1; i-- {
			src := fmt.Sprintf("%s.%d", w.path, i)
			dst := fmt.Sprintf("%s.%d", w.path, i+1)
			if err := os.Rename(src, dst); err != nil && !os.IsNotExist(err) {
				return err
			}
		}
		if err := os.Rename(w.path, w.path+".1"); err != nil && !os.IsNotExist(err) {
			return err
		}
	} else if err := os.Remove(w.path); err != nil && !os.IsNotExist(err) {
		return err
	}

	file, err := os.OpenFile(w.path, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	w.file = file
	w.size = 0
	return nil
}

func parseLogLevel(raw string) (slog.Level, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "", defaultLogLevel:
		return slog.LevelInfo, nil
	case "debug":
		return slog.LevelDebug, nil
	case "warn", "warning":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return slog.LevelInfo, fmt.Errorf("unsupported log level: %s", raw)
	}
}

func connectionInfoFromRequest(remoteAddr string, req *http.Request) connectionInfo {
	info := connectionInfo{
		RemoteAddr: strings.TrimSpace(remoteAddr),
		RemoteIP:   hostFromAddr(remoteAddr),
	}
	if req != nil {
		info.XForwardedFor = strings.TrimSpace(req.Header.Get("X-Forwarded-For"))
		info.XRealIP = strings.TrimSpace(req.Header.Get("X-Real-IP"))
		info.UserAgent = strings.TrimSpace(req.UserAgent())
		info.Origin = strings.TrimSpace(req.Header.Get("Origin"))
		if info.RemoteAddr == "" {
			info.RemoteAddr = strings.TrimSpace(req.RemoteAddr)
			info.RemoteIP = hostFromAddr(req.RemoteAddr)
		}
	}

	info.SourceIP = firstForwardedIP(info.XForwardedFor)
	if info.SourceIP == "" {
		info.SourceIP = info.XRealIP
	}
	if info.SourceIP == "" {
		info.SourceIP = info.RemoteIP
	}
	return info
}

func hostFromAddr(addr string) string {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return ""
	}
	host, _, err := net.SplitHostPort(addr)
	if err == nil {
		return strings.Trim(host, "[]")
	}
	return strings.Trim(addr, "[]")
}

func firstForwardedIP(xForwardedFor string) string {
	for _, part := range strings.Split(xForwardedFor, ",") {
		value := strings.TrimSpace(part)
		if value != "" {
			return value
		}
	}
	return ""
}

func requestPath(req *http.Request) string {
	if req == nil || req.URL == nil {
		return "-"
	}
	return req.URL.Path
}

func (info connectionInfo) logAttrs() []any {
	return []any{
		"source_ip", printable(info.SourceIP),
		"remote_ip", printable(info.RemoteIP),
		"remote_addr", printable(info.RemoteAddr),
		"x_forwarded_for", printable(info.XForwardedFor),
		"x_real_ip", printable(info.XRealIP),
		"user_agent", printable(info.UserAgent),
		"origin", printable(info.Origin),
	}
}
