# 多模态文章页码与段落级溯源方案

**日期：** 2026-08-25
**适用仓库：** `/Users/doppler/Documents/Github/multimodal-llm-wiki/`
**方案状态：** 设计稿
**首期范围：** 不依赖整页截图、PDF 页面渲染或 `bbox` 高亮

## 1. 先说结论

多模态 Wiki 的引用不应只显示一个难以理解的 `Evidence ID`，而应把一条回答证据整理成一张人类可读的“证据卡片”：

> **来源文章** → **第 N 页** → **内容块/段落** → **原文摘录** → **图片、表格或公式** → **稳定 Evidence ID**

首期把 MinerU 的 `item` 作为最小引用单元。它比自然语言中的“句子”更可靠，因为 `item` 已经具备 `page_start`、`item_id`、`raw_ref`、内容类型和资源引用。页面上把它称为“段落”或“内容块”时，根据 `item_type` 做区分：正文使用“段落”，图片、表格和公式使用“内容块”，避免把解析块误称为语义完整段落。

点击答案中的 `〔1〕` 后，右侧展示证据卡片；卡片可以继续打开现有的来源页锚点或多模态证据地图。首期不承诺在整页图片上框选坐标，但保留现有 `bbox`，为以后增加页面渲染回跳留下接口。

## 2. 当前仓库已经有什么

仓库已经具备大部分底层能力：

- `mmwiki-0.1 Source Package` 保存 `manifest.json`、`items.jsonl`、`chunks.jsonl`、`assets.json`、原始 MinerU JSON 和图片资源。
- `Item` 已保存 `page_start/page_end`、`bbox`、`item_id`、`raw_text`、`caption`、`search_text`、`asset_ids` 和 `provenance.raw_ref`。
- `Chunk` 已保存 `item_ids`、`page_refs`、`asset_ids` 和检索用文本。
- Pipeline 已生成稳定形式的 Evidence ID：`package_id@source_checksum#item_id`。
- 来源页已经为每个 item 生成 HTML/Markdown 锚点；多模态证据地图已经可以展示图片、表格、公式、Caption、OCR 和语义说明。
- 查询页已经支持答案区、Wiki 页面区、原始 Evidence 区和编号引用切换。

当前主要缺口不是“没有证据”，而是“证据没有被翻译成人类一眼能读懂的定位信息”：引用卡片没有统一展示段落摘录、内容块编号、原始字段和派生字段的关系，用户需要理解底层 ID 才能核验。

## 3. 方案选择

### 方案 A：继续直接展示 Evidence ID

只在答案旁显示 `〔package@version#item〕`，点击后打开来源页。

优点是改动最少；缺点是对普通用户不友好，页码、段落、图表和原始资源之间的关系不直观，不适合作为最终体验。

### 方案 B：Evidence Resolver + 人类可读证据卡片（推荐）

增加一个统一的证据解析层，把 `evidence_id` 解析成页面展示模型，再由查询页面渲染：来源、页码、内容类型、章节、摘录、图片/表格/公式、派生说明和回链。

优点是复用现有数据和查询页面，引用展示与检索逻辑解耦，后续加入整页截图时只需扩展定位字段。代价是需要补充一层确定性格式化和测试。

### 方案 C：首期直接生成整页图并做坐标高亮

为每页保存 PNG 或 PDF 页面渲染结果，用 `bbox` 在前端框选。

定位体验最好，但需要上游提供 PDF 或增加页面渲染产物，资源体积、渲染服务和坐标系适配都会显著扩大首期范围。本次明确不采用，作为后续增强方向。

## 4. 首期用户体验

### 4.1 答案中的引用

回答正文继续使用短编号，例如：

> ReToken 使用价值空间相似度选择视觉 token。〔1〕

编号只负责阅读，不把内部 ID 直接塞进正文。每个编号必须对应一个经过校验的 Evidence ID；如果模型引用了不存在的 ID，回答继续沿用现有的拒绝或证据不足逻辑。

