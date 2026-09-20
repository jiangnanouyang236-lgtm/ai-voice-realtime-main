SHELL := /usr/bin/env bash

DIST_DIR ?= dist
LOCAL_PACKAGE_NAME ?= rust_client-local
BOARD_DEV_PACKAGE_NAME ?= rust_client-board-dev
BOARD_RELEASE_PACKAGE_NAME ?= rust_client-board-release
UBUNTU22_ARM64_DEV_PACKAGE_NAME ?= rust_client-ubuntu22-arm64-dev
UBUNTU22_ARM64_RELEASE_PACKAGE_NAME ?= rust_client-ubuntu22-arm64-release
UBUNTU22_ARM64_DEV_BUILD_OUT ?= $(DIST_DIR)/buildx-ubuntu22-arm64-dev
UBUNTU22_ARM64_RELEASE_BUILD_OUT ?= $(DIST_DIR)/buildx-ubuntu22-arm64-release
RUST_VERSION ?= stable
CARGO_FEATURES ?= native-webrtc
CARGO_FEATURE_ARGS := $(if $(strip $(CARGO_FEATURES)),--features "$(CARGO_FEATURES)",)

.PHONY: help test clippy print-config print-env-help build-dev build-release package-local package-board-dev package-board-release package-ubuntu22-arm64-dev package-ubuntu22-arm64-release package-ubuntu22-arm64 clean-dist

help:
	@printf '%s\n' 'Rust Client build targets:'
	@printf '%s\n' '  make test                    Run Rust native WebRTC tests'
	@printf '%s\n' '  make clippy                  Run Rust clippy with warnings denied'
	@printf '%s\n' '  make print-config            Print effective config with secrets redacted'
	@printf '%s\n' '  make print-env-help          Print environment variable mapping'
	@printf '%s\n' '  make build-dev               Build debug/dev binary on this machine'
	@printf '%s\n' '  make build-release           Build release binary on this machine'
	@printf '%s\n' '  make package-board-dev       Build/package dev binary on the current board/machine'
	@printf '%s\n' '  make package-board-release   Build/package release binary on the current board/machine'
	@printf '%s\n' '  make package-ubuntu22-arm64-dev      Build Ubuntu 22.04 ARM64 dev package via Docker buildx'
	@printf '%s\n' '  make package-ubuntu22-arm64-release  Build Ubuntu 22.04 ARM64 release package via Docker buildx'
	@printf '%s\n' '  make package-ubuntu22-arm64          Alias of package-ubuntu22-arm64-release'
	@printf '%s\n' '  make clean-dist              Remove rust_client/dist'

test:
	cargo test $(CARGO_FEATURE_ARGS)

clippy:
	cargo clippy $(CARGO_FEATURE_ARGS) -- -D warnings

print-config:
	: "$${ROBOT_ID:?ROBOT_ID is required for rust_client config}"
	cargo run --bin rust_client $(CARGO_FEATURE_ARGS) -- --print-config

print-env-help:
	cargo run --bin rust_client $(CARGO_FEATURE_ARGS) -- --print-env-help

build-dev:
	cargo build $(CARGO_FEATURE_ARGS)

build-release:
	cargo build --release $(CARGO_FEATURE_ARGS)

package-local: build-release
	./scripts/package_release.sh target/release/rust_client "$(DIST_DIR)/$(LOCAL_PACKAGE_NAME)"

package-board-dev: build-dev
	./scripts/package_release.sh target/debug/rust_client "$(DIST_DIR)/$(BOARD_DEV_PACKAGE_NAME)"

package-board-release: build-release
	./scripts/package_release.sh target/release/rust_client "$(DIST_DIR)/$(BOARD_RELEASE_PACKAGE_NAME)"

package-ubuntu22-arm64-dev:
	docker buildx build \
		--platform linux/arm64 \
		--build-arg RUST_VERSION="$(RUST_VERSION)" \
		--build-arg CARGO_PROFILE=debug \
		--build-arg CARGO_FEATURES="$(CARGO_FEATURES)" \
		--file Dockerfile.ubuntu22 \
		--target export \
		--output type=local,dest="$(UBUNTU22_ARM64_DEV_BUILD_OUT)" \
		.
	./scripts/package_release.sh "$(UBUNTU22_ARM64_DEV_BUILD_OUT)/rust_client" "$(DIST_DIR)/$(UBUNTU22_ARM64_DEV_PACKAGE_NAME)"

package-ubuntu22-arm64-release:
	docker buildx build \
		--platform linux/arm64 \
		--build-arg RUST_VERSION="$(RUST_VERSION)" \
		--build-arg CARGO_PROFILE=release \
		--build-arg CARGO_FEATURES="$(CARGO_FEATURES)" \
		--file Dockerfile.ubuntu22 \
		--target export \
		--output type=local,dest="$(UBUNTU22_ARM64_RELEASE_BUILD_OUT)" \
		.
	./scripts/package_release.sh "$(UBUNTU22_ARM64_RELEASE_BUILD_OUT)/rust_client" "$(DIST_DIR)/$(UBUNTU22_ARM64_RELEASE_PACKAGE_NAME)"

package-ubuntu22-arm64: package-ubuntu22-arm64-release

clean-dist:
	rm -rf "$(DIST_DIR)"
