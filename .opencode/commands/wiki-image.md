---
description: 一键演示 Figure 4 原图理解和多模态检索
agent: build
---

请加载 `multimodal-wiki` Skill。调用 `wiki_query` 工具，参数如下：

- `question`：`根据 Figure 4，ReToken 推理时的数据流是什么？请按顺序说明，并指出图中缓存的对象。`
- `mode`：`multimodal`
- `provider`：`api`

`wiki_query` 已返回排版完整的最终 Markdown。调用成功后必须逐字输出工具返回值，不得概括、改写、重排或另写总结；必须保留全部步骤、知识页链接、证据摘录、命中的原图 Markdown、Evidence ID 和实际检索模式，不能用 Caption 冒充读图结果。
