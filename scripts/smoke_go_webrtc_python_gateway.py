#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_PACKETS_PATH = Path("/private/tmp/dify_stream_test_live_opus_packets.json")
DEFAULT_LOG_DIR = Path("/private/tmp/dify_stream_test_live_smoke_logs")
DEFAULT_PYDEPS = Path("/private/tmp/dify_stream_test_pydeps")
DEFAULT_OPUS_LIB_DIR = Path("/opt/homebrew/lib")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real Go WebRTC -> Python Gateway -> STT/LLM/TTS voice smoke."
    )
    parser.add_argument("--sample", type=Path, required=True, help="16 kHz mono PCM WAV input")
    parser.add_argument("--packets-path", type=Path, default=DEFAULT_PACKETS_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--python-deps", type=Path, default=DEFAULT_PYDEPS)
    parser.add_argument("--opus-lib-dir", type=Path, default=DEFAULT_OPUS_LIB_DIR)
    parser.add_argument("--robot-id", default="test_01")
    parser.add_argument("--bot-id", default="xiaowen")
    parser.add_argument("--go-addr", default="127.0.0.1:8282")
    parser.add_argument("--gateway-ws-url", default="ws://127.0.0.1:7860/ws")
    parser.add_argument(
        "--client",
        choices=("go", "rust", "rust-app", "services"),
        default="go",
        help="Client implementation to run after services are ready.",
    )
    parser.add_argument(
        "--service-runtime-sec",
        type=float,
        default=0.0,
        help=(
            "For --client services, keep the service stack running for this many "
            "seconds. The default 0 runs until Ctrl-C."
        ),
    )
    parser.add_argument(
        "--rust-app-runtime-sec",
        type=float,
        default=0.0,
        help=(
            "For --client rust-app, stop the Rust app after this many seconds and "
            "treat a still-running app as a successful bounded startup check. "
            "The default 0 runs until the app exits or the user presses Ctrl-C."
        ),
    )
    parser.add_argument("--assume-services-running", action="store_true")
    parser.add_argument(
        "--require-robot-secret",
        action="store_true",
        help="Do not override GATEWAY_REQUIRE_ROBOT_SECRET=false for the Python Gateway smoke.",
    )
    return parser.parse_args()


def load_env(args: argparse.Namespace) -> dict[str, str]:
    explicit_env = os.environ.copy()
    env = explicit_env.copy()
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for raw in dotenv.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            if explicit_env.get(key):
                continue
            env[key] = value

    private_identity = load_private_robot_identity(ROOT / "rust_client" / ".env.local")
    configured_robot_id = private_identity.get("ROBOT_ID", "").strip()
    if configured_robot_id and configured_robot_id != args.robot_id:
        raise RuntimeError(
            "rust_client/.env.local ROBOT_ID does not match --robot-id; "
            "refusing to reuse credentials for a different Robot"
        )
    for key, value in private_identity.items():
        if not env.get(key, "").strip():
            env[key] = value

    env["PYTHONUNBUFFERED"] = "1"
    if args.python_deps.exists():
        env["PYTHONPATH"] = prepend_path(env.get("PYTHONPATH", ""), args.python_deps)
    if args.opus_lib_dir.exists():
        env["DYLD_LIBRARY_PATH"] = prepend_path(env.get("DYLD_LIBRARY_PATH", ""), args.opus_lib_dir)
    if not args.require_robot_secret:
        env["GATEWAY_REQUIRE_ROBOT_SECRET"] = "false"
        env["GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET"] = "false"
    else:
        env["GATEWAY_REQUIRE_ROBOT_SECRET"] = "true"
        env["GO_VOICE_GATEWAY_REQUIRE_ROBOT_SECRET"] = "true"

    env["ROBOT_ID"] = args.robot_id
    env.setdefault("ROBOT_SECRET", "")
    if args.require_robot_secret and not env["ROBOT_SECRET"].strip():
        raise RuntimeError(
            "--require-robot-secret needs ROBOT_SECRET in the process environment, "
            ".env, or rust_client/.env.local"
        )
    env["GO_VOICE_GATEWAY_ADDR"] = args.go_addr
    env["GO_VOICE_GATEWAY_ASR_PROCESSOR"] = "python_gateway"
    env["GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_WS_URL"] = args.gateway_ws_url
    env["GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_ID"] = args.robot_id
    if env.get("ROBOT_SECRET") and not env.get("GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET"):
        env["GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_ROBOT_SECRET"] = env["ROBOT_SECRET"]
    env["GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_CLIENT_TYPE"] = "go_voice_gateway"
    env["GO_VOICE_GATEWAY_ASR_PYTHON_GATEWAY_TIMEOUT_MS"] = "75000"
    env["GO_VOICE_GATEWAY_BOT_ID"] = args.bot_id
    if args.client in {"rust", "rust-app", "services"}:
        env.setdefault("GO_VOICE_GATEWAY_DOWNLINK_AUDIO_TRANSPORT", "webrtc_rtp")

    env["GO_LIVE_SMOKE_WS_URL"] = f"ws://{args.go_addr}/ws"
    env["GO_LIVE_SMOKE_ORIGIN"] = f"http://{args.go_addr}"
    env["GO_LIVE_SMOKE_ROBOT_ID"] = args.robot_id
    env["GO_LIVE_SMOKE_BOT_ID"] = args.bot_id
    env["LIVE_OPUS_PACKETS_PATH"] = str(args.packets_path)
    env.setdefault("GOCACHE", "/private/tmp/dify_stream_test_go_build_cache")

    env["GATEWAY_URL"] = f"ws://{args.go_addr}/ws"
    env["ROBOT_ID"] = args.robot_id
    env.setdefault("ROBOT_SECRET", "")
    env["BOT_ID"] = args.bot_id
    env["TRANSPORT_POLICY"] = "webrtc_only"
    env["WEBRTC_ENABLED"] = "true"
    env["TRANSPORT_FALLBACK_ENABLED"] = "false"
    env["WEBRTC_OFFER_FACTORY"] = "native"
    env["WEBRTC_NATIVE_GATHER_TIMEOUT_MS"] = "5000"
    env["WEBRTC_NATIVE_RTP_PROBE_ENABLED"] = "false"
    env["RTC_AUDIO_UPLINK"] = "rtp_only"
    env["RUST_LIVE_SMOKE_SAMPLE"] = str(args.sample)
    env.setdefault("RUST_LOG", "info")
    return env


def load_private_robot_identity(path: Path) -> dict[str, str]:
    """Load only the private Robot identity fields used by this smoke."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    allowed = {"ROBOT_ID", "ROBOT_SECRET"}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in allowed:
            values[key] = value.strip().strip("'").strip('"')
    return values


def prepend_path(existing: str, path: Path) -> str:
    return str(path) + (os.pathsep + existing if existing else "")


def ensure_opus_import(args: argparse.Namespace):
    if args.python_deps.exists():
        sys.path.insert(0, str(args.python_deps))
    if args.opus_lib_dir.exists():
        os.environ["DYLD_LIBRARY_PATH"] = prepend_path(
            os.environ.get("DYLD_LIBRARY_PATH", ""),
            args.opus_lib_dir,
        )
    try:
        import opuslib  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "opuslib is required to encode the smoke WAV. Install it in the active env "
            "or pass --python-deps pointing at a target directory containing opuslib."
        ) from exc
    return opuslib


def encode_sample(args: argparse.Namespace) -> None:
    opuslib = ensure_opus_import(args)
    with wave.open(str(args.sample), "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()
        pcm = wf.readframes(wf.getnframes())
    if channels != 1 or sample_rate != 16000 or sample_width != 2:
        raise RuntimeError(
            f"unexpected sample format channels={channels} rate={sample_rate} width={sample_width}; "
            "expected mono 16kHz PCM16 WAV"
        )

    frame_samples = int(sample_rate * 20 / 1000)
    frame_bytes = frame_samples * channels * sample_width
    encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_AUDIO)
    packets: list[str] = []
    for offset in range(0, len(pcm), frame_bytes):
        frame = pcm[offset:offset + frame_bytes]
        if len(frame) < frame_bytes:
            frame += b"\x00" * (frame_bytes - len(frame))
        packet = encoder.encode(frame, frame_samples)
        packets.append(base64.b64encode(packet).decode("ascii"))
    args.packets_path.parent.mkdir(parents=True, exist_ok=True)
    args.packets_path.write_text(json.dumps({"packets": packets}), encoding="utf-8")
    print(f"encoded_packets={len(packets)} sample={args.sample}")


def wait_port(port: int, timeout: float = 45.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise TimeoutError(f"port {port} not ready")


def start(name: str, cmd: list[str], *, cwd: Path, env: dict[str, str], log_dir: Path) -> subprocess.Popen:
    print(f"start {name}: {' '.join(cmd)}")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_handle = (log_dir / f"{name}.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    log_handle.close()
    return proc


def collect_tail(log_dir: Path, name: str, max_lines: int = 80) -> list[str]:
    path = log_dir / f"{name}.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]


def stop_all(procs: list[tuple[str, subprocess.Popen]]) -> None:
    for _name, proc in reversed(procs):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.time() + 8
    for _name, proc in reversed(procs):
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def service_specs(
    go_addr: str, env: dict[str, str]
) -> list[tuple[str, list[str], Path, int, float]]:
    go_port = int(go_addr.rsplit(":", 1)[1])
    return [
        ("mcp_robot", [sys.executable, "mcp_servers/robot_sse_server.py"], ROOT, 5003, 45.0),
        ("mcp_singing", [sys.executable, "mcp_servers/singing_sse_server.py"], ROOT, 5005, 45.0),
        ("mcp_utils", [sys.executable, "mcp_servers/utils_sse_server.py"], ROOT, 5004, 45.0),
        ("stt", [sys.executable, "stt/stt_grpc_server.py"], ROOT, int(env.get("STT_GRPC_SERVER_PORT", "50054")), 45.0),
        ("llm", [sys.executable, "llm/llm_grpc_server.py"], ROOT, int(env.get("LLM_GRPC_SERVER_PORT", "50053")), 75.0),
        ("tts", [sys.executable, "tts/tts_grpc_server.py"], ROOT, int(env.get("TTS_GRPC_SERVER_PORT", "50052")), 45.0),
        ("python_gateway", [sys.executable, "gateway/gateway_server.py"], ROOT, int(env.get("GATEWAY_BIND_PORT", "7860")), 75.0),
        ("go_gateway", ["go", "run", "."], ROOT / "go_voice_gateway", go_port, 45.0),
    ]


def run_go_client(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["go", "run", "./cmd/live_speech_smoke"],
        cwd=str(ROOT / "go_voice_gateway"),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
    )


def run_rust_client(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["cargo", "run", "--features", "native-webrtc", "--bin", "live_speech_smoke"],
        cwd=str(ROOT / "rust_client"),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=150,
    )


def generate_barge_in_wav(env: dict[str, str], output_path: Path) -> dict[str, object] | None:
    text = env.get("RUST_LIVE_SMOKE_BARGE_IN_TEXT", "").strip()
    if not text:
        return None
    profile_id = env.get("RUST_LIVE_SMOKE_BARGE_IN_TTS_PROFILE_ID", "wzk-jialan").strip()
    if not profile_id:
        raise RuntimeError("RUST_LIVE_SMOKE_BARGE_IN_TTS_PROFILE_ID must not be empty")

    import grpc
    from tts import tts_service_pb2, tts_service_pb2_grpc

    target = f"127.0.0.1:{int(env.get('TTS_GRPC_SERVER_PORT', '50052'))}"
    channel = grpc.insecure_channel(target)
    stub = tts_service_pb2_grpc.TTSServiceStub(channel)
    session_id = f"barge-fixture-{time.time_ns()}"

    def requests():
        yield tts_service_pb2.TextChunk(
            text=text,
            is_final=False,
            session_id=session_id,
            config=tts_service_pb2.TTSConfig(tts_profile_id=profile_id),
        )
        yield tts_service_pb2.TextChunk(text="", is_final=True, session_id=session_id)

    pcm = bytearray()
    sample_rate = 0
    channels = 0
    sample_width = 0
    chunks = 0
    try:
        for chunk in stub.StreamTextToSpeech(requests(), timeout=60):
            if not chunk.audio_data:
                continue
            if sample_rate and sample_rate != chunk.sample_rate:
                raise RuntimeError("generated barge-in sample rate changed within one stream")
            sample_rate = chunk.sample_rate
            channels = chunk.channels
            sample_width = chunk.sample_width
            pcm.extend(chunk.audio_data)
            chunks += 1
    finally:
        channel.close()
    if not pcm:
        raise RuntimeError("TTS-Base generated no barge-in audio")
    if (sample_rate, channels, sample_width) != (16000, 1, 2):
        raise RuntimeError(
            "TTS-Base barge-in fixture must be 16kHz mono PCM16, "
            f"got rate={sample_rate} channels={channels} width={sample_width}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm))
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    manifest = {
        "source_kind": "tts_base_generated_user_simulation",
        "text": text,
        "tts_profile_id": profile_id,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "chunks": chunks,
        "pcm_bytes": len(pcm),
        "duration_ms": round(len(pcm) / (sample_rate * channels * sample_width) * 1000, 1),
        "wav_path": str(output_path),
        "wav_sha256": digest,
    }
    manifest_path = output_path.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    env["RUST_LIVE_SMOKE_BARGE_IN_SAMPLE"] = str(output_path)
    print(
        "barge_in_fixture_generated "
        f"profile={profile_id} duration_ms={manifest['duration_ms']} sha256={digest}"
    )
    return manifest


def run_rust_app(env: dict[str, str], runtime_sec: float) -> int:
    cmd = ["cargo", "run", "--features", "native-webrtc", "--bin", "rust_client"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT / "rust_client"),
        env=env,
        text=True,
        start_new_session=True,
    )
    try:
        if runtime_sec <= 0:
            return proc.wait()

        deadline = time.time() + runtime_sec
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.25)
        if proc.poll() is not None:
            return proc.returncode or 0

        print(f"rust_app_runtime_reached={runtime_sec:.1f}s; stopping app")
        stop_process_group(proc, signal.SIGINT, timeout=8.0)
        return 0
    except KeyboardInterrupt:
        print("rust_app_interrupted=true; stopping app")
        stop_process_group(proc, signal.SIGINT, timeout=8.0)
        return 130


def stop_process_group(proc: subprocess.Popen, sig: signal.Signals, timeout: float) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return


def run_services_only(args: argparse.Namespace) -> int:
    print(f"services_ready=true go_ws=ws://{args.go_addr}/ws")
    if args.go_addr.startswith("0.0.0.0:"):
        port = args.go_addr.rsplit(":", 1)[1]
        print(f"remote_rust_gateway_url=ws://<this-machine-lan-ip>:{port}/ws")
    print(f"service_log_dir={args.log_dir}")
    try:
        if args.service_runtime_sec <= 0:
            while True:
                time.sleep(3600)
        else:
            time.sleep(args.service_runtime_sec)
        return 0
    except KeyboardInterrupt:
        print("services_interrupted=true")
        return 130


def main() -> int:
    args = parse_args()
    env = load_env(args)
    if args.client == "go":
        encode_sample(args)
    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        if not args.assume_services_running:
            for name, cmd, cwd, port, timeout in service_specs(args.go_addr, env):
                proc = start(name, cmd, cwd=cwd, env=env, log_dir=args.log_dir)
                procs.append((name, proc))
                wait_port(port, timeout=timeout)
                print(f"ready {name} port={port}")

        if args.client == "rust":
            generate_barge_in_wav(env, args.log_dir.parent / "barge-in-input-16k.wav")

        print(f"run {args.client} live client")
        if args.client == "services":
            return run_services_only(args)

        if args.client == "rust-app":
            exit_code = run_rust_app(env, args.rust_app_runtime_sec)
            if exit_code != 0:
                print(f"{args.client}_client_exit={exit_code}")
                for name, _proc in procs:
                    print(f"--- {name} tail ---")
                    print("\n".join(collect_tail(args.log_dir, name, 60)))
            return exit_code

        client = run_go_client(env) if args.client == "go" else run_rust_client(env)
        args.log_dir.mkdir(parents=True, exist_ok=True)
        (args.log_dir / f"{args.client}_client.log").write_text(
            client.stdout, encoding="utf-8"
        )
        print(f"{args.client}_client_output_begin")
        print(client.stdout.rstrip())
        print(f"{args.client}_client_output_end")
        if client.returncode != 0:
            print(f"{args.client}_client_exit={client.returncode}")
            for name, _proc in procs:
                print(f"--- {name} tail ---")
                print("\n".join(collect_tail(args.log_dir, name, 60)))
            return client.returncode
        return 0
    finally:
        stop_all(procs)
        print(f"log_dir={args.log_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