### 4.2 证据卡片

点击 `〔1〕` 后展示：

```text
Evidence 1 · 正文段落
来源：论文_002_cs_LG.pdf
版本：1d9dabf3e92b
位置：第 3 页 · 章节/面包屑
内容块：item-p0003-b0004
原始引用：content_list_v2[2][3]

摘录：……保留自 MinerU 的 raw_text……

Evidence ID：论文_002_cs_LG@1d9dabf3e92b#item-p0003-b0004
[打开来源记录] [打开多模态证据地图]
```

对图片、图表、表格和公式，卡片增加对应内容：

- 图片或图表：原始资源预览、原始 Caption、Image Caption、Image OCR。
- 表格：完整或可滚动的结构化表格，不用检索摘要替代原表格。
- 公式：LaTeX 和可读文本。
- 模型推断：明确标记为“派生信息/推断”，不能替代原始图片、表格或公式。

当页码或原文缺失时显示“未记录”，不根据上下文猜测；当证据是多个 item 时，显示为同一个证据组下的多个内容块。

### 4.3 页面布局

沿用现有 `render_query_html` 的左右布局：

- 左侧：问题、回答、短引用编号和运行信息。
- 右侧：默认显示“证据卡片”；保留“Wiki 页面”和“原始 Evidence”切换。
- 卡片顶部固定显示“第 N 页”和内容类型，用户无需先理解 Evidence ID。
- 原始来源页继续使用 `item_id` 锚点回跳；多模态证据地图继续作为图文集中浏览入口。

## 5. 数据与定位契约

### 5.1 不改变现有稳定身份

继续使用：

```text
evidence_id = package_id@source_version#item_id
```

其中 `source_version` 使用当前 package checksum 的短版本。它能避免文章重新解析后，新旧证据混淆；旧版本仍保留在 runtime 时，历史引用仍可解析。

### 5.2 增加统一的展示定位模型

不要求模型直接生成展示文本，由 Evidence Resolver 从现有字段确定性生成：

```json
{
  "evidence_id": "论文_002_cs_LG@1d9dabf3e92b#item-p0003-b0004",
  "source_title": "论文_002_cs_LG.pdf",
  "source_version": "1d9dabf3e92b",
  "item_id": "item-p0003-b0004",
  "kind": "paragraph",
  "page_index": 3,
  "page_label": "第 3 页",
  "breadcrumb": "2. Method",
  "raw_ref": "content_list_v2[2][3]",
  "excerpt": "……",
  "asset_ids": [],
  "bbox": {"values": [169, 506, 826, 563], "coordinate_system": "normalized_1000"},
  "links": {
    "source_item": "wiki/sources/论文_002_cs_LG.md#item-p0003-b0004",
    "evidence_map": "wiki/evidence/论文_002_cs_LG-multimodal.md"
  }
}
```

字段规则：

- `page_index` 直接来自现有 `page_start`，统一按 1-based 展示。
- `item_id` 是首期稳定的内容块标识，不另造一个可能漂移的自然语言段落 ID。
- `raw_ref` 用于工程核验，不作为主要用户文案。
- `excerpt` 按 `raw_text → caption → search_text` 的顺序选择，并限制展示长度；原文完整内容仍留在来源页。
- `kind` 由 `item_type` 映射为“正文段落、标题、图片、图表、表格、公式、代码、其他”。
- `bbox` 继续透传但首期只作为元数据展示或未来接口，不触发页面高亮。

### 5.3 Chunk 与 Item 的职责

- `Chunk` 是召回单元，不直接作为最终阅读定位单元。
- `Chunk.item_ids` 用来展开成一个或多个 Evidence 卡片。
- `Item` 是最终引用单元，负责页码、内容块、原文和资源回链。
- 图片 OCR、Caption 等 `visual_evidence` 是 Item/Asset 的派生证据，必须保留父级 Evidence ID 和 `parent_item_ids`。

## 6. 端到端数据流

