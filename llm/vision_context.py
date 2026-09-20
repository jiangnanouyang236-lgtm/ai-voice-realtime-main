"""按需读取 Go Voice Gateway 中的最新视觉快照。"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urljoin

import httpx


VISION_IMAGE_MAX_BYTES = 100 * 1024
VISION_PROMPT = (
    "请只根据本轮提供的图片回答用户当前的视觉问题。"
    "只用一到两句简短中文描述画面中最主要、最明显的内容，不要列点。"
    "除非用户明确要求，否则不要尝试辨认图片文字、人物身份或细小物品；"
    "看不清或无法确定时要明确说明，不要猜测图片外的信息，"
    "也不要引用之前轮次的图片。"
)
_VISUAL_INTENT_PHRASES = (
    "这是什么",
    "这个是什么",
    "那是什么",
    "这是谁",
    "这个是谁",
    "那是谁",
    "看看这个",
    "看一下这个",
    "看下这个",
    "帮我看看这个",
    "帮我看一下这个",
    "你看到了什么",
    "你看到什么",
    "你能看到什么",
    "眼前是什么",
    "前面是什么",
    "前边是什么",
    "前面有什么",
    "前边有什么",
    "周围有什么",
    "这里有什么",
    "那里有什么",
    "这是哪里",
    "这是什么地方",
)
_NON_VISUAL_CONTEXT_PHRASES = (
    "前面说",
    "前边说",
    "刚才说",
    "上面说",
    "这件事",
    "这个问题",
    "这个功能",
    "这个方案",
    "这个代码",
    "这个日志",
    "这个文档",
    "怎么看待",
    "你怎么看",
)
_VISUAL_SUBJECT_PHRASES = (
    "穿的",
    "穿着",
    "穿这个",
    "穿搭",
    "衣服",
    "衣着",
    "这身",
    "这个颜色",
    "手里",
    "手上",
    "桌上",
    "桌面",
    "画面",
    "镜头",
    "光线",
    "背景",
    "身后",
    "旁边",
)
_VISUAL_QUESTION_PHRASES = (
    "是什么",
    "有什么",
    "怎么样",
    "如何",
    "好看",
    "合适",
    "适合",
    "什么颜色",
    "看到了",
    "看见",
    "看清",
    "认出",
    "识别",
)
_COMPOUND_NON_VISUAL_PHRASES = (
    "天气",
    "新闻",
    "搜索",
    "查询",
    "查一下",
    "提醒",
    "闹钟",
    "向前",
    "向后",
    "左转",
    "右转",
    "跳舞",
    "巡逻",
    "呼叫视频",
    "视频电话",
    # 客户端发来的环境描述/系统消息（开头是固定的引导语），不是用户问视觉问题。
    # 应当走普通聊天，让 9b 把环境描述当作 user message 继续对话，而不是拉画面。
    "请根据以下环境主动回应",
    "请根据环境主动回应",
    "根据以下环境主动回应",
    "根据环境主动回应",
)

# 视觉"看/看到/看见"在 normalized 文本中作为子串出现即可，允许在前面插入时间/指代副词。
# 子串必须独立，避免与"看到我"等非视觉意图混淆。
_VISUAL_SEE_VERBS = ("看到了什么", "看到什么", "看到画面", "看到镜头", "看到桌子上", "看到桌上", "看到画面里", "看到画面中的")
_VISUAL_SEE_VERB_ALONE = ("看到", "看到过", "看到画面")
_VISUAL_QUESTION_TAILS = ("是什么", "有什么", "怎么样", "如何", "好看", "合适", "适合", "什么颜色", "看到了", "看见", "看清", "认出", "识别", "了", "吗", "的")
# 时间/指代副词可插入"你+视觉动词"之间，例如"你现在看到了什么"、"刚才画面里有什么"。
_VISUAL_ADVERB_PREFIXES = ("现在", "刚才", "刚才的", "之前", "现在这个", "刚才那个", "现在那个", "刚才那个画面", "现在这个画面")


def _matches_visual_see_pattern(normalized: str) -> bool:
    """识别 'X 看到/看到X 是什么/有什么' 结构。

    处理以下变体（normalized 文本中以连续子串形式出现）：
        - "你看到了什么"、"你现在看到了什么"、"刚才你看到了什么"
        - "你看到画面里有什么"、"刚才画面里有什么"
        - "你看到画面里的什么"、"桌面上有什么"
    """
    if not normalized:
        return False
    for verb in _VISUAL_SEE_VERBS:
        if verb in normalized:
            return True
    # "你 + 副词? + 看到 + (了|了过|过) + 什么" 模式
    for prefix in ("", *_VISUAL_ADVERB_PREFIXES):
        head = f"你{prefix}看到" if prefix else "你看到"
        if head in normalized:
            tail_idx = normalized.find(head) + len(head)
            tail = normalized[tail_idx:]
            if any(tail.startswith(suffix) for suffix in ("了什么", "到什么", "了画面", "到画面")):
                return True
            if "了" in tail and any(q in tail for q in ("什么", "谁", "哪", "怎么", "吗")):
                return True
    # "画面/桌上/镜头里 + 有什么/是什么" 反向模式
    subjects = ("画面", "画面里", "画面中", "镜头", "镜头里", "桌子上", "桌上", "桌面上", "身上", "背景", "身上穿")
    for subj in subjects:
        if subj in normalized and any(q in normalized for q in ("有什么", "是什么", "怎么样", "有什么东西", "看到什么")):
            return True
    # 帮我看看/看下 + 视觉主语 模式（"帮我看看周围"、"看下我身上"）
    look_verbs = ("帮我看看", "帮我看一下", "帮我看下", "看一下", "看看", "看下")
    for verb in look_verbs:
        idx = normalized.find(verb)
        if idx == -1:
            continue
        tail = normalized[idx + len(verb):]
        if any(tail.startswith(s) for s in ("周围", "周围环境", "周围有什么", "我身上", "我穿的", "我穿", "背景", "画面", "桌面", "桌上")):
            return True
    return False


def is_visual_context_intent(text: str) -> bool:
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]+", "", str(text or "")).lower()
    if not normalized:
        return False
    if any(phrase in normalized for phrase in _NON_VISUAL_CONTEXT_PHRASES):
        return False
    if any(phrase in normalized for phrase in _COMPOUND_NON_VISUAL_PHRASES):
        return False
    if _matches_visual_see_pattern(normalized):
        return True
    if any(phrase in normalized for phrase in _VISUAL_INTENT_PHRASES):
        return True
    return (
        any(phrase in normalized for phrase in _VISUAL_SUBJECT_PHRASES)
        and any(phrase in normalized for phrase in _VISUAL_QUESTION_PHRASES)
    )


@dataclass(frozen=True)
class VisionSnapshot:
    jpeg: bytes
    frame_id: str
    captured_at_ms: int | None
    age_ms: int | None

    def data_url(self) -> str:
        encoded = base64.b64encode(self.jpeg).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"


@dataclass(frozen=True)
class VisionFetchResult:
    status: str
    snapshot: VisionSnapshot | None = None
    http_status: int | None = None


def build_visual_user_content(text: str, snapshot: VisionSnapshot) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": str(text or "").strip()},
        {
            "type": "image_url",
            "image_url": {"url": snapshot.data_url()},
        },
    ]


class VisionSnapshotClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_sec: float,
        async_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/") + "/"
        self.token = str(token or "").strip()
        self._client = async_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            trust_env=False,
        )

    async def fetch_latest(self, session_id: str) -> VisionFetchResult:
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return VisionFetchResult(status="invalid_session")
        if not self.base_url or not self.token:
            return VisionFetchResult(status="not_configured")

        url = urljoin(self.base_url, "internal/vision/snapshot")
        try:
            response = await self._client.get(
                url,
                params={"session_id": safe_session_id},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.TimeoutException:
            return VisionFetchResult(status="timeout")
        except httpx.HTTPError:
            return VisionFetchResult(status="network_error")

        if response.status_code == httpx.codes.NOT_FOUND:
            return VisionFetchResult(status="unavailable", http_status=response.status_code)
        if response.status_code != httpx.codes.OK:
            return VisionFetchResult(
                status="gateway_error",
                http_status=response.status_code,
            )
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "image/jpeg":
            return VisionFetchResult(
                status="invalid_content_type",
                http_status=response.status_code,
            )

        jpeg = bytes(response.content)
        if not jpeg or len(jpeg) > VISION_IMAGE_MAX_BYTES:
            return VisionFetchResult(
                status="invalid_image_size",
                http_status=response.status_code,
            )
        if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            return VisionFetchResult(
                status="invalid_jpeg",
                http_status=response.status_code,
            )

        return VisionFetchResult(
            status="ok",
            http_status=response.status_code,
            snapshot=VisionSnapshot(
                jpeg=jpeg,
                frame_id=response.headers.get("x-vision-frame-id", ""),
                captured_at_ms=_optional_int(response.headers.get("x-vision-captured-at-ms")),
                age_ms=_optional_int(response.headers.get("x-vision-age-ms")),
            ),
        )


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
