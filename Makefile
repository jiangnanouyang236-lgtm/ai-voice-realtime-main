SHELL := /usr/bin/env bash

GO_GATEWAY_DIR := go_voice_gateway
RUST_CLIENT_DIR := rust_client
COMPOSE_FILE ?= docker-compose.v3.yml
COMPOSE_BUSINESS_FILE ?= deploy/compose/business.yml
COMPOSE_GO_GATEWAY_FILE ?= deploy/compose/go-gateway.host.yml
COMPOSE_MCP_FILE ?= deploy/compose/mcp.yml
COMPOSE_V4_FILE ?= docker-compose.v4.yml
COMPOSE_V4_DEV_FILE ?= docker-compose.v4.dev.yml
MODEL_COMPOSE_FILE ?= deploy/vllm/docker-compose.models.yml
MODEL_ENV_FILE ?= deploy/vllm/.env.models
AI_VOICE_DOCKER_NETWORK ?= wzk-ai-net

.PHONY: help check check-v3 preflight-offline preflight-lan preflight-compose-v4 repo-hygiene repo-hygiene-index evidence-check test-python weak-network-check package-v3-release clean-v3-dist clean-local-artifacts print-v3-config-help compose-network compose-v3-validate compose-v3-config compose-v3-up compose-v3-logs compose-v3-smoke compose-v3-down compose-business-config compose-business-up compose-business-logs compose-business-down compose-go-config compose-go-up compose-go-logs compose-go-down compose-mcp-config compose-mcp-up compose-mcp-logs compose-mcp-down compose-v4-config compose-v4-dev-config compose-v4-build compose-v4-up compose-v4-dev-up compose-v4-logs compose-v4-down models-config models-up models-logs models-down

help:
	@printf '%s\n' 'AI Voice workspace targets:'
	@printf '%s\n' '  make check                 Run the current repository gate (compatibility alias: check-v3)'
	@printf '%s\n' '  make check-v3              Run repository, Go, Rust, Python, and Compose gates'
	@printf '%s\n' '  make preflight-offline     Validate repository/Harness without private services'
	@printf '%s\n' '  make preflight-lan         Validate current private LAN configuration (no network probe)'
	@printf '%s\n' '  make preflight-compose-v4  Validate V4 Compose inputs without a network probe'
	@printf '%s\n' '  make repo-hygiene          Reject tracked secrets, generated files, and misplaced docs'
	@printf '%s\n' '  make repo-hygiene-index    Check the exact staged tree before commit'
	@printf '%s\n' '  make evidence-check EVIDENCE_REPORT=reports/evidence.json  Validate a claimed evidence level'
	@printf '%s\n' '  make test-python           Run the canonical Python test tree'
	@printf '%s\n' '  make weak-network-check   Run repeatable WebRTC weak-network test matrix'
	@printf '%s\n' '  make package-v3-release    Build Go linux/amd64 and Rust linux/arm64 release packages via buildx'
	@printf '%s\n' '  make clean-v3-dist         Remove Go/Rust package dist directories'
	@printf '%s\n' '  make clean-local-artifacts Remove caches, compiler workdirs, logs, and OS junk'
	@printf '%s\n' '  make print-v3-config-help  Show config inspection commands'
	@printf '%s\n' '  make compose-network       Ensure shared Docker network exists'
	@printf '%s\n' '  make compose-v3-validate   Validate split Docker Compose v3 config without starting services'
	@printf '%s\n' '  make compose-v3-config     Render Docker Compose v3 config'
	@printf '%s\n' '  make compose-v3-up         Start Docker Compose v3 core stack from existing images'
	@printf '%s\n' '  make compose-v3-logs       Follow Go/Python Gateway compose logs'
	@printf '%s\n' '  make compose-v3-smoke      Smoke-check published compose ports'
	@printf '%s\n' '  make compose-v3-down       Stop Docker Compose v3 stack'
	@printf '%s\n' '  make compose-business-up   Start only Python business services'
	@printf '%s\n' '  make compose-go-up         Start only Go Gateway host-network entry from existing image'
	@printf '%s\n' '  make compose-mcp-up        Start optional MCP services'
	@printf '%s\n' '  make compose-v4-config     Render V4 split Python services'
	@printf '%s\n' '  make compose-v4-build      Build the shared V4 Python image'
	@printf '%s\n' '  make compose-v4-up         Start V4 without source mounts'
	@printf '%s\n' '  make compose-v4-dev-up     Start V4 with read-only source mounts'
	@printf '%s\n' '  make models-config         Render vLLM model-layer compose config'
	@printf '%s\n' '  make models-up             Start vLLM model-layer compose stack'
	@printf '%s\n' '  make models-logs           Follow vLLM model-layer logs'
	@printf '%s\n' '  make models-down           Stop vLLM model-layer compose stack'

