package main

import (
	"encoding/json"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	bootstrapLogger := log.New(os.Stdout, "go_voice_gateway ", log.LstdFlags|log.Lmicroseconds)
	if healthcheckURL, ok := argValue("--healthcheck-url"); ok {
		if err := runHTTPHealthcheck(healthcheckURL); err != nil {
			bootstrapLogger.Fatalf("healthcheck failed: %v", err)
		}
		return
	}
	if hasArg("--healthcheck") {
		if err := runHTTPHealthcheck(healthcheckURLFromListenAddr(os.Getenv("GO_VOICE_GATEWAY_ADDR"))); err != nil {
			bootstrapLogger.Fatalf("healthcheck failed: %v", err)
		}
		return
	}
	if hasArg("--print-env-help") {
		if err := printEnvHelp(os.Stdout, "Go Voice Gateway environment variables", goGatewayEnvHelpEntries()); err != nil {
			bootstrapLogger.Fatalf("print env help failed: %v", err)
		}
		return
	}
	cfg, err := LoadConfigFromEnv()
	if err != nil {
		bootstrapLogger.Fatalf("load config failed: %v", err)
	}
	if hasArg("--print-config") {
		encoder := json.NewEncoder(os.Stdout)
		encoder.SetIndent("", "  ")
		if err := encoder.Encode(cfg.Printable()); err != nil {
			bootstrapLogger.Fatalf("print config failed: %v", err)
		}
		return
	}
	logOutput, closeLogOutput, err := newLogOutput(os.Stdout, cfg, "go_voice_gateway")
	if err != nil {
		bootstrapLogger.Fatalf("create log output failed: %v", err)
	}
	defer func() {
		if err := closeLogOutput(); err != nil {
			bootstrapLogger.Printf("close log output failed: %v", err)
		}
	}()
	structuredLogger, err := newStructuredLogger(logOutput, cfg.LogFormat, cfg.LogLevel)
	if err != nil {
		bootstrapLogger.Fatalf("create logger failed: %v", err)
	}
	logger := slog.NewLogLogger(structuredLogger.Handler(), slog.LevelInfo)
	server, err := NewServerWithStructuredLogger(cfg, logger, structuredLogger)
	if err != nil {
		logger.Fatalf("create server failed: %v", err)
	}
	defer server.Close()
	httpServer := &http.Server{
		Addr:              cfg.Addr,
		Handler:           server.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	structuredLogger.Info(
		"server_listening",
		"addr", cfg.Addr,
		"ws_path", cfg.WebSocketPath,
		"rtc_enabled", cfg.RTCEnabled,
		"ice_servers", len(cfg.ICEServers),
		"ice_policy", cfg.ICETransportPolicy.String(),
		"ice_networks", strings.Join(iceNetworkTypeStrings(cfg.ICENetworkTypes), ","),
		"rtc_udp_port_range", fmt.Sprintf("%d-%d", cfg.RTCUDPMinPort, cfg.RTCUDPMaxPort),
		"downlink_transport", cfg.Audio.DownlinkTransport,
		"asr_processor", cfg.ASR.Processor,
		"python_gateway_ws_url", printable(cfg.ASR.PythonGatewayWSURL),
		"python_gateway_connection_mode", printable(cfg.ASR.PythonGatewayConnectionMode),
		"internal_voice_enabled", cfg.InternalVoice.Enabled,
		"internal_voice_ws_url", printable(cfg.InternalVoice.WSURL),
		"device_auth_backend", cfg.DeviceAuth.Backend,
		"require_robot_secret", cfg.DeviceAuth.RequireRobotSecret,
		"message_limit_bytes", cfg.MessageLimitBytes,
		"write_timeout_ms", cfg.WriteTimeout.Milliseconds(),
		"negotiation_timeout_ms", cfg.NegotiationTimeout.Milliseconds(),
		"interface", printable(cfg.LocalInterfaceName),
		"ip", printable(cfg.LocalIP),
		"allowed_origins", strings.Join(cfg.AllowedOrigins, ","),
		"log_format", cfg.LogFormat,
		"log_level", cfg.LogLevel,
		"log_file", printable(cfg.LogFile),
		"log_dir", printable(cfg.LogDir),
		"log_max_bytes", cfg.LogMaxBytes,
		"log_backup_count", cfg.LogBackupCount,
	)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		logger.Fatalf("server stopped: %v", err)
	}
}

func runHTTPHealthcheck(rawURL string) error {
	return runHTTPHealthcheckWithClient(rawURL, &http.Client{Timeout: 3 * time.Second})
}

type healthcheckHTTPClient interface {
	Do(*http.Request) (*http.Response, error)
}

func runHTTPHealthcheckWithClient(rawURL string, client healthcheckHTTPClient) error {
	rawURL = strings.TrimSpace(rawURL)
	if rawURL == "" {
		return &healthcheckError{message: "empty healthcheck URL"}
	}
	request, err := http.NewRequest(http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(request)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return &healthcheckError{message: "unexpected status " + resp.Status}
	}
	return nil
}

func healthcheckURLFromListenAddr(addr string) string {
	addr = strings.TrimSpace(addr)
	if addr == "" {
		addr = defaultAddr
	}
	port := ""
	if strings.HasPrefix(addr, "[") {
		if idx := strings.LastIndex(addr, "]:"); idx >= 0 {
			port = strings.TrimSpace(addr[idx+2:])
		}
	}
	if port == "" {
		if idx := strings.LastIndex(addr, ":"); idx >= 0 {
			port = strings.TrimSpace(addr[idx+1:])
		} else if isDecimalPort(addr) {
			port = addr
		}
	}
	if port == "" {
		port = "8282"
	}
	return "http://127.0.0.1:" + port + "/healthz"
}

func isDecimalPort(value string) bool {
	if value == "" {
		return false
	}
	for _, ch := range value {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}

type healthcheckError struct {
	message string
}

func (e *healthcheckError) Error() string {
	return e.message
}

func hasArg(flag string) bool {
	for _, arg := range os.Args[1:] {
		if arg == flag {
			return true
		}
	}
	return false
}

func argValue(flag string) (string, bool) {
	args := os.Args[1:]
	for i, arg := range args {
		if arg == flag {
			if i+1 >= len(args) {
				return "", true
			}
			return args[i+1], true
		}
		if strings.HasPrefix(arg, flag+"=") {
			return strings.TrimPrefix(arg, flag+"="), true
		}
	}
	return "", false
}
