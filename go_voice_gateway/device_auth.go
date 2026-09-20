package main

import (
	"context"
	"fmt"
	"log"
	"strings"
	"time"
)

type DeviceAuthenticator interface {
	AuthenticateRegister(context.Context, clientMessage) (deviceRegistration, error)
}

type deviceRegistration struct {
	RobotID    string
	BotID      string
	BotName    string
	ClientType string
	IsNewRobot bool
}

type noopDeviceAuthenticator struct {
	defaultBotID string
}

func (a noopDeviceAuthenticator) AuthenticateRegister(_ context.Context, msg clientMessage) (deviceRegistration, error) {
	return fallbackDeviceRegistration(msg, a.defaultBotID), nil
}

type pythonGatewayDeviceAuthenticator struct {
	bridge *PythonGatewayBridgeProcessor
}

func (a pythonGatewayDeviceAuthenticator) AuthenticateRegister(ctx context.Context, msg clientMessage) (deviceRegistration, error) {
	if a.bridge == nil {
		return deviceRegistration{}, fmt.Errorf("python gateway device authenticator is not configured")
	}
	return a.bridge.AuthenticateRegister(ctx, msg)
}

func newConfiguredDeviceAuthenticator(cfg Config, logger *log.Logger) (DeviceAuthenticator, error) {
	if !cfg.DeviceAuth.RequireRobotSecret {
		return noopDeviceAuthenticator{defaultBotID: cfg.BotID}, nil
	}

	switch strings.ToLower(strings.TrimSpace(cfg.DeviceAuth.Backend)) {
	case deviceAuthBackendPythonGateway:
		bridge, err := NewPythonGatewayBridgeProcessor(PythonGatewayBridgeConfig{
			WSURL:           cfg.ASR.PythonGatewayWSURL,
			ClientType:      cfg.ASR.PythonGatewayClientType,
			BotID:           firstNonEmpty(cfg.ASR.PythonGatewayBotID, cfg.BotID),
			Timeout:         time.Duration(maxInt(cfg.ASR.PythonGatewayTimeoutMS, 1)) * time.Millisecond,
			TotalTimeout:    time.Duration(maxInt(cfg.ASR.PythonGatewayTotalTimeoutMS, 0)) * time.Millisecond,
			MaxMessageBytes: maxInt(cfg.ASR.PythonGatewayMaxMessageKB, 1) * 1024,
		}, logger)
		if err != nil {
			return nil, err
		}
		return pythonGatewayDeviceAuthenticator{bridge: bridge}, nil
	case "", deviceAuthBackendNone:
		return nil, fmt.Errorf("device auth requires robot_secret but backend is %q", cfg.DeviceAuth.Backend)
	default:
		return nil, fmt.Errorf("unsupported device auth backend: %s", cfg.DeviceAuth.Backend)
	}
}

func fallbackDeviceRegistration(msg clientMessage, defaultBotID string) deviceRegistration {
	robotID := strings.TrimSpace(msg.RobotID)
	if robotID == "" {
		robotID = "unknown_robot"
	}
	clientType := strings.TrimSpace(msg.ClientType)
	if clientType == "" {
		clientType = "unknown"
	}
	botID := strings.TrimSpace(defaultBotID)
	if botID == "" {
		botID = defaultBotID
	}
	return deviceRegistration{
		RobotID:    robotID,
		BotID:      botID,
		BotName:    botID,
		ClientType: clientType,
		IsNewRobot: false,
	}
}
