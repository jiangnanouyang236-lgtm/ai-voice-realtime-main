from singing.matcher import extract_song_query, match_song


SONGS = [
    {
        "id": "001",
        "slug": "aini",
        "title": "爱你",
        "artist": "王心凌",
        "aliases": ["爱泥", "王心凌的爱你"],
    },
    {
        "id": "002",
        "slug": "caihongdeweixiao",
        "title": "彩虹的微笑",
        "artist": "王心凌",
        "aliases": ["彩虹微笑"],
    },
    {
        "id": "003",
        "slug": "jiemaowanwan",
        "title": "睫毛弯弯",
        "artist": "王心凌",
        "aliases": [],
    },
]


def test_extract_song_query_preserves_title_but_removes_request_words():
    assert extract_song_query("请你给我唱一首《彩虹的微笑》我听听") == "彩虹的微笑"
    assert extract_song_query("唱首当你吧") == "当你"


def test_match_song_accepts_alias_and_single_character_asr_error():
    alias = match_song("唱首爱泥", SONGS)
    typo = match_song("给我唱一下接毛弯弯", SONGS)

    assert alias.status == "matched"
    assert alias.best.song["id"] == "001"
    assert typo.status == "matched"
    assert typo.best.song["id"] == "003"


def test_match_song_rejects_unrelated_name_instead_of_randomizing():
    result = match_song("唱首完全不存在的歌", SONGS)

    assert result.status == "not_found"


def test_match_song_marks_close_candidates_as_ambiguous():
    songs = [
        {"id": "a", "title": "彩虹微笑", "slug": "a", "aliases": []},
        {"id": "b", "title": "彩红微笑", "slug": "b", "aliases": []},
    ]

    result = match_song("彩鸿微笑", songs)

    assert result.status == "ambiguous"
    assert [candidate.song["id"] for candidate in result.candidates] == ["a", "b"]


def test_match_song_recognizes_generic_request():
    for query in (
        "随便唱一首歌",
        "给我唱一首歌吧。",
        "请你给我唱首歌好吗？",
        "来一首歌曲我听听",
    ):
        assert match_song(query, SONGS).status == "generic"
