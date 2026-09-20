# Vision Acceptance Fixture

Vision Gold 不接受文字占位、生成描述或缺失图片的 case。每条 fixture 必须同时提供：

- 仓库内固定 JPEG、PNG 或 WebP 文件；
- 图片 SHA-256；
- 可直接观察的 `required_facts`；
- 不应出现的 `forbidden_facts`；
- 独立审核记录。

当前固定样本为 `classroom-001.jpg`，由 owner 提供的 PNG 等比例转换为 1280×720 JPEG；`cases.jsonl` 绑定转换后文件 SHA-256。Gold 只记录肉眼可直接确认的场景元素，不推断精确人数、人物身份或难以辨认的黑板文字。

图片进入 Gold 只表示 fixture 和事实标签获得批准；只有实际 Vision 服务返回结果通过事实检查，才能声明对应运行环境的 Vision 验证完成。