check-v3: preflight-offline repo-hygiene
	$(MAKE) -C "$(GO_GATEWAY_DIR)" test
	$(MAKE) -C "$(RUST_CLIENT_DIR)" test
	python -m pytest test/ -q
	python scripts/validate_compose_v3.py

check: check-v3

preflight-offline:
	python scripts/ai_preflight.py --profile offline

preflight-lan:
	python scripts/ai_preflight.py --profile lan

preflight-compose-v4:
	python scripts/ai_preflight.py --profile compose-v4

repo-hygiene:
	python scripts/check_repo_hygiene.py

repo-hygiene-index:
	python scripts/check_repo_hygiene.py --index

evidence-check:
	@test -n "$(EVIDENCE_REPORT)" || { printf '%s\n' 'EVIDENCE_REPORT is required'; exit 2; }
	python scripts/validate_evidence_report.py "$(EVIDENCE_REPORT)"

test-python:
	python -m pytest test/ -q

weak-network-check:
	python scripts/webrtc_weak_network_matrix.py

package-v3-release:
	$(MAKE) -C "$(GO_GATEWAY_DIR)" package-ubuntu22-amd64
	$(MAKE) -C "$(RUST_CLIENT_DIR)" package-ubuntu22-arm64-release

clean-v3-dist:
	$(MAKE) -C "$(GO_GATEWAY_DIR)" clean-dist
	$(MAKE) -C "$(RUST_CLIENT_DIR)" clean-dist

clean-local-artifacts:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '.DS_Store' -type f -delete
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	rm -rf logs run
	rm -rf "$(RUST_CLIENT_DIR)/target"
	rm -rf esp32-s3-usb-aec/build esp32-s3-usb-aec/build-aec-off esp32-s3-usb-aec/build-clean esp32-s3-usb-aec/.idf_component_cache
	rm -rf admin-ui/frontend/.vite
	rm -f gateway/nohup.out esp32-s3-usb-aec/test.log

print-v3-config-help:
	@printf '%s\n' 'Go Gateway effective config:'
	@printf '%s\n' '  cd go_voice_gateway/dist/go_voice_gateway-ubuntu22-amd64 && ./run.sh --print-config'
	@printf '%s\n' '  cd go_voice_gateway && go run . --print-config'
	@printf '%s\n' 'Go Gateway env mapping:'
	@printf '%s\n' '  cd go_voice_gateway/dist/go_voice_gateway-ubuntu22-amd64 && ./run.sh --print-env-help'
	@printf '%s\n' '  cd go_voice_gateway && go run . --print-env-help'
	@printf '%s\n' ''
	@printf '%s\n' 'Rust client effective config:'
	@printf '%s\n' '  cd rust_client && ROBOT_ID=test_01 ROBOT_SECRET=... cargo run --bin rust_client --features native-webrtc -- --print-config'
	@printf '%s\n' '  cd rust_client/dist/rust_client-ubuntu22-arm64-release && ROBOT_ID=test_01 ROBOT_SECRET=... ./run.sh --print-config'
	@printf '%s\n' 'Rust client env mapping:'
	@printf '%s\n' '  cd rust_client && cargo run --bin rust_client --features native-webrtc -- --print-env-help'
	@printf '%s\n' '  cd rust_client/dist/rust_client-ubuntu22-arm64-release && ./run.sh --print-env-help'

