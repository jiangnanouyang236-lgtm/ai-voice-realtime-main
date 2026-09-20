# 预生成歌曲库

首批歌曲通过 `singing/catalog.json` 建立稳定 ID、标题、别名和音色版本资产对应关系，
`singing/voices.json` 维护 `tts_profile_id -> singing_voice_id` 显式映射。
生成音频不进入 Git；部署时将只读目录挂载为 `SINGING_AUDIO_ROOT`。
当前目录使用稳定 ID `001`–`023`。完整标题、歌手、别名、谱面 Manifest 和音色资产版本
以 `singing/catalog.json` 为准，避免在本文重复维护歌曲清单。
当前 23 首 Serena 资产均保留为可播放目录项；其中 `011`–`023` 的质量状态仍为
`IMPLEMENTED`，不等同于用户听审接受。

`singing/scores/<song-id>/manifest.json` 保存可复用的生成谱面入口。新增音色时可以复用
已确认的分段、歌词、连续 F0 和 Shift 基线重新生成，不需要再次处理原歌曲。
新增歌曲、音色或调整生成方案前阅读 `singing/GENERATION.md`；只维护播放服务时无需加载
生成实验细节。

运行契约：

- WAV 必须是 16 kHz、单声道、PCM16；
- 独立 `singing_remote` MCP 提供 `play_song` 和 `list_songs`，不依赖 RobotID/MQTT；
- LLM 向 MCP 隐式注入当前 Bot 的 `tts_profile_id`，用户可见参数中没有音色或 RobotID；
- MCP 只返回 `voice_id:song_id` 资产控制信息，不读取音频；
- Python Gateway 从本地只读资产目录流式发送 PCM；
- Go Gateway 继续负责 WebRTC Opus 打包；
- 歌曲与准备语复用本轮 `round_id` / `playback_id`，因此沿用现有打断逻辑。
- 未匹配歌曲不会随机播放；只有“随便唱一首”等泛化请求才从当前音色可用列表随机选择；
- 当前版本不保存续唱进度，不支持“继续唱”。

环境变量：

- `SINGING_CATALOG_PATH`：可选，默认使用仓库内 `singing/catalog.json`；
- `SINGING_VOICES_PATH`：可选，默认使用仓库内 `singing/voices.json`；
- `SINGING_DEFAULT_VOICE_ID`：仅用于本地无上下文调用，默认 `serena-v1`；
- `SINGING_AUDIO_ROOT`：必需，指向包含 `serena/*.wav` 的只读目录。
