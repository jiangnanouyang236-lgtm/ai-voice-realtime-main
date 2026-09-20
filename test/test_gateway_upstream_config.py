import unittest

from gateway.upstream_config import (
    build_initial_upstream_statuses,
    build_upstream_specs,
    parse_grpc_upstream_url,
)


class GatewayUpstreamConfigTest(unittest.TestCase):
    def test_parse_grpc_upstream_url_defaults_to_grpc(self):
        spec = parse_grpc_upstream_url("127.0.0.1:50054/")

        self.assertEqual(
            {"url": "grpc://127.0.0.1:50054", "target": "127.0.0.1:50054", "secure": False},
            spec,
        )

    def test_parse_grpc_upstream_url_accepts_grpcs(self):
        spec = parse_grpc_upstream_url("grpcs://stt.example:50054")

        self.assertEqual("grpcs://stt.example:50054", spec["url"])
        self.assertEqual("stt.example:50054", spec["target"])
        self.assertTrue(spec["secure"])

    def test_parse_grpc_upstream_url_rejects_invalid_scheme(self):
        with self.assertRaises(ValueError):
            parse_grpc_upstream_url("http://stt.example:50054")

    def test_build_upstream_specs_maps_required_services(self):
        specs = build_upstream_specs(
            {
                "stt_service_url": "grpc://127.0.0.1:50054",
                "llm_service_url": "127.0.0.1:50053",
                "tts_service_url": "grpcs://tts.example:50052",
            }
        )

        self.assertEqual(["llm", "stt", "tts"], sorted(specs.keys()))
        self.assertFalse(specs["stt"]["secure"])
        self.assertFalse(specs["llm"]["secure"])
        self.assertTrue(specs["tts"]["secure"])

    def test_build_initial_upstream_statuses_preserves_runtime_shape(self):
        specs = build_upstream_specs(
            {
                "stt_service_url": "grpc://127.0.0.1:50054",
                "llm_service_url": "grpc://127.0.0.1:50053",
                "tts_service_url": "grpc://127.0.0.1:50052",
            }
        )
        statuses = build_initial_upstream_statuses(specs)

        self.assertEqual(["llm", "stt", "tts"], sorted(statuses.keys()))
        self.assertEqual("stt", statuses["stt"]["service"])
        self.assertEqual("grpc://127.0.0.1:50054", statuses["stt"]["target"])
        self.assertFalse(statuses["stt"]["secure"])
        self.assertEqual("configured", statuses["stt"]["status"])
        self.assertEqual("等待预热", statuses["stt"]["message"])
        self.assertIsNone(statuses["stt"]["last_ready_at"])
        self.assertIsNone(statuses["stt"]["last_error"])
        self.assertIsNone(statuses["stt"]["last_error_at"])


if __name__ == "__main__":
    unittest.main()
