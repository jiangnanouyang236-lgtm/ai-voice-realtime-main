import io
import os
import wave
import unittest
from types import SimpleNamespace

os.environ["CONFIG_DATABASE_URL"] = ""

import gateway.opus_audio as opus_audio  # noqa: E402
from gateway.audio_protocol import (  # noqa: E402
    AudioValidationError,
    decode_audio_frame,
    decode_client_audio_frame,
    encode_audio_frame,
    header_int,
)
from gateway.config import (  # noqa: E402
    GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
    GATEWAY_MAX_AUDIO_RAW_BYTES,
)
from gateway.opus_audio import (  # noqa: E402
    AudioStreamAssembler,
    OpusPCMStreamEncoder,
    audio_stream_utterance_id,
    build_audio_end_log_fields,
    build_opus_packet_stream,
    encode_pcm16_to_opus_packet_stream,
    encode_server_tts_pcm_frame,
    decode_opus_audio_to_wav,
    parse_opus_packet_stream,
)
from gateway.wav_audio import (  # noqa: E402
    truncate_wav_audio_to_limit,
    validate_wav_audio,
    wav_to_pcm,
)
from gateway.gateway_server import _decode_audio_request_payload  # noqa: E402


def _wav_bytes(
    *,
    duration_ms: int = 1000,
    sample_rate: int = GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    frames = max(1, int(sample_rate * duration_ms / 1000))
    frame_bytes = b"\x00" * sample_width * channels
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(frame_bytes * frames)
    return buffer.getvalue()


class GatewayAudioLimitsTest(unittest.TestCase):
    def test_header_int_parses_values_or_returns_default(self):
        self.assertEqual(20, header_int("20", 10))
        self.assertEqual(20, header_int(20.9, 10))
        self.assertEqual(10, header_int(None, 10))
        self.assertEqual(10, header_int("bad", 10))

    def test_validate_accepts_current_rust_client_wav_contract(self):
        metadata = validate_wav_audio(_wav_bytes(duration_ms=1000))

        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, metadata["sample_rate"])
        self.assertEqual(1, metadata["channels"])
        self.assertEqual(2, metadata["sample_width"])
        self.assertAlmostEqual(1000.0, metadata["duration_ms"], delta=1.0)

    def test_validate_rejects_invalid_wav_data(self):
        with self.assertRaises(AudioValidationError) as ctx:
            validate_wav_audio(b"not a wav")

        self.assertEqual("INVALID_AUDIO_DATA", ctx.exception.code)

    def test_validate_rejects_unsupported_sample_rate(self):
        with self.assertRaises(AudioValidationError) as ctx:
            validate_wav_audio(_wav_bytes(sample_rate=8000))

        self.assertEqual("UNSUPPORTED_AUDIO_FORMAT", ctx.exception.code)

    def test_validate_rejects_unsupported_channels(self):
        with self.assertRaises(AudioValidationError) as ctx:
            validate_wav_audio(_wav_bytes(channels=2))

        self.assertEqual("UNSUPPORTED_AUDIO_FORMAT", ctx.exception.code)

    def test_validate_rejects_unsupported_sample_width(self):
        with self.assertRaises(AudioValidationError) as ctx:
            validate_wav_audio(_wav_bytes(sample_width=1))

        self.assertEqual("UNSUPPORTED_AUDIO_FORMAT", ctx.exception.code)

    def test_validate_marks_audio_over_ten_seconds(self):
        metadata = validate_wav_audio(_wav_bytes(duration_ms=11000))

        self.assertTrue(metadata["duration_limit_exceeded"])
        self.assertEqual(11000.0, metadata["duration_ms"])

    def test_truncate_audio_over_ten_seconds(self):
        wav_data, metadata = truncate_wav_audio_to_limit(_wav_bytes(duration_ms=11000))
        validated = validate_wav_audio(wav_data)

        self.assertTrue(metadata["truncated"])
        self.assertEqual(11000.0, metadata["original_duration_ms"])
        self.assertAlmostEqual(10000.0, metadata["duration_ms"], delta=1.0)
        self.assertAlmostEqual(10000.0, validated["duration_ms"], delta=1.0)

    def test_wav_to_pcm_returns_pcm_bytes_and_sample_rate(self):
        pcm_data, sample_rate = wav_to_pcm(_wav_bytes(duration_ms=500))

        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, sample_rate)
        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, len(pcm_data))

    def test_wav_to_pcm_truncates_audio_over_ten_seconds(self):
        pcm_data, sample_rate = wav_to_pcm(_wav_bytes(duration_ms=11000))

        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, sample_rate)
        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE * 10 * 2, len(pcm_data))

    def test_binary_audio_frame_round_trips_client_opus(self):
        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"
        frame = encode_audio_frame(
            payload,
            direction="client_input",
            encoding="opus",
            bot_id="xiaowen",
            trace_id="trace_1",
            round_id="round_1",
            playback_id="round_1:playback",
            sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
            channels=1,
            duration_ms=250.0,
            opus_frame_ms=20,
            packet_count=1,
        )

        header, decoded_payload = decode_audio_frame(frame)
        message = decode_client_audio_frame(frame)

        self.assertEqual("audio_frame", header["type"])
        self.assertEqual("opus", header["encoding"])
        self.assertEqual(payload, decoded_payload)
        self.assertEqual("audio", message["type"])
        self.assertEqual("binary_frame", message["audio_transport"])
        self.assertEqual("opus", message["audio_encoding"])
        self.assertEqual(payload, message["audio_bytes"])
        self.assertEqual("xiaowen", message["bot_id"])
        self.assertEqual("trace_1", message["trace_id"])
        self.assertEqual("round_1", message["round_id"])
        self.assertEqual("round_1:playback", message["playback_id"])
        self.assertEqual(20, message["opus_frame_ms"])
        self.assertEqual(1, message["packet_count"])

    def test_binary_turn_candidate_frame_preserves_shadow_metadata(self):
        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"
        frame = encode_audio_frame(
            payload,
            event_type="turn_candidate",
            context_session_id="python-main-session",
            direction="client_input",
            encoding="opus",
            trace_id="trace_1",
            utterance_id="utt_1",
            candidate_seq=2,
            speech_epoch=1,
            audio_watermark=16000,
            silence_ms=300,
            shadow=True,
            sample_rate=16000,
            channels=1,
            opus_frame_ms=20,
            packet_count=1,
        )

        message = decode_client_audio_frame(frame)

        self.assertEqual("turn_candidate", message["type"])
        self.assertEqual(2, message["candidate_seq"])
        self.assertEqual("python-main-session", message["context_session_id"])
        self.assertEqual(1, message["speech_epoch"])
        self.assertEqual(16000, message["audio_watermark"])
        self.assertEqual(300, message["silence_ms"])
        self.assertTrue(message["shadow"])
        self.assertEqual(payload, message["audio_bytes"])

    def test_binary_audio_frame_round_trips_server_tts_trace_metadata(self):
        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"tts"
        frame = encode_audio_frame(
            payload,
            direction="server_tts",
            encoding="opus",
            trace_id="trace_2",
            round_id="round_2",
            playback_id="round_2:playback",
            chunk_seq=7,
            sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
            channels=1,
            duration_ms=20.0,
        )

        header, decoded_payload = decode_audio_frame(frame)

        self.assertEqual(payload, decoded_payload)
        self.assertEqual("server_tts", header["direction"])
        self.assertEqual("opus", header["encoding"])
        self.assertEqual("trace_2", header["trace_id"])
        self.assertEqual("round_2", header["round_id"])
        self.assertEqual("round_2:playback", header["playback_id"])
        self.assertEqual(7, header["chunk_seq"])

    def test_binary_audio_frame_rejects_unsupported_client_encoding(self):
        frame = encode_audio_frame(
            _wav_bytes(duration_ms=250),
            direction="client_input",
            encoding="wav",
            sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
            channels=1,
            duration_ms=250.0,
        )

        with self.assertRaises(AudioValidationError) as ctx:
            decode_client_audio_frame(frame)

        self.assertEqual("UNSUPPORTED_AUDIO_FORMAT", ctx.exception.code)

    def test_parse_opus_packet_stream_extracts_length_prefixed_packets(self):
        payload = (
            b"OPUSRAW1"
            + (3).to_bytes(2, "big")
            + b"abc"
            + (4).to_bytes(2, "big")
            + b"defg"
        )

        packets = parse_opus_packet_stream(payload)

        self.assertEqual([b"abc", b"defg"], packets)

    def test_build_opus_packet_stream_round_trips_packets(self):
        payload = build_opus_packet_stream([b"abc", b"defg"])

        self.assertEqual(
            b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc" + (4).to_bytes(2, "big") + b"defg",
            payload,
        )
        self.assertEqual([b"abc", b"defg"], parse_opus_packet_stream(payload))

    def test_parse_opus_packet_stream_rejects_truncated_packet(self):
        payload = b"OPUSRAW1" + (10).to_bytes(2, "big") + b"abc"

        with self.assertRaises(AudioValidationError) as ctx:
            parse_opus_packet_stream(payload)

        self.assertEqual("INVALID_AUDIO_DATA", ctx.exception.code)

    def test_decode_opus_audio_to_wav_trims_padding_and_returns_metadata(self):
        calls = []

        class FakeDecoder:
            def __init__(self, sample_rate: int, channels: int):
                self.sample_rate = sample_rate
                self.channels = channels

            def decode(self, packet: bytes, frame_size: int, decode_fec: bool = False) -> bytes:
                calls.append((packet, frame_size, decode_fec))
                return b"\x01\x00" * frame_size * self.channels

        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Decoder=FakeDecoder)
        try:
            payload = (
                b"OPUSRAW1"
                + (3).to_bytes(2, "big")
                + b"abc"
                + (4).to_bytes(2, "big")
                + b"defg"
            )
            wav_data, metadata = decode_opus_audio_to_wav(
                payload,
                sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                channels=1,
                duration_ms=30,
                opus_frame_ms=20,
            )
        finally:
            opus_audio.opuslib = original_opuslib

        pcm_data, sample_rate = wav_to_pcm(wav_data)

        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, sample_rate)
        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE * 30 // 1000 * 2, len(pcm_data))
        self.assertEqual([(b"abc", 320, False), (b"defg", 320, False)], calls)
        self.assertEqual(len(payload), metadata["source_audio_bytes"])
        self.assertEqual(2, metadata["opus_packets"])
        self.assertEqual(20, metadata["opus_frame_ms"])

    def test_decode_client_audio_frame_maps_stream_chunk(self):
        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"
        frame = encode_audio_frame(
            payload,
            direction="client_input",
            encoding="opus",
            bot_id="xiaowen",
            utterance_id="u1",
            chunk_seq=2,
            stream_event="chunk",
            sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
            channels=1,
            duration_ms=100.0,
            opus_frame_ms=20,
            packet_count=1,
        )

        message = decode_client_audio_frame(frame)

        self.assertEqual("audio_chunk", message["type"])
        self.assertEqual("binary_stream_chunk", message["audio_transport"])
        self.assertEqual("u1", message["utterance_id"])
        self.assertEqual(2, message["chunk_seq"])

    def test_audio_stream_assembler_rebuilds_batch_audio_payload(self):
        assembler = AudioStreamAssembler(
            {
                "utterance_id": "u1",
                "bot_id": "xiaowen",
                "sample_rate": GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                "channels": 1,
                "opus_frame_ms": 20,
            }
        )
        first = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"
        second = b"OPUSRAW1" + (4).to_bytes(2, "big") + b"defg"

        assembler.append(
            {
                "utterance_id": "u1",
                "audio_bytes": first,
                "chunk_seq": 1,
                "duration_ms": 20.0,
            }
        )
        assembler.append(
            {
                "utterance_id": "u1",
                "audio_bytes": second,
                "chunk_seq": 2,
                "duration_ms": 20.0,
            }
        )
        message = assembler.finish(
            {
                "utterance_id": "u1",
                "bot_id": "xiaowen",
                "duration_ms": 40.0,
            }
        )

        self.assertEqual("audio", message["type"])
        self.assertEqual("binary_stream", message["audio_transport"])
        self.assertEqual("u1", message["utterance_id"])
        self.assertEqual(2, message["stream_chunk_count"])
        self.assertEqual([b"abc", b"defg"], parse_opus_packet_stream(message["audio_bytes"]))

    def test_audio_stream_helpers_normalize_id_and_log_fields(self):
        self.assertEqual("u1", audio_stream_utterance_id({"utterance_id": " u1 "}))
        self.assertEqual("", audio_stream_utterance_id({}))

        fields = build_audio_end_log_fields(
            {
                "stream_chunk_count": 2,
                "packet_count": 4,
                "stream_source_audio_bytes": 128,
                "stream_elapsed_ms": 37.5,
            },
            utterance_id="u1",
            queue_size=1,
            dropped=0,
        )

        self.assertEqual(
            {
                "utterance_id": "u1",
                "chunks": 2,
                "packets": 4,
                "opus_bytes": 128,
                "queue_size": 1,
                "dropped": 0,
                "elapsed_ms": 37.5,
            },
            fields,
        )

    def test_decode_audio_request_payload_decodes_opus_and_preserves_stream_metadata(self):
        class FakeDecoder:
            def __init__(self, sample_rate: int, channels: int):
                self.sample_rate = sample_rate
                self.channels = channels

            def decode(self, packet: bytes, frame_size: int, decode_fec: bool = False) -> bytes:
                return b"\x01\x00" * frame_size * self.channels

        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"
        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Decoder=FakeDecoder)
        try:
            decoded = _decode_audio_request_payload(
                {
                    "audio_bytes": payload,
                    "audio_transport": "binary_stream",
                    "audio_encoding": "opus",
                    "sample_rate": GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                    "channels": 1,
                    "duration_ms": 20.0,
                    "opus_frame_ms": 20,
                    "utterance_id": "u1",
                    "stream_chunk_count": 2,
                    "stream_source_audio_bytes": 128,
                    "stream_elapsed_ms": 37.5,
                }
            )
        finally:
            opus_audio.opuslib = original_opuslib

        pcm_data, sample_rate = wav_to_pcm(decoded.audio_data)
        metadata = decoded.audio_metadata

        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE, sample_rate)
        self.assertEqual(GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE * 20 // 1000 * 2, len(pcm_data))
        self.assertEqual("binary_stream", decoded.audio_transport)
        self.assertEqual("opus", decoded.audio_encoding)
        self.assertEqual("u1", metadata["utterance_id"])
        self.assertEqual(2, metadata["stream_chunk_count"])
        self.assertEqual(128, metadata["stream_source_audio_bytes"])
        self.assertEqual(37.5, metadata["stream_elapsed_ms"])
        self.assertEqual(1, metadata["opus_packets"])

    def test_decode_audio_request_payload_rejects_invalid_payloads(self):
        with self.assertRaises(AudioValidationError) as ctx:
            _decode_audio_request_payload({"audio_bytes": "not-bytes"})
        self.assertEqual("INVALID_AUDIO_TRANSPORT", ctx.exception.code)

        with self.assertRaises(AudioValidationError) as ctx:
            _decode_audio_request_payload(
                {
                    "audio_bytes": b"x" * (GATEWAY_MAX_AUDIO_RAW_BYTES + 1),
                    "audio_encoding": "opus",
                }
            )
        self.assertEqual("AUDIO_TOO_LARGE", ctx.exception.code)

        with self.assertRaises(AudioValidationError) as ctx:
            _decode_audio_request_payload(
                {
                    "audio_bytes": b"OPUSRAW1",
                    "audio_encoding": "wav",
                }
            )
        self.assertEqual("UNSUPPORTED_AUDIO_FORMAT", ctx.exception.code)

    def test_audio_stream_assembler_rejects_out_of_order_chunks(self):
        assembler = AudioStreamAssembler(
            {
                "utterance_id": "u1",
                "sample_rate": GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                "channels": 1,
                "opus_frame_ms": 20,
            }
        )
        payload = b"OPUSRAW1" + (3).to_bytes(2, "big") + b"abc"

        with self.assertRaises(AudioValidationError) as ctx:
            assembler.append(
                {
                    "utterance_id": "u1",
                    "audio_bytes": payload,
                    "chunk_seq": 2,
                }
            )

        self.assertEqual("INVALID_AUDIO_STREAM", ctx.exception.code)

    def test_encode_pcm16_to_opus_packet_stream_returns_packet_payload(self):
        class FakeEncoder:
            def __init__(self, sample_rate: int, channels: int, application: int):
                self.sample_rate = sample_rate
                self.channels = channels
                self.application = application

            def encode(self, frame: bytes, frame_size: int) -> bytes:
                calls.append((len(frame), frame_size))
                return b"OP"

        calls = []
        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Encoder=FakeEncoder, APPLICATION_AUDIO=2049)
        try:
            payload, metadata = encode_pcm16_to_opus_packet_stream(
                b"\x01\x00" * 160,
                sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                channels=1,
                opus_frame_ms=20,
            )
        finally:
            opus_audio.opuslib = original_opuslib

        self.assertEqual(b"OPUSRAW1\x00\x02OP", payload)
        self.assertEqual([(640, 320)], calls)
        self.assertEqual(1, metadata["packet_count"])
        self.assertEqual(20, metadata["opus_frame_ms"])
        self.assertEqual(320, metadata["pcm_bytes"])

    def test_encode_server_tts_pcm_frame_wraps_opus_payload_with_trace_header(self):
        class FakeEncoder:
            def __init__(self, sample_rate: int, channels: int, application: int):
                self.sample_rate = sample_rate
                self.channels = channels
                self.application = application

            def encode(self, frame: bytes, frame_size: int) -> bytes:
                return b"OP"

        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Encoder=FakeEncoder, APPLICATION_AUDIO=2049)
        try:
            frame, metadata = encode_server_tts_pcm_frame(
                b"\x00\x00" * 320,
                sample_rate=16000,
                channels=1,
                header={
                    "trace_id": "trace-1",
                    "round_id": "round-1",
                    "playback_id": "round-1:playback",
                    "chunk_seq": 3,
                },
            )
        finally:
            opus_audio.opuslib = original_opuslib

        header, payload = decode_audio_frame(frame)

        self.assertEqual("server_tts", header["direction"])
        self.assertEqual("opus", header["encoding"])
        self.assertEqual("trace-1", header["trace_id"])
        self.assertEqual("round-1", header["round_id"])
        self.assertEqual("round-1:playback", header["playback_id"])
        self.assertEqual(3, header["chunk_seq"])
        self.assertEqual(20, header["opus_frame_ms"])
        self.assertEqual(1, header["packet_count"])
        self.assertEqual(640, header["pcm_bytes"])
        self.assertEqual(metadata, {"opus_frame_ms": 20, "packet_count": 1, "pcm_bytes": 640})
        self.assertEqual(b"OPUSRAW1\x00\x02OP", payload)

    def test_stream_encoder_reuses_one_opus_encoder_across_pcm_chunks(self):
        class FakeEncoder:
            instances = 0

            def __init__(self, sample_rate: int, channels: int, application: int):
                FakeEncoder.instances += 1
                self.calls = 0

            def encode(self, frame: bytes, frame_size: int) -> bytes:
                self.calls += 1
                return f"P{self.calls}".encode()

        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Encoder=FakeEncoder, APPLICATION_AUDIO=2049)
        try:
            encoder = OpusPCMStreamEncoder(sample_rate=16000, channels=1)
            first_payload, first_metadata = encoder.encode(b"\x00\x00" * 1600)
            second_payload, second_metadata = encoder.encode(
                b"\x00\x00" * 1600,
                final=True,
            )
        finally:
            opus_audio.opuslib = original_opuslib

        self.assertEqual(1, FakeEncoder.instances)
        self.assertEqual(5, first_metadata["packet_count"])
        self.assertEqual(5, second_metadata["packet_count"])
        self.assertEqual(
            [b"P1", b"P2", b"P3", b"P4", b"P5"],
            parse_opus_packet_stream(first_payload),
        )
        self.assertEqual(
            [b"P6", b"P7", b"P8", b"P9", b"P10"],
            parse_opus_packet_stream(second_payload),
        )
        with self.assertRaises(AudioValidationError):
            encoder.encode(b"\x00\x00" * 320)

    def test_stream_encoder_pads_only_the_final_partial_opus_frame(self):
        encoded_lengths = []

        class FakeEncoder:
            def __init__(self, sample_rate: int, channels: int, application: int):
                pass

            def encode(self, frame: bytes, frame_size: int) -> bytes:
                encoded_lengths.append((len(frame), frame_size))
                return b"OP"

        original_opuslib = opus_audio.opuslib
        opus_audio.opuslib = SimpleNamespace(Encoder=FakeEncoder, APPLICATION_AUDIO=2049)
        try:
            encoder = OpusPCMStreamEncoder(sample_rate=16000, channels=1)
            payload, metadata = encoder.encode(b"\x01\x00" * 160, final=True)
        finally:
            opus_audio.opuslib = original_opuslib

        self.assertEqual([(640, 320)], encoded_lengths)
        self.assertEqual(1, metadata["packet_count"])
        self.assertEqual(320, metadata["pcm_bytes"])
        self.assertEqual([b"OP"], parse_opus_packet_stream(payload))

    def test_encode_pcm16_to_opus_packet_stream_ignores_bitrate_setter_failure(self):
        class FakeEncoder:
            bitrate_attempts = 0

            def __init__(self, sample_rate: int, channels: int, application: int):
                self.sample_rate = sample_rate
                self.channels = channels
                self.application = application

            @property
            def bitrate(self):
                return 0

            @bitrate.setter
            def bitrate(self, _value):
                FakeEncoder.bitrate_attempts += 1
                raise RuntimeError("unsupported bitrate ctl")

            def encode(self, frame: bytes, frame_size: int) -> bytes:
                calls.append((len(frame), frame_size))
                return b"OP"

        calls = []
        original_opuslib = opus_audio.opuslib
        original_bitrate_unsupported = opus_audio._OPUS_BITRATE_SET_UNSUPPORTED
        opus_audio.opuslib = SimpleNamespace(Encoder=FakeEncoder, APPLICATION_AUDIO=2049)
        opus_audio._OPUS_BITRATE_SET_UNSUPPORTED = False
        try:
            with self.assertLogs("gateway.opus_audio", level="WARNING") as logs:
                payload, metadata = encode_pcm16_to_opus_packet_stream(
                    b"\x01\x00" * 160,
                    sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                    channels=1,
                    opus_frame_ms=20,
                )
                payload2, metadata2 = encode_pcm16_to_opus_packet_stream(
                    b"\x01\x00" * 160,
                    sample_rate=GATEWAY_ALLOWED_AUDIO_SAMPLE_RATE,
                    channels=1,
                    opus_frame_ms=20,
                )
        finally:
            opus_audio.opuslib = original_opuslib
            opus_audio._OPUS_BITRATE_SET_UNSUPPORTED = original_bitrate_unsupported

        self.assertEqual(b"OPUSRAW1\x00\x02OP", payload)
        self.assertEqual(b"OPUSRAW1\x00\x02OP", payload2)
        self.assertEqual([(640, 320), (640, 320)], calls)
        self.assertEqual(1, FakeEncoder.bitrate_attempts)
        self.assertEqual(1, sum("设置 Opus bitrate" in line for line in logs.output))
        self.assertEqual(1, metadata["packet_count"])
        self.assertEqual(1, metadata2["packet_count"])


if __name__ == "__main__":
    unittest.main()