compose-network:
	@docker network inspect "$(AI_VOICE_DOCKER_NETWORK)" >/dev/null 2>&1 || docker network create "$(AI_VOICE_DOCKER_NETWORK)"

compose-v3-validate:
	python scripts/validate_compose_v3.py

compose-v3-config:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" -f "$(COMPOSE_GO_GATEWAY_FILE)" config

compose-v3-up: compose-network
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" -f "$(COMPOSE_GO_GATEWAY_FILE)" up -d

compose-v3-logs:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" -f "$(COMPOSE_GO_GATEWAY_FILE)" logs -f go-gateway python-gateway

compose-v3-smoke:
	python scripts/smoke_compose_v3.py

compose-v3-down:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" -f "$(COMPOSE_GO_GATEWAY_FILE)" -f "$(COMPOSE_MCP_FILE)" --profile mcp down

compose-business-config:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" config

compose-business-up: compose-network
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" up -d --build

compose-business-logs:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" logs -f python-gateway stt llm tts admin

compose-business-down:
	docker compose -f "$(COMPOSE_BUSINESS_FILE)" down

compose-go-config:
	docker compose -f "$(COMPOSE_GO_GATEWAY_FILE)" config

compose-go-up:
	docker compose -f "$(COMPOSE_GO_GATEWAY_FILE)" up -d

compose-go-logs:
	docker compose -f "$(COMPOSE_GO_GATEWAY_FILE)" logs -f go-gateway

compose-go-down:
	docker compose -f "$(COMPOSE_GO_GATEWAY_FILE)" down

compose-mcp-config:
	docker compose -f "$(COMPOSE_MCP_FILE)" --profile mcp config

compose-mcp-up: compose-network
	docker compose -f "$(COMPOSE_MCP_FILE)" --profile mcp up -d --build

compose-mcp-logs:
	docker compose -f "$(COMPOSE_MCP_FILE)" --profile mcp logs -f mcp-utils mcp-robot mcp-singing

compose-mcp-down:
	docker compose -f "$(COMPOSE_MCP_FILE)" --profile mcp down

compose-v4-config:
	docker compose -f "$(COMPOSE_V4_FILE)" --profile admin --profile mcp config

compose-v4-dev-config:
	docker compose -f "$(COMPOSE_V4_FILE)" -f "$(COMPOSE_V4_DEV_FILE)" --profile admin --profile mcp config

compose-v4-build:
	docker compose -f "$(COMPOSE_V4_FILE)" build

compose-v4-up:
	docker compose -f "$(COMPOSE_V4_FILE)" up -d

compose-v4-dev-up:
	docker compose -f "$(COMPOSE_V4_FILE)" -f "$(COMPOSE_V4_DEV_FILE)" up -d

compose-v4-logs:
	docker compose -f "$(COMPOSE_V4_FILE)" logs -f wzk-python-gateway wzk-stt-grpc wzk-llm-grpc wzk-tts-grpc

compose-v4-down:
	docker compose -f "$(COMPOSE_V4_FILE)" --profile admin --profile mcp down

models-config:
	@test -f "$(MODEL_ENV_FILE)" || { printf '%s\n' "Missing $(MODEL_ENV_FILE). Copy deploy/vllm/.env.models.example first."; exit 1; }
	docker compose --env-file "$(MODEL_ENV_FILE)" -f "$(MODEL_COMPOSE_FILE)" config

models-up: compose-network
	@test -f "$(MODEL_ENV_FILE)" || { printf '%s\n' "Missing $(MODEL_ENV_FILE). Copy deploy/vllm/.env.models.example first."; exit 1; }
	docker compose --env-file "$(MODEL_ENV_FILE)" -f "$(MODEL_COMPOSE_FILE)" up -d --build

models-logs:
	docker compose --env-file "$(MODEL_ENV_FILE)" -f "$(MODEL_COMPOSE_FILE)" logs -f vllm-stt vllm-llm vllm-router vllm-tts

models-down:
	docker compose --env-file "$(MODEL_ENV_FILE)" -f "$(MODEL_COMPOSE_FILE)" down
