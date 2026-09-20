#!/usr/bin/env python3
"""Repeatable macOS USB audio smoke test for the ESP32-S3 UAC device."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import threading
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from scipy.signal import butter, correlate, correlation_lags, sosfilt


SCHEMA_VERSION = 1


@dataclass
class StreamStatus:
    input_overflow: int = 0
    input_underflow: int = 0
    output_overflow: int = 0
    output_underflow: int = 0
    callback_events: int = 0

    def note(self, status: sd.CallbackFlags) -> None:
        if not status:
            return
        self.callback_events += 1
        self.input_overflow += int(status.input_overflow)
        self.input_underflow += int(status.input_underflow)
        self.output_overflow += int(status.output_overflow)
        self.output_underflow += int(status.output_underflow)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def device_dict(index: int, info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "name",
        "hostapi",
        "max_input_channels",
        "max_output_channels",
        "default_low_input_latency",
        "default_low_output_latency",
        "default_high_input_latency",
        "default_high_output_latency",
        "default_samplerate",
    )
    return {"index": index, **{key: info[key] for key in keys}}


def find_device(name: str, direction: str) -> tuple[int, dict[str, Any]]:
    matches: list[tuple[int, dict[str, Any]]] = []
    channel_key = "max_input_channels" if direction == "input" else "max_output_channels"
    for index, info in enumerate(sd.query_devices()):
        if info["name"] == name and int(info[channel_key]) > 0:
            matches.append((index, dict(info)))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one {direction} device named {name!r}, found {len(matches)}"
        )
    return matches[0]


def check_coreaudio_metadata(input_name: str, output_name: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["system_profiler", "SPAudioDataType", "-json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return {
        "command_ok": proc.returncode == 0,
        "input_name_present": input_name in proc.stdout,
        "output_name_present": output_name in proc.stdout,
        "stderr": proc.stderr.strip(),
    }


def status_passed(status: StreamStatus) -> bool:
    return (
        status.input_overflow == 0
        and status.input_underflow == 0
        and status.output_overflow == 0
        and status.output_underflow == 0
    )


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    pcm = np.clip(np.asarray(samples), -1.0, 1.0)
    pcm16 = np.rint(pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm16.tobytes())


def pcm_metrics(samples: np.ndarray) -> dict[str, Any]:
    data = np.asarray(samples, dtype=np.float64)
    abs_data = np.abs(data)
    return {
        "rms": float(np.sqrt(np.mean(data * data))) if data.size else 0.0,
        "peak": float(np.max(abs_data)) if data.size else 0.0,
        "mean": float(np.mean(data)) if data.size else 0.0,
        "clip_samples": int(np.count_nonzero(abs_data >= 0.999)),
        "zero_fraction": float(np.mean(data == 0.0)) if data.size else 1.0,
    }


def make_test_signal(sample_rate: int, duration: float, amplitude: float) -> np.ndarray:
    t = np.arange(round(sample_rate * duration), dtype=np.float64) / sample_rate
    fade = min(0.15, duration / 4.0)
    envelope = np.minimum(1.0, t / fade) * np.minimum(1.0, (duration - t) / fade)
    rng = np.random.default_rng(0xAEC)
    noise = rng.standard_normal(t.size)
    bandpass = butter(6, (180.0, 3400.0), btype="bandpass", fs=sample_rate, output="sos")
    carrier = sosfilt(bandpass, noise)
    carrier /= max(float(np.max(np.abs(carrier))), 1e-12)
    modulation = 0.55 + 0.45 * np.sin(2.0 * np.pi * 2.7 * t) ** 2
    return (amplitude * envelope * modulation * carrier).astype(np.float32)


def run_input(
    device: int, sample_rate: int, duration: float, blocksize: int
) -> tuple[np.ndarray, StreamStatus, float]:
    total_frames = round(sample_rate * duration)
    recorded = np.zeros(total_frames, dtype=np.float32)
    status = StreamStatus()
    done = threading.Event()
    position = 0

    def callback(indata: np.ndarray, frames: int, _time: Any, flags: sd.CallbackFlags) -> None:
        nonlocal position
        status.note(flags)
        count = min(frames, total_frames - position)
        if count > 0:
            recorded[position : position + count] = indata[:count, 0]
            position += count
        if position >= total_frames:
            done.set()

    started = time.monotonic()
    with sd.InputStream(
        device=device,
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        latency="high",
        callback=callback,
    ):
        if not done.wait(duration + 5.0):
            raise RuntimeError("input stream timed out")
    return recorded, status, time.monotonic() - started


def run_output(
    device: int, sample_rate: int, signal: np.ndarray, blocksize: int
) -> tuple[StreamStatus, float]:
    status = StreamStatus()
    done = threading.Event()
    position = 0

    def callback(outdata: np.ndarray, frames: int, _time: Any, flags: sd.CallbackFlags) -> None:
        nonlocal position
        status.note(flags)
        outdata.fill(0.0)
        count = min(frames, signal.size - position)
        if count > 0:
            outdata[:count, 0] = signal[position : position + count]
            position += count
        if position >= signal.size:
            done.set()

    expected = signal.size / sample_rate
    started = time.monotonic()
    with sd.OutputStream(
        device=device,
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        latency="high",
        callback=callback,
    ):
        if not done.wait(expected + 5.0):
            raise RuntimeError("output stream timed out")
    return status, time.monotonic() - started


def run_duplex(
    input_device: int,
    output_device: int,
    sample_rate: int,
    signal: np.ndarray,
    blocksize: int,
) -> tuple[np.ndarray, StreamStatus, float]:
    recorded = np.zeros_like(signal)
    status = StreamStatus()
    done = threading.Event()
    position = 0

    def callback(
        indata: np.ndarray,
        outdata: np.ndarray,
        frames: int,
        _time: Any,
        flags: sd.CallbackFlags,
    ) -> None:
        nonlocal position
        status.note(flags)
        outdata.fill(0.0)
        count = min(frames, signal.size - position)
        if count > 0:
            outdata[:count, 0] = signal[position : position + count]
            recorded[position : position + count] = indata[:count, 0]
            position += count
        if position >= signal.size:
            done.set()

    expected = signal.size / sample_rate
    started = time.monotonic()
    with sd.Stream(
        device=(input_device, output_device),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=blocksize,
        latency="high",
        callback=callback,
    ):
        if not done.wait(expected + 5.0):
            raise RuntimeError("duplex stream timed out")
    return recorded, status, time.monotonic() - started


def echo_metrics(
    reference: np.ndarray,
    microphone: np.ndarray,
    sample_rate: int,
    skip_seconds: float = 2.0,
    max_lag_ms: float = 300.0,
) -> dict[str, Any]:
    skip = min(round(skip_seconds * sample_rate), reference.size - 1)
    x = np.asarray(reference[skip:], dtype=np.float64)
    y = np.asarray(microphone[skip:], dtype=np.float64)
    corr = correlate(y, x, mode="full", method="fft")
    lags = correlation_lags(y.size, x.size, mode="full")
    max_lag = round(max_lag_ms * sample_rate / 1000.0)
    allowed = (lags >= -max_lag) & (lags <= max_lag)
    best_index = np.flatnonzero(allowed)[np.argmax(np.abs(corr[allowed]))]
    lag = int(lags[best_index])

    if lag >= 0:
        aligned_x = x[: x.size - lag] if lag else x
        aligned_y = y[lag:]
    else:
        aligned_x = x[-lag:]
        aligned_y = y[: y.size + lag]

    x_energy = float(np.dot(aligned_x, aligned_x))
    y_energy = float(np.dot(aligned_y, aligned_y))
    gain = float(np.dot(aligned_x, aligned_y) / max(x_energy, 1e-30))
    normalized_corr = float(
        np.dot(aligned_x, aligned_y) / np.sqrt(max(x_energy * y_energy, 1e-30))
    )
    return {
        "analysis_skip_seconds": skip_seconds,
        "search_max_lag_ms": max_lag_ms,
        "best_lag_samples": lag,
        "best_lag_ms": lag * 1000.0 / sample_rate,
        "projected_gain": gain,
        "projected_residual_db": float(20.0 * np.log10(abs(gain) + 1e-30)),
        "normalized_correlation": normalized_corr,
    }


class SerialCapture:
    def __init__(self, port: str | None, baudrate: int, output: Path) -> None:
        self.port = port
        self.baudrate = baudrate
        self.output = output
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "SerialCapture":
        if self.port is None:
            return self

        def worker() -> None:
            try:
                import serial

                with serial.Serial(self.port, self.baudrate, timeout=0.2) as stream:
                    with self.output.open("wb") as log:
                        while not self._stop.is_set():
                            chunk = stream.read(4096)
                            if chunk:
                                log.write(chunk)
                                log.flush()
            except Exception as exc:  # serial failures must not hide audio results
                self.error = str(exc)

        self._thread = threading.Thread(target=worker, name="serial-capture", daemon=True)
        self._thread.start()
        time.sleep(0.3)
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def test_result(
    name: str,
    passed: bool,
    status: StreamStatus | None = None,
    **details: Any,
) -> dict[str, Any]:
    result = {"name": name, "passed": passed, **details}
    if status is not None:
        result["stream_status"] = asdict(status)
    return result


def run_test(args: argparse.Namespace) -> int:
    if platform.system() != "Darwin":
        raise RuntimeError("this test runner currently supports macOS only")

    output_dir = Path(args.output_dir or f"test-results/{datetime.now():%Y%m%d-%H%M%S}-{args.label}")
    output_dir.mkdir(parents=True, exist_ok=False)
    input_index, input_info = find_device(args.input_name, "input")
    output_index, output_info = find_device(args.output_name, "output")
    coreaudio = check_coreaudio_metadata(args.input_name, args.output_name)

    enumeration_passed = (
        round(float(input_info["default_samplerate"])) == args.sample_rate
        and round(float(output_info["default_samplerate"])) == args.sample_rate
        and int(input_info["max_input_channels"]) == 1
        and int(output_info["max_output_channels"]) == 1
        and coreaudio["input_name_present"]
        and coreaudio["output_name_present"]
    )
    tests: list[dict[str, Any]] = [
        test_result("enumeration", enumeration_passed, coreaudio=coreaudio)
    ]

    signal = make_test_signal(args.sample_rate, args.duplex_seconds, args.signal_amplitude)
    playback_signal = make_test_signal(
        args.sample_rate, args.playback_seconds, args.signal_amplitude
    )
    serial_log = output_dir / "firmware.log"

    with SerialCapture(args.serial_port, args.serial_baudrate, serial_log) as serial_capture:
        idle, idle_status, idle_elapsed = run_input(
            input_index, args.sample_rate, args.idle_seconds, args.blocksize
        )
        write_wav(output_dir / "idle_mic.wav", idle, args.sample_rate)
        idle_pcm = pcm_metrics(idle)
        tests.append(
            test_result(
                "idle_recording",
                status_passed(idle_status) and idle_pcm["clip_samples"] == 0,
                idle_status,
                elapsed_seconds=idle_elapsed,
                pcm=idle_pcm,
            )
        )

        playback_status, playback_elapsed = run_output(
            output_index, args.sample_rate, playback_signal, args.blocksize
        )
        tests.append(
            test_result(
                "playback",
                status_passed(playback_status),
                playback_status,
                elapsed_seconds=playback_elapsed,
                signal_pcm=pcm_metrics(playback_signal),
            )
        )

        microphone, duplex_status, duplex_elapsed = run_duplex(
            input_index, output_index, args.sample_rate, signal, args.blocksize
        )
        write_wav(output_dir / "duplex_reference.wav", signal, args.sample_rate)
        write_wav(output_dir / "duplex_mic.wav", microphone, args.sample_rate)
        microphone_pcm = pcm_metrics(microphone)
        echo = echo_metrics(signal, microphone, args.sample_rate)
        tests.append(
            test_result(
                "full_duplex",
                status_passed(duplex_status)
                and microphone_pcm["clip_samples"] == 0
                and microphone_pcm["rms"] > 1e-6,
                duplex_status,
                elapsed_seconds=duplex_elapsed,
                reference_pcm=pcm_metrics(signal),
                microphone_pcm=microphone_pcm,
                echo=echo,
            )
        )

        time.sleep(args.reopen_delay_seconds)
        reopened, reopen_status, reopen_elapsed = run_input(
            input_index, args.sample_rate, args.reopen_seconds, args.blocksize
        )
        write_wav(output_dir / "reopen_mic.wav", reopened, args.sample_rate)
        reopen_pcm = pcm_metrics(reopened)
        first_100ms = reopened[: round(args.sample_rate * 0.1)]
        tests.append(
            test_result(
                "microphone_reopen",
                status_passed(reopen_status)
                and reopen_pcm["clip_samples"] == 0
                and reopen_pcm["rms"] > 1e-6,
                reopen_status,
                elapsed_seconds=reopen_elapsed,
                pcm=reopen_pcm,
                first_100ms_pcm=pcm_metrics(first_100ms),
            )
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "label": args.label,
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "sounddevice": sd.__version__,
        },
        "config": {
            "sample_rate": args.sample_rate,
            "blocksize": args.blocksize,
            "signal_amplitude": args.signal_amplitude,
            "idle_seconds": args.idle_seconds,
            "playback_seconds": args.playback_seconds,
            "duplex_seconds": args.duplex_seconds,
            "reopen_delay_seconds": args.reopen_delay_seconds,
            "reopen_seconds": args.reopen_seconds,
        },
        "devices": {
            "input": device_dict(input_index, input_info),
            "output": device_dict(output_index, output_info),
        },
        "serial_capture": {
            "requested_port": args.serial_port,
            "baudrate": args.serial_baudrate,
            "log": serial_log.name if args.serial_port else None,
            "error": serial_capture.error,
        },
        "tests": tests,
        "summary": {
            "passed": all(test["passed"] for test in tests) and serial_capture.error is None,
            "passed_count": sum(bool(test["passed"]) for test in tests),
            "total_count": len(tests),
        },
    }
    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(report_path), **report["summary"]}, ensure_ascii=False))
    return 0 if report["summary"]["passed"] else 1


def load_report(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def full_duplex_echo(report: dict[str, Any]) -> dict[str, Any]:
    for test in report["tests"]:
        if test["name"] == "full_duplex":
            return test["echo"]
    raise ValueError("report has no full_duplex result")


def compare_reports(args: argparse.Namespace) -> int:
    aec_on = load_report(args.aec_on)
    aec_off = load_report(args.aec_off)
    on_echo = full_duplex_echo(aec_on)
    off_echo = full_duplex_echo(aec_off)
    improvement = off_echo["projected_residual_db"] - on_echo["projected_residual_db"]
    off_correlation = abs(float(off_echo["normalized_correlation"]))
    observable_echo = off_correlation >= args.min_aec_off_correlation
    same_config = aec_on["config"] == aec_off["config"]
    same_devices = (
        aec_on["devices"]["input"]["name"] == aec_off["devices"]["input"]["name"]
        and aec_on["devices"]["output"]["name"] == aec_off["devices"]["output"]["name"]
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "aec_on_report": str(Path(args.aec_on)),
        "aec_off_report": str(Path(args.aec_off)),
        "same_config": same_config,
        "same_devices": same_devices,
        "aec_on_projected_residual_db": on_echo["projected_residual_db"],
        "aec_off_projected_residual_db": off_echo["projected_residual_db"],
        "improvement_db": improvement,
        "minimum_improvement_db": args.min_improvement_db,
        "aec_off_absolute_correlation": off_correlation,
        "minimum_aec_off_correlation": args.min_aec_off_correlation,
        "observable_echo": observable_echo,
        "passed": same_config
        and same_devices
        and observable_echo
        and improvement >= args.min_improvement_db,
    }
    if args.output:
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def run_coupling_calibration(args: argparse.Namespace) -> int:
    if platform.system() != "Darwin":
        raise RuntimeError("this test runner currently supports macOS only")
    if not args.confirm_aec_off:
        raise RuntimeError("coupling calibration requires --confirm-aec-off")

    output_dir = Path(
        args.output_dir or f"test-results/{datetime.now():%Y%m%d-%H%M%S}-coupling"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    input_index, input_info = find_device(args.input_name, "input")
    output_index, output_info = find_device(args.output_name, "output")

    steps: list[dict[str, Any]] = []
    for amplitude in args.amplitudes:
        signal = make_test_signal(args.sample_rate, args.duration, amplitude)
        microphone, status, elapsed = run_duplex(
            input_index, output_index, args.sample_rate, signal, args.blocksize
        )
        suffix = f"{amplitude:.3f}".replace(".", "p")
        write_wav(output_dir / f"reference_{suffix}.wav", signal, args.sample_rate)
        write_wav(output_dir / f"microphone_{suffix}.wav", microphone, args.sample_rate)
        echo = echo_metrics(
            signal,
            microphone,
            args.sample_rate,
            skip_seconds=min(1.0, args.duration / 4.0),
        )
        observable = abs(echo["normalized_correlation"]) >= args.min_correlation
        steps.append(
            {
                "amplitude": amplitude,
                "elapsed_seconds": elapsed,
                "passed_stream": status_passed(status),
                "stream_status": asdict(status),
                "reference_pcm": pcm_metrics(signal),
                "microphone_pcm": pcm_metrics(microphone),
                "echo": echo,
                "observable_echo": observable,
            }
        )
        time.sleep(args.between_steps_seconds)

    observable_steps = sum(bool(step["observable_echo"]) for step in steps)
    report = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "kind": "aec-off-coupling-calibration",
        "host": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "sounddevice": sd.__version__,
        },
        "config": {
            "sample_rate": args.sample_rate,
            "blocksize": args.blocksize,
            "duration": args.duration,
            "amplitudes": args.amplitudes,
            "between_steps_seconds": args.between_steps_seconds,
            "minimum_correlation": args.min_correlation,
        },
        "devices": {
            "input": device_dict(input_index, input_info),
            "output": device_dict(output_index, output_info),
        },
        "steps": steps,
        "summary": {
            "passed": all(step["passed_stream"] for step in steps) and observable_steps > 0,
            "observable_steps": observable_steps,
            "total_steps": len(steps),
            "maximum_absolute_correlation": max(
                abs(step["echo"]["normalized_correlation"]) for step in steps
            ),
        },
    }
    report_path = output_dir / "coupling-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(report_path), **report["summary"]}, ensure_ascii=False))
    return 0 if report["summary"]["passed"] else 1


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def audio_amplitude(value: str) -> float:
    parsed = float(value)
    if not 0.0 < parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be > 0 and <= 1")
    return parsed


def amplitude_list(value: str) -> list[float]:
    try:
        values = [audio_amplitude(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not values:
        raise argparse.ArgumentTypeError("must contain at least one amplitude")
    if values != sorted(set(values)):
        raise argparse.ArgumentTypeError("amplitudes must be unique and ascending")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run one macOS audio baseline")
    run.add_argument("--label", default="aec-on")
    run.add_argument("--output-dir")
    run.add_argument("--input-name", default="wzk-mic")
    run.add_argument("--output-name", default="wzk-speaker")
    run.add_argument("--sample-rate", type=int, default=16000)
    run.add_argument("--blocksize", type=int, default=512)
    run.add_argument("--signal-amplitude", type=audio_amplitude, default=0.05)
    run.add_argument("--idle-seconds", type=positive_float, default=3.0)
    run.add_argument("--playback-seconds", type=positive_float, default=3.0)
    run.add_argument("--duplex-seconds", type=positive_float, default=10.0)
    run.add_argument("--reopen-delay-seconds", type=positive_float, default=1.0)
    run.add_argument("--reopen-seconds", type=positive_float, default=1.0)
    run.add_argument("--serial-port")
    run.add_argument("--serial-baudrate", type=int, default=115200)
    run.set_defaults(func=run_test)

    compare = subparsers.add_parser("compare", help="compare AEC-on and AEC-off reports")
    compare.add_argument("--aec-on", required=True)
    compare.add_argument("--aec-off", required=True)
    compare.add_argument("--min-improvement-db", type=float, default=6.0)
    compare.add_argument("--min-aec-off-correlation", type=float, default=0.02)
    compare.add_argument("--output")
    compare.set_defaults(func=compare_reports)

    coupling = subparsers.add_parser(
        "coupling", help="measure acoustic coupling with an AEC-off firmware"
    )
    coupling.add_argument("--confirm-aec-off", action="store_true")
    coupling.add_argument("--output-dir")
    coupling.add_argument("--input-name", default="wzk-mic")
    coupling.add_argument("--output-name", default="wzk-speaker")
    coupling.add_argument("--sample-rate", type=int, default=16000)
    coupling.add_argument("--blocksize", type=int, default=512)
    coupling.add_argument("--duration", type=positive_float, default=5.0)
    coupling.add_argument(
        "--amplitudes", type=amplitude_list, default=[0.01, 0.02, 0.04, 0.08]
    )
    coupling.add_argument("--between-steps-seconds", type=positive_float, default=0.5)
    coupling.add_argument("--min-correlation", type=float, default=0.02)
    coupling.set_defaults(func=run_coupling_calibration)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
