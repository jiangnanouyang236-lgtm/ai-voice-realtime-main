import json

import pytest

from mcp_servers import singing_sse_server
from singing.library import SingingCatalogError
from singing.service import NOT_LEARNED_TEXT, SingingService


def _fixtures():
    voices = {
        "schema_version": "singing-voices/v1",
        "default_voice_id": "serena-v1",
        "voices": [
            {"id": "serena-v1", "display_name": "Serena", "tts_profile_ids": ["default_tts_profile"]},
            {"id": "kangkang-v1", "display_name": "Kangkang", "tts_profile_ids": ["kangkang_tts"]},
        ],
    }
    variant = {"asset": "placeholder.wav", "duration_seconds": 30, "sha256": "0" * 64}
    catalog = {
        "schema_version": "singing-catalog/v2",
        "songs": [
            {
                "id": "001", "slug": "aini", "title": "爱你", "artist": "王心凌",
                "aliases": ["爱泥"],
                "variants": {"serena-v1": {**variant, "asset": "serena/001.wav"}},
            },
            {
                "id": "002", "slug": "dangni", "title": "当你", "artist": "林俊杰",
                "aliases": [],
                "variants": {
                    "serena-v1": {**variant, "asset": "serena/002.wav", "sha256": "1" * 64},
                    "kangkang-v1": {**variant, "asset": "kangkang/002.wav", "sha256": "2" * 64},
                },
            },
            {
                "id": "003", "slug": "qifengle", "title": "起风了", "artist": "买辣椒也用券",
                "aliases": [],
                "variants": {"kangkang-v1": {**variant, "asset": "kangkang/003.wav", "sha256": "3" * 64}},
            },
        ],
    }
    return catalog, voices


class _PickLast:
    @staticmethod
    def choice(values):
        return values[-1]


@pytest.fixture
def service():
    catalog, voices = _fixtures()
    return SingingService(catalog=catalog, voice_registry=voices, chooser=_PickLast())


def test_play_song_fuzzy_match_returns_current_voice_asset(service):
    result = service.play_song("唱首爱泥我听听", voice_id="serena-v1")

    assert result["kind"] == "singing_playback"
    assert result["song_id"] == "001"
    assert result["voice_id"] == "serena-v1"
    assert result["asset_id"] == "serena-v1:001"


def test_generic_play_only_randomizes_current_voice_songs(service):
    result = service.play_song("随便唱首歌", voice_id="serena-v1")

    assert result["song_id"] == "002"


def test_known_song_without_current_voice_variant_declines(service):
    result = service.play_song("唱首起风了", voice_id="serena-v1")

    assert result["kind"] == "singing_unavailable"
    assert result["reason"] == "voice_variant_missing"
    assert result["message"] == NOT_LEARNED_TEXT


def test_unknown_song_declines_instead_of_randomizing(service):
    result = service.play_song("唱首不存在的歌曲", voice_id="serena-v1")

    assert result["kind"] == "singing_unavailable"
    assert result["reason"] == "song_not_found"
    assert result["message"] == NOT_LEARNED_TEXT


def test_list_songs_filters_current_voice_and_artist(service):
    all_songs = service.list_songs("", voice_id="serena-v1", limit=10)
    artist = service.list_songs("王心凌", voice_id="serena-v1", limit=10)

    assert [song["song_id"] for song in all_songs["songs"]] == ["001", "002"]
    assert [song["song_id"] for song in artist["songs"]] == ["001"]
    assert all(song["song_id"] != "003" for song in all_songs["songs"])


def test_list_songs_paginates_without_dumping_catalog(service):
    first = service.list_songs("", voice_id="serena-v1", limit=1)
    second = service.list_songs("", voice_id="serena-v1", limit=1, cursor=first["next_cursor"])

    assert first["has_more"] is True
    assert first["songs"][0]["song_id"] == "001"
    assert second["has_more"] is False
    assert second["songs"][0]["song_id"] == "002"


def test_unknown_hidden_voice_is_rejected(service):
    with pytest.raises(SingingCatalogError, match="未知唱歌音色"):
        service.resolve_voice_id({"singing_voice_id": "missing-v1"})


def test_tts_profile_maps_to_singing_voice_without_robot_id(service):
    assert service.resolve_voice_id({"tts_profile_id": "kangkang_tts"}) == "kangkang-v1"
    assert service.resolve_voice_id({"tts_profile_id": "unknown_tts"}) == ""


def test_deployment_registry_maps_wzk_serena_profile():
    assert SingingService().resolve_voice_id({"tts_profile_id": "wzk-serena"}) == "serena-v1"


def test_versioned_catalog_keeps_all_retained_serena_songs_playable():
    deployed = SingingService()
    songs = deployed.list_songs("", voice_id="serena-v1", limit=10)
    catalog_ids = [song["id"] for song in deployed.catalog["songs"]]

    assert catalog_ids == [f"{value:03d}" for value in range(1, 24)]
    assert songs["total_available"] == 23
    assert deployed.play_song("后来", voice_id="serena-v1")["song_id"] == "011"
    assert deployed.play_song("樱花草", voice_id="serena-v1")["song_id"] == "023"


def test_mcp_tools_do_not_expose_voice_or_robot_id():
    schemas = {
        tool["name"]: set(tool["inputSchema"]["properties"])
        for tool in singing_sse_server.TOOLS_LIST
    }

    assert schemas == {
        "play_song": {"query"},
        "list_songs": {"query", "limit", "cursor"},
    }


def test_mcp_dispatch_uses_hidden_voice_context(monkeypatch, service):
    monkeypatch.setattr(singing_sse_server, "SERVICE", service)

    result = json.loads(singing_sse_server._call_tool(
        "play_song",
        {"query": "起风了", "_meta": {"singing_voice_id": "kangkang-v1"}},
    ))

    assert result["kind"] == "singing_playback"
    assert result["asset_id"] == "kangkang-v1:003"


def test_jsonrpc_lists_independent_singing_tools():
    response = singing_sse_server.handle_jsonrpc_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })

    assert [tool["name"] for tool in response["result"]["tools"]] == ["play_song", "list_songs"]
