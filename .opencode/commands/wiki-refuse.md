---
description: 一键验证证据不足时不会编造精确视觉数据
agent: build
---

请加载 `multimodal-wiki` Skill。调用 `wiki_query` 工具，参数如下：

- `question`：`Figure 4 中蓝色方框的 RGB 精确数值是多少？`
- `mode`：`multimodal`
- `provider`：`api`

请明确区分“看过图片”和“图片足以支持精确 RGB 数值”。如果 Evidence 不足，必须直接拒答并说明还缺少什么。`wiki_query` 调用成功后逐字输出其完整 Markdown，不得压缩拒答理由，不得删除已核验的原图、Evidence 和运行信息。
