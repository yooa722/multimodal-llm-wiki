# 多模态 LLM Wiki Schema

本 Wiki 用于把解析组交付的文本、表格、图片、图表和公式整理为可追溯的 Markdown 知识页。

## 分层

- `runtime/raw/` 保存不可变 package 副本。模型不得修改。
- `runtime/vault/wiki/sources/` 保存带文本、完整表格、图片和 Evidence ID 的来源页。
- `runtime/vault/wiki/evidence/` 保存按来源生成的多模态证据地图，集中展示文档结构、页码、原始 Caption、表格、公式和图片。
- `runtime/vault/wiki/concepts/`、`entities/`、`analyses/` 保存模型维护的知识页。
- `runtime/vault/wiki/index.md`、`overview.md` 和只追加的 `log.md` 负责导航与维护记录。
- 本文件定义页面和操作规则。

## 页面规则

- 页面按概念、实体、跨来源分析组织，不按每个 chunk 各建一页。
- 每页必须有 YAML frontmatter，至少包含 `summary`、`source_ids`、`source_versions`、`evidence_ids` 和 `evidence_modalities`。
- 正文中的关键结论必须能回到 Evidence ID。
- 使用 `[[WikiLink]]` 连接相关页面。没有实际关系时不强行链接。
- 新来源与旧页面相关时更新旧页面；发生冲突时并列记录来源，不自行裁决。
- 图片和表格是一等证据，必须保留原始资源链接；模型说明不能替代原始事实。
- API 构建时，有原始图片且视觉模型可用，分析阶段必须读取图片本身；不能只根据 Caption、相邻文字或上游语义说明声称理解了图像。
- 图片可见文字、原始 Caption 和表格单元格属于 `extracted`；从布局、箭头、颜色或跨证据关系得到的解释属于 `inferred`；看不清或来源冲突属于 `ambiguous`。
- 稳定知识页中的原图、完整表格、公式和来源跳转由 Pipeline 根据 Evidence ID 确定性回填，模型不得自行构造资源路径或表格事实。

## 操作规则

- Ingest：分析新来源，再创建或更新页面，同时更新 `index.md` 并追加 `log.md`。
- Query：通过 Wiki 与来源关系定位上游 chunk，再读取原始 item、表格和图片回答；答案必须引用 Evidence。
- Lint：检查缺失页面、资源、引用、重复标题、断链、孤立页和待创建页面。
- 查询结果不自动写回 Wiki。
