SHELL := /usr/bin/env bash

DIST_DIR ?= dist
UBUNTU22_AMD64_PACKAGE_NAME ?= go_voice_gateway-ubuntu22-amd64
UBUNTU22_AMD64_BUILD_OUT ?= $(DIST_DIR)/buildx-ubuntu22-amd64
GO_IMAGE ?= golang:1.24-bookworm

.PHONY: help test print-config print-env-help rtc-route-probe build-local package-ubuntu22-amd64 clean-dist

help:
	@printf '%s\n' 'Go Voice Gateway build targets:'
	@printf '%s\n' '  make test                     Run Go Gateway tests'
	@printf '%s\n' '  make print-config             Print effective config with secrets redacted'
	@printf '%s\n' '  make print-env-help           Print environment variable mapping'
	@printf '%s\n' '  make rtc-route-probe          Run WebRTC STUN/TURN route probe'
	@printf '%s\n' '  make build-local              Build binary on this machine'
	@printf '%s\n' '  make package-ubuntu22-amd64   Build Ubuntu 22.04 x86_64 package via Docker buildx'
	@printf '%s\n' '  make clean-dist               Remove go_voice_gateway/dist'

test:
	go test ./...

print-config:
	go run . --print-config

print-env-help:
	go run . --print-env-help

rtc-route-probe:
	go run ./cmd/rtc_route_probe

build-local:
	mkdir -p "$(DIST_DIR)"
	go build -trimpath -o "$(DIST_DIR)/go_voice_gateway" .

package-ubuntu22-amd64:
	docker buildx build \
		--platform linux/amd64 \
		--build-arg GO_IMAGE="$(GO_IMAGE)" \
		--file Dockerfile.buildx.ubuntu22 \
		--target export \
		--output type=local,dest="$(UBUNTU22_AMD64_BUILD_OUT)" \
		.
	rm -rf "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)"
	mkdir -p "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)"
	cp "$(UBUNTU22_AMD64_BUILD_OUT)/go_voice_gateway" "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)/go_voice_gateway"
	cp Dockerfile.ubuntu22 "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)/Dockerfile.ubuntu22"
	printf '%s\n' \
		'#!/usr/bin/env bash' \
		'set -euo pipefail' \
		'DIR="$$(cd "$$(dirname "$$0")" && pwd)"' \
		': "$${GO_VOICE_GATEWAY_ADDR:=0.0.0.0:8282}"' \
		': "$${GO_VOICE_GATEWAY_RTC_ENABLED:=true}"' \
		': "$${GO_VOICE_GATEWAY_ASR_PROCESSOR:=python_gateway}"' \
		': "$${GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL:=ws://127.0.0.1:7860/ws}"' \
		': "$${GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE:=session}"' \
		': "$${GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS:=30000}"' \
		': "$${GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT:=webrtc_rtp}"' \
		': "$${GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY:=all}"' \
		': "$${GO_VOICE_GATEWAY_ICE_NETWORK_TYPES:=udp4}"' \
		': "$${GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT:=35500}"' \
		': "$${GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT:=35600}"' \
		'export GO_VOICE_GATEWAY_ADDR GO_VOICE_GATEWAY_RTC_ENABLED GO_VOICE_GATEWAY_ASR_PROCESSOR GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CONNECTION_MODE GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_SESSION_IDLE_RECYCLE_MS GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT GO_VOICE_GATEWAY_ICE_TRANSPORT_POLICY GO_VOICE_GATEWAY_ICE_NETWORK_TYPES GO_VOICE_GATEWAY_RTC_UDP_MIN_PORT GO_VOICE_GATEWAY_RTC_UDP_MAX_PORT' \
		'exec "$$DIR/go_voice_gateway" "$$@"' \
		> "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)/run.sh"
	chmod +x "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)/go_voice_gateway" "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME)/run.sh"
	tar -C "$(DIST_DIR)" -czf "$(DIST_DIR)/$(UBUNTU22_AMD64_PACKAGE_NAME).tar.gz" "$(UBUNTU22_AMD64_PACKAGE_NAME)"

clean-dist:
	rm -rf "$(DIST_DIR)"
