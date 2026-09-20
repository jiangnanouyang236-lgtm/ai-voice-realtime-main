import json

from llm.llm_client import (
    _authoritative_tool_result_response,
    _terminal_exit_response_for_tool,
)


def test_singing_tool_result_becomes_asset_control_without_exit():
    result = json.dumps({
        "kind": "singing_playback",
        "song_id": "005",
        "title": "睫毛弯弯",
        "voice_id": "serena-v1",
        "asset_id": "serena-v1:005",
    })

    assert _terminal_exit_response_for_tool("singing_remote.play_song", result) == (
        "[SINGING_PLAYBACK:serena-v1:005]"
    )


def test_singing_unavailable_result_uses_product_copy():
    result = json.dumps({
        "kind": "singing_unavailable",
        "reason": "song_not_found",
        "message": "这首歌我还没学会，换一首好吗？",
    }, ensure_ascii=False)

    assert _terminal_exit_response_for_tool("singing_remote.play_song", result) is None
    assert _authoritative_tool_result_response("singing_remote.play_song", result) == (
        "这首歌我还没学会，换一首好吗？"
    )


def test_singing_catalog_is_formatted_for_voice_response():
    result = json.dumps({
        "kind": "singing_catalog",
        "query": "",
        "total_available": 2,
        "songs": [
            {"song_id": "001", "title": "爱你", "artist": "王心凌"},
            {"song_id": "002", "title": "彩虹的微笑", "artist": "王心凌"},
        ],
        "has_more": False,
    }, ensure_ascii=False)

    response = _authoritative_tool_result_response("singing_remote.list_songs", result)

    assert response == "我现在会唱《爱你》、《彩虹的微笑》。你想听哪一首？"
