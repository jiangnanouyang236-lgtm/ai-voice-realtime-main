import unittest

from llm import llm_service_pb2
from llm import workflow_service_pb2
from stt import stt_service_pb2
from tts import tts_service_pb2


def _service(module, full_name):
    return module.DESCRIPTOR.services_by_name[full_name]


def _field_contract(message):
    return {field.name: (field.number, field.type) for field in message.fields}


class ExistingServiceStreamingContractTest(unittest.TestCase):
    def test_llm_stream_chat_contract_is_unchanged(self):
        service = _service(llm_service_pb2, "LLMService")
        self.assertEqual(["StreamChat", "ClearSession"], list(service.methods_by_name))
        stream_chat = service.methods_by_name["StreamChat"]
        self.assertFalse(stream_chat.client_streaming)
        self.assertTrue(stream_chat.server_streaming)
        self.assertEqual("llm.ChatRequest", stream_chat.input_type.full_name)
        self.assertEqual("llm.ChatResponse", stream_chat.output_type.full_name)
        self.assertEqual(
            {
                "text": (1, 9),
                "session_id": (2, 9),
                "bot_id": (3, 9),
                "config": (4, 11),
                "robot_id": (5, 9),
                "trace_id": (6, 9),
            },
            _field_contract(llm_service_pb2.ChatRequest.DESCRIPTOR),
        )
        self.assertEqual(
            {"text": (1, 9), "is_final": (2, 8), "metrics_json": (3, 9)},
            _field_contract(llm_service_pb2.ChatResponse.DESCRIPTOR),
        )

    def test_stt_contract_is_unchanged(self):
        service = _service(stt_service_pb2, "STTService")
        self.assertEqual(["RecognizeSpeech", "StreamRecognize"], list(service.methods_by_name))
        recognize = service.methods_by_name["RecognizeSpeech"]
        self.assertFalse(recognize.client_streaming)
        self.assertFalse(recognize.server_streaming)
        stream = service.methods_by_name["StreamRecognize"]
        self.assertTrue(stream.client_streaming)
        self.assertTrue(stream.server_streaming)

    def test_tts_bidirectional_stream_contract_is_unchanged(self):
        service = _service(tts_service_pb2, "TTSService")
        self.assertEqual(["StreamTextToSpeech"], list(service.methods_by_name))
        stream = service.methods_by_name["StreamTextToSpeech"]
        self.assertTrue(stream.client_streaming)
        self.assertTrue(stream.server_streaming)
        self.assertEqual("tts.TextChunk", stream.input_type.full_name)
        self.assertEqual("tts.AudioChunk", stream.output_type.full_name)
        self.assertEqual(
            {
                "text": (1, 9),
                "is_final": (2, 8),
                "session_id": (3, 9),
                "config": (4, 11),
                "trace_id": (5, 9),
                "round_id": (6, 9),
                "playback_id": (7, 9),
                "gateway_send_epoch_ms": (8, 1),
            },
            _field_contract(tts_service_pb2.TextChunk.DESCRIPTOR),
        )

    def test_workflow_service_is_additive_and_separate(self):
        service = _service(workflow_service_pb2, "WorkflowService")
        self.assertEqual(
            [
                "ClassifyComplex",
                "PlanComplex",
                "ExecuteStep",
                "StreamSynthesis",
                "CancelWorkflow",
            ],
            list(service.methods_by_name),
        )
        self.assertFalse(service.methods_by_name["PlanComplex"].client_streaming)
        self.assertFalse(service.methods_by_name["PlanComplex"].server_streaming)
        self.assertFalse(service.methods_by_name["ExecuteStep"].client_streaming)
        self.assertFalse(service.methods_by_name["ExecuteStep"].server_streaming)
        synthesis = service.methods_by_name["StreamSynthesis"]
        self.assertFalse(synthesis.client_streaming)
        self.assertTrue(synthesis.server_streaming)
        self.assertEqual(
            {
                "plan_id": (1, 9),
                "step_id": (2, 9),
                "session_id": (3, 9),
                "robot_id": (4, 9),
                "trace_id": (5, 9),
                "round_id": (6, 9),
                "playback_completed": (7, 8),
                "playback_id": (8, 9),
            },
            _field_contract(workflow_service_pb2.ExecuteStepRequest.DESCRIPTOR),
        )
        self.assertEqual(
            {
                "plan_id": (1, 9),
                "session_id": (2, 9),
                "bot_id": (3, 9),
                "robot_id": (4, 9),
                "trace_id": (5, 9),
                "round_id": (6, 9),
                "playback_id": (7, 9),
            },
            _field_contract(workflow_service_pb2.SynthesisRequest.DESCRIPTOR),
        )


if __name__ == "__main__":
    unittest.main()
