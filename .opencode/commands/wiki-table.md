---
description: 一键演示完整表格回读和带 Evidence 引用的问答
agent: build
---

请加载 `multimodal-wiki` Skill。调用 `wiki_query` 工具，参数如下：

- `question`：`开发测试阶段需要多少天、多少人、多少预算？`
- `mode`：`hybrid`
- `provider`：`api`

`wiki_query` 已返回排版完整的最终 Markdown。调用成功后必须逐字输出工具返回值，不得概括、改写、重排或另写总结；必须保留知识页链接、Evidence 摘录、完整表格和运行信息，不能只显示 Caption 或压缩后的文本。
