# 真人录音说明

目标是 30～50 个 candidate end，Synthetic TTS 与 Human Recorded 必须分别统计。

建议覆盖犹豫、填充词、300～1500 ms 思考停顿、说到一半、改口、长句、短回答和拖尾语气。录音前取得说话人同意；原始音频放入 `audio/`，不提交仓库。

每条录音在独立 manifest 中记录：匿名 id、类别、ground truth、candidate 时间点、trailing silence、采样率、声道、设备类型和录制日期。不要记录姓名等不必要的个人信息。