```text
MinerU Source Package
  ├─ manifest / items / chunks / assets / raw
  ↓
校验与归档
  ↓
Item Locator：page_index、item_id、raw_ref、excerpt、asset refs
  ↓
Wiki 来源页 + 多模态证据地图
  ↓
页面定位检索 → Chunk 召回 → 展开 Item
  ↓
Evidence Resolver 生成证据卡片
  ↓
模型只返回允许集合内的 evidence_refs
  ↓
查询页面把内部引用映射为 〔1〕、〔2〕，点击查看卡片
```

关键原则是：检索可以使用文本代理、Caption、OCR 和向量；最终回答仍然必须回到真实 Item、Asset、表格或公式。Caption 和模型说明只帮助找到证据，不能冒充原始证据。

## 7. 实施拆分

### 阶段一：引用展示契约

1. 在 `mmwiki` 中增加纯函数式 Evidence Resolver，不修改 Source Package。
2. 统一处理 Evidence ID、Item、Chunk、Asset 和 `visual_evidence` 的关联。
3. 增加 `kind`、`page_label`、`excerpt`、`breadcrumb`、`raw_ref` 和来源链接的确定性格式化。
4. 保持现有证据校验：不存在的 Evidence、Item 或 Asset 一律不能渲染成有效引用。

### 阶段二：查询页面的人类阅读体验

1. 改造 `render_query_html` 的右侧面板，默认显示证据卡片。
2. 保留现有 Wiki/原始 Evidence 切换和 item 锚点。
3. 对图片、表格、公式增加图文混排卡片；对派生 Caption/OCR 增加明确标签。
4. 对缺页码、缺摘录、资源缺失和需要复核的 item 显示状态，而不是静默隐藏。

### 阶段三：评测与文档

1. 用仓库内置的 5 份 Source Package 覆盖正文、图片、图表、表格、公式和 OCR 场景。
2. 增加查询展示测试：引用编号、页码、item 锚点、摘录、资源和派生证据都能对应上。
3. 增加回归检查：版本变化不串证据，chunk 多 item 不丢引用，模型返回非法引用会被拒绝。
4. 更新 README、`config/schema.md` 和 OpenCode Skill，说明“段落”在首期实际上是 MinerU 内容块。

## 8. 验收标准

首期完成后，用户应能做到：

- 从回答中的 `〔1〕` 在一次点击内看到来源文章、页码、章节、内容类型和可读摘录。
- 对正文准确看到对应 `item_id` 和 MinerU `raw_ref`；对图片、表格和公式看到原始资源或完整结构。
- 在不查看底层 JSON 的情况下理解“这条结论来自哪篇文章的哪一页、哪一块内容”。
- 从证据卡片打开现有来源页锚点，继续查看上下文。
- 清楚区分原始 Caption、OCR、模型 Caption 和模型推断。
- 当证据不完整时看到明确的“未记录/需复核/资源缺失”，系统不伪造精确位置。

技术验收至少包括：

- 所有展示出来的引用都能通过 `evidence_id → item_id → source package` 解析。
- `page_index` 与 `Item.page_start` 一致；`page_refs` 与展开后的 Item 页码一致。
- 所有 `asset_ids` 都能在 `assets.json` 或 runtime 资源中解析。
- 生成页面和查询页面的证据卡片使用同一套 Resolver，避免两套定位规则漂移。

## 9. 后续增强，不纳入首期

- 把 PDF 或每页 PNG 纳入 Source Package，支持整页浏览。
- 使用归一化 `bbox` 在页面图上高亮段落、表格或图片。
- 在 MinerU 提供可靠字符偏移时，增加句子级或字符级定位。
- 对跨页段落建立 `page_start/page_end + 多个 bbox` 的复合定位。
- 将证据卡片抽成独立 API，供 Web、Obsidian 和 OpenCode Desktop 共用。

这些增强都可以复用首期的 `evidence_id`、`item_id` 和定位模型，不需要推翻已有数据结构。
