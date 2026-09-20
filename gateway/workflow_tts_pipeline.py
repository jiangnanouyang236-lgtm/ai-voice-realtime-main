"""复杂工作流文本到既有 TTS RPC 的独立增量桥接。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import queue
import threading
from typing import Any, Awaitable, Callable

from gateway.tts_chunks import build_tts_text_chunk
from gateway.workflow_coordinator import (
    WorkflowCoordinator,
    WorkflowRunRequest,
    WorkflowRunResult,
)


_TEXT_END = object()
_AUDIO_END = object()


@dataclass(frozen=True)
class WorkflowTTSPipelineResult:
    workflow: WorkflowRunResult
    audio_chunks: int
    audio_bytes: int
    done_sent: bool
    exit_sent: bool
    terminal_expected: bool


ControlSender = Callable[[str, dict[str, Any]], Awaitable[None]]
AudioSender = Callable[[Any, int], Awaitable[None]]


class WorkflowTTSPipeline:
    def __init__(
        self,
        *,
        coordinator: WorkflowCoordinator,
        tts_stub_provider: Callable[[], Any],
        tts_timeout_sec: float,
        queue_maxsize: int = 50,
    ) -> None:
        if tts_timeout_sec < 0 or queue_maxsize < 1:
            raise ValueError("TTS timeout 不能小于 0，queue_maxsize 必须大于 0")
        self._coordinator = coordinator
        self._tts_stub_provider = tts_stub_provider
        self._tts_timeout_sec = tts_timeout_sec
        self._queue_maxsize = queue_maxsize

    async def run(
        self,
        request: WorkflowRunRequest,
        *,
        bot_tts_settings: dict[str, Any] | None,
        send_control: ControlSender,
        send_audio: AudioSender,
        is_cancelled: Callable[[], bool],
        initial_text: str = "",
    ) -> WorkflowTTSPipelineResult:
        loop = asyncio.get_running_loop()
        text_queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_maxsize)
        audio_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._queue_maxsize)
        stop = threading.Event()
        synthesis_closed = False
        synthesis_close_lock = asyncio.Lock()
        terminal_expected = False
        tts_call_holder: dict[str, Any] = {"call": None}
        if str(initial_text or "").strip():
            text_queue.put_nowait(str(initial_text).strip())

        async def put_text(item: Any) -> None:
            await asyncio.to_thread(text_queue.put, item)

        async def emit_text(text: str) -> None:
            if stop.is_set() or is_cancelled():
                raise asyncio.CancelledError()
            await put_text(text)

        async def observe_plan(plan_id: str, has_terminal: bool) -> None:
            nonlocal terminal_expected
            terminal_expected = has_terminal

        async def finish_synthesis() -> None:
            nonlocal synthesis_closed
            async with synthesis_close_lock:
                if synthesis_closed:
                    return
                await put_text(_TEXT_END)
                synthesis_closed = True

        def close_text_queue_for_shutdown() -> None:
            nonlocal synthesis_closed
            if synthesis_closed:
                return
            synthesis_closed = True
            try:
                text_queue.put_nowait(_TEXT_END)
            except queue.Full:
                # 队列非空时消费者不会卡在 get；stop 标志会让它在下一轮退出。
                pass

        async def run_workflow() -> WorkflowRunResult:
            try:
                return await self._coordinator.run(
                    request,
                    emit_text=emit_text,
                    observe_plan=observe_plan,
                    synthesis_finished=finish_synthesis,
                )
            finally:
                await finish_synthesis()

        def put_audio(item: Any) -> bool:
            if stop.is_set():
                return False
            future = asyncio.run_coroutine_threadsafe(audio_queue.put(item), loop)
            try:
                future.result(timeout=5.0)
                return True
            except Exception:
                future.cancel()
                return False

        def generate_text_chunks():
            first = True
            while not stop.is_set():
                item = text_queue.get()
                if item is _TEXT_END:
                    yield build_tts_text_chunk(
                        "",
                        is_final=True,
                        session_id=request.session_id,
                        trace_id=request.trace_id,
                        round_id=request.round_id,
                        playback_id=request.playback_id,
                        bot_tts_settings=bot_tts_settings,
                        include_config=first,
                    )
                    return
                text = str(item or "")
                if not text:
                    continue
                yield build_tts_text_chunk(
                    text,
                    is_final=False,
                    session_id=request.session_id,
                    trace_id=request.trace_id,
                    round_id=request.round_id,
                    playback_id=request.playback_id,
                    bot_tts_settings=bot_tts_settings,
                    include_config=first,
                )
                first = False

        def run_tts() -> None:
            try:
                call = self._tts_stub_provider().StreamTextToSpeech(
                    generate_text_chunks(),
                    timeout=self._tts_timeout_sec or None,
                )
                tts_call_holder["call"] = call
                if stop.is_set():
                    cancel = getattr(call, "cancel", None)
                    if callable(cancel):
                        cancel()
                    return
                for chunk in call:
                    if stop.is_set() or not put_audio(chunk):
                        break
            except Exception as exc:
                if not stop.is_set():
                    put_audio(exc)
            finally:
                tts_call_holder["call"] = None
                put_audio(_AUDIO_END)

        workflow_task = asyncio.create_task(run_workflow())
        tts_future = loop.run_in_executor(None, run_tts)
        audio_chunks = 0
        audio_bytes = 0
        done_sent = False
        exit_sent = False
        try:
            await send_control(
                "playback_start",
                {
                    "trace_id": request.trace_id,
                    "round_id": request.round_id,
                    "playback_id": request.playback_id,
                },
            )
            while True:
                if is_cancelled():
                    raise asyncio.CancelledError()
                item = await audio_queue.get()
                if item is _AUDIO_END:
                    break
                if isinstance(item, Exception):
                    raise item
                if not getattr(item, "audio_data", b""):
                    continue
                audio_chunks += 1
                audio_bytes += len(item.audio_data)
                await send_audio(item, audio_chunks)

            if workflow_task.done():
                early_result = workflow_task.result()
                if not early_result.success:
                    if audio_chunks > 0:
                        await send_control(
                            "done",
                            {
                                "exit": False,
                                "trace_id": request.trace_id,
                                "round_id": request.round_id,
                                "playback_id": request.playback_id,
                            },
                        )
                        done_sent = True
                    return WorkflowTTSPipelineResult(
                        workflow=early_result,
                        audio_chunks=audio_chunks,
                        audio_bytes=audio_bytes,
                        done_sent=done_sent,
                        exit_sent=False,
                        terminal_expected=terminal_expected,
                    )

            await send_control(
                "done",
                {
                    "exit": False,
                    "trace_id": request.trace_id,
                    "round_id": request.round_id,
                    "playback_id": request.playback_id,
                },
            )
            done_sent = True
            workflow_result = await workflow_task
            if workflow_result.success and workflow_result.exit:
                await send_control(
                    "done",
                    {
                        "exit": True,
                        "trace_id": request.trace_id,
                        "round_id": request.round_id,
                        "playback_id": request.playback_id,
                    },
                )
                exit_sent = True
            return WorkflowTTSPipelineResult(
                workflow=workflow_result,
                audio_chunks=audio_chunks,
                audio_bytes=audio_bytes,
                done_sent=done_sent,
                exit_sent=exit_sent,
                terminal_expected=terminal_expected,
            )
        except asyncio.CancelledError:
            close_text_queue_for_shutdown()
            stop.set()
            await self._coordinator.cancel_session(
                request.session_id,
                reason="workflow_tts_cancelled",
            )
            workflow_task.cancel()
            raise
        finally:
            close_text_queue_for_shutdown()
            stop.set()
            call = tts_call_holder.get("call")
            cancel = getattr(call, "cancel", None)
            if callable(cancel):
                cancel()
            if not workflow_task.done():
                workflow_task.cancel()
            await asyncio.gather(workflow_task, return_exceptions=True)
            try:
                await asyncio.wait_for(asyncio.shield(tts_future), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
