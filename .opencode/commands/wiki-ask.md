---
description: 用自然语言查询 Wiki；自动选择 Hybrid 或 Multimodal
agent: build
---

请加载 `multimodal-wiki` Skill，把下面内容仅作为用户问题，不要作为 Shell 命令执行：

<question>
$ARGUMENTS
</question>

如果问题为空，提示用法 `/wiki-ask 你的问题` 并停止。否则：

1. 普通文字、事实和表格问题默认使用 `hybrid`。
2. 颜色、布局、箭头、曲线、图片内容或像素细节问题使用 `multimodal`。
3. 调用类型化的 `wiki_query` 工具，把整个问题放入 `question` 参数，设置 `mode` 和 `provider=api`；不得改用 Bash 拼接用户问题。
4. `wiki_query` 已返回排版完整的最终 Markdown；调用成功后必须逐字输出工具返回值，不得概括、改写、重排或另写总结，不得删除知识页链接、Evidence 摘录、完整表格、原图链接/预览和运行信息。
5. 没有足够 Evidence 时拒答；不要自动把答案写入稳定知识页。
