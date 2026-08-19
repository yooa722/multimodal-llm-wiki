# 轻量增量多模态 Wiki 优化说明与业界方案对比

## 1. 文档结论

本次优化的核心不是新增一套独立的视觉 RAG，而是：

> 在不改写用户原始 Markdown Wiki 的前提下，把 MinerU 已解析出的 Caption 作为图片语义代理，增量接入现有文本 Wiki、Evidence、Skill、检索和本地渲染流程。

当前阶段面向 OpenCode Skill/CLI Demo，优先验证三个问题：

1. 用户已有文本 Wiki 是否可以低风险接入多模态能力；
2. 不依赖 VLM 和向量模型时，图片语义是否仍然可以被检索；
3. 工程复杂度、调用成本和用户使用门槛是否可控。

当前结论是：

- 方案可以满足“轻量增量接入”的目标；
- 默认路径为 **MinerU Caption + BM25/关键词检索**，不默认调用 VLM、Embedding 或 Rerank；
- 原始 Wiki 保持只读，导入结果是可追溯的派生 Wiki；
- 相较完整的多模态 Wiki 产品，本方案功能范围更窄，但在已有 Wiki 兼容、低成本、可回退和工程风险控制方面更有针对性；
- 不应宣称当前方案已经全面超过 `nashsu/llm_wiki`，当前优势是“接入路径和成本控制”，而不是“视觉理解能力和产品成熟度”。

## 2. 本次已经实现的能力

### 2.1 总体流程

```text
用户已有 Markdown Wiki
        │
        ├─ 原始 Markdown / 原始图片：只读，不改写
        │
        ├─ 读取图片内容并计算 SHA-256
        │        │
        │        └─ 匹配 MinerU Source Package 的 Asset SHA-256
        │                         │
        │                         └─ 复用 MinerU Caption
        │
        └─ 生成派生 Wiki
             ├─ pages/       派生 Markdown
             ├─ assets/      去重后的本地图片
             └─ manifest.json 来源、Caption、状态和错误记录
                          │
                          └─ 注册为现有文本 Wiki 外部来源
                              ├─ 页面正文 + 图片 alt 进入 BM25
                              ├─ 继续使用现有 Evidence / source_id / version
                              └─ 继续使用现有本地 Web Renderer
```

派生目录为：

```text
runtime/vault/wiki/external/<wiki_id>/
├── pages/
├── assets/
└── manifest.json
```

### 2.2 已实现功能清单

| 能力 | 当前实现 | 产品效果 |
|---|---|---|
| OpenCode 接入 | 复用现有 `multimodal-wiki` Skill，并增加导入、状态和查询说明 | 用户仍然从同一个 Skill/CLI 入口操作 |
| 已有 Wiki 导入 | `python3 app.py ingest-wiki ...` | 不要求用户重建原有 Wiki |
| 图片语法 | 支持标准 Markdown `![alt](path)` 和 Obsidian `![[path]]` | 兼容常见个人 Wiki 写法 |
| Caption 关联 | 用户图片 SHA-256 匹配 MinerU Asset SHA-256 | 不依赖文件名和目录结构，避免同名误关联 |
| Caption 物化 | 写入派生 Markdown 图片 `alt` | Caption 可以直接进入已有 BM25/关键词检索 |
| 图片去重 | 相同图片内容只复制一次，多个引用复用派生资产 | 降低磁盘占用和重复处理 |
| 缺失处理 | Caption 缺失保留原 alt；无 alt 则保留空 alt并记录 `caption_missing` | 不自动猜测图片内容，避免隐性幻觉 |
| 安全路径 | 拒绝远程、绝对、越界图片路径 | 单张图片异常不会拖垮整库，也不自动下载未知资源 |
| VLM 开关 | `MMWIKI_ENABLE_VLM=false`，支持 `--vlm on/off` | 默认不产生视觉模型成本，需要时再开启 |
| 向量检索开关 | `MMWIKI_ENABLE_VECTOR_RETRIEVAL=false`，支持 `--vector-retrieval on/off` | 默认使用轻量 BM25，不删除已有向量索引 |
| 查询回退 | Multimodal 请求在开关关闭时回退到 BM25/Caption，并返回原因 | 用户知道实际使用了哪条路径 |
| 本地渲染 | Renderer 支持标准 Markdown 图片、alt 和派生资产路径 | 图片能够在本地 Web 页面中展示并跳转原图 |
| 幂等导入 | 重复导入不重复复制资产，内容未变时返回 `unchanged` | 适合反复验证和后续增量更新 |

### 2.3 当前使用方式

```bash
python3 app.py ingest-wiki \
  /absolute/path/to/wiki \
  --caption-package /absolute/path/to/mmwiki-package

python3 app.py wiki-status

python3 app.py query "图片中的系统架构是什么？" \
  --retrieval-mode auto
```

默认状态：

```text
VLM：off
向量检索：off
Caption 来源：MinerU
查询路径：BM25 + Caption
```

如果确实需要现有的向量或多模态能力，可以临时开启：

```bash
python3 app.py query "图片中的系统架构是什么？" \
  --retrieval-mode auto \
  --vlm on \
  --vector-retrieval on
```

## 3. 与 `nashsu/llm_wiki` 的能力对比

以下对比基于 `nashsu/llm_wiki` 公开仓库主分支的 README、图片摄入实现和相关 Skill 文档。该项目定位为完整的跨平台桌面 LLM Wiki 产品，具备多模态图片摄入、图片搜索展示、可选向量检索和较完整的知识图谱能力；其图片流程会提取文档图片，并通过视觉模型生成事实性 Caption，再将图片放入搜索和展示链路。[nashsu/llm_wiki README](https://github.com/nashsu/llm_wiki/blob/main/README.md)、[图片摄入实现](https://github.com/nashsu/llm_wiki/blob/main/src/lib/ingest.ts)

| 对比维度 | `nashsu/llm_wiki` | 本次实现 | 判断 |
|---|---|---|---|
| 产品形态 | 完整的跨平台桌面应用，包含摄入、Wiki 维护、搜索、图谱和界面 | 当前是 OpenCode Skill/CLI Demo，复用现有 Wiki Pipeline | nashsu 产品完整度更高；本方案更适合验证增量接入路线 |
| 多模态入口 | 以自身摄入流程为中心，自动提取文档内嵌图片 | 面向用户已经存在的 Markdown Wiki，新增派生导入入口 | 本方案在“已有 Wiki 不迁移”场景更直接 |
| 图片 Caption | 默认路线是调用视觉模型生成事实性 Caption，并通过图片 SHA-256 缓存避免重复 Caption | 默认复用 MinerU 已有 Caption，通过图片 SHA-256 关联；当前不自动调用 VLM Caption | nashsu 的 Caption 泛化能力更强；本方案延迟、成本和可控性更好 |
| 无 Caption 场景 | 可以通过 VLM 为图片生成语义描述 | 保留原 alt 或空 alt，标记 `caption_missing`，不自动猜测 | 本方案更保守，适合可信和低成本优先的 Demo |
| Caption 缓存 | 具备跨文档复用的图片 Caption Cache | 当前只做派生资产去重，不做 Caption Cache | nashsu 在大批量重复图片场景更成熟；本方案明确把缓存留到后续 |
| 图片检索 | 图片 Caption 进入搜索，搜索结果支持图片区域展示 | 图片 alt 进入现有 BM25；向量检索默认关闭 | 本方案改动小、成本低，但语义召回上限低于完整向量/视觉检索 |
| 向量检索 | 可选 Embedding，使用 LanceDB 等索引能力 | 复用当前已有向量索引，默认不使用、不删除；需要时显式开启 | 本方案更适合硬件和成本不确定的验证阶段 |
| 原图展示 | 具备 image-aware search、lightbox 和跳转来源等产品化体验 | 本地 Web Renderer 支持 Markdown 图片、alt、原图 HTTP 链接 | nashsu 前端体验更成熟；本方案先保证链路可用和可解释 |
| 原始资料处理 | 自身管理来源、媒体目录和 Wiki 生成流程 | 不接管原始 Wiki，不改写用户目录，只生成外部派生来源 | 本方案对既有用户数据更安全，迁移风险更低 |
| 来源追溯 | 支持来源页面、图片引用和跳转原始文档 | 保留 `source_id + source_version + item_id`，Manifest 记录 SHA、源路径、派生路径和 Caption 状态 | 本方案在本地工程调试和验收时更容易逐项核对 |
| Skill 接入 | 有独立的 `llm_wiki_skill` 与 API 工作流 | 复用当前 `multimodal-wiki` Skill，并增加 `wiki_import/wiki_status/wiki_query` 语义 | 本方案更贴合当前自研 Agent 办公平台的 Skill 化入口 |
| 工程目标 | 追求完整产品能力和持续维护 | 追求最小增量、低成本、兼容已有文本 Wiki | 二者目标不同，不能用单一指标判断谁“更好” |

### 3.1 我们不具备的能力

需要明确承认以下差距：

- 当前没有自动 VLM Caption 生成能力；
- 当前没有独立的图片语义向量库；
- 当前没有 nashsu 级别的图片搜索网格、Lightbox、细粒度原文跳转和完整桌面 UI；
- 当前只支持本地 Markdown 和本地相对图片，不支持远程图片自动下载；
- 当前没有 Caption Cache；
- 当前没有区域级 bbox、图中对象定位和像素级视觉问答；
- 当前 Demo 仍然依赖用户已有 MinerU Package 作为 Caption 来源。

因此，当前方案的竞争点不是“视觉能力更强”，而是：

> 用较小的工程代价，把多模态语义安全地叠加到已有文本 Wiki 上，同时保持原始数据、检索回退和成本边界可控。

## 4. 当前产品优势：工程角度

### 4.1 增量接入风险低

原始 Wiki 只读，系统通过派生目录承载变化：

```text
原始 Wiki ──只读──> 图片 SHA-256 / Caption 匹配 ──> 派生 Wiki
```

这样可以避免：

- 误改用户原 Markdown；
- 破坏用户已有 WikiLink；
- 因多模态失败导致文本 Wiki 无法使用；
- 用户必须重新生成全部历史 Wiki；
- 改造过程中无法回滚。

### 4.2 默认成本边界清晰

当前默认路径不调用：

- VLM；
- 文本或视觉 Embedding；
- Rerank。

Caption 由 MinerU 解析阶段提供，派生层只做本地哈希匹配、Markdown 转换和资产复制。因此 Demo 可以先验证：

1. 图片语义是否能进入搜索；
2. 图片引用和来源是否正确；
3. 前端是否能展示派生结果；
4. 用户体验是否合理。

只有验证确实需要语义向量或视觉理解时，才通过配置打开对应能力。这比一开始让每个图片都经过 VLM 更适合当前硬件和答辩 Demo。

### 4.3 复用已有文本 Wiki 流程

本次没有新增独立的 Visual Retriever、Visual DB 或对象存储服务，而是让派生 Markdown 以外部来源注册进原有状态和检索流程：

- 图片 Caption 作为 Markdown `alt`；
- `alt` 与正文共同组成 `search_text`；
- 现有 BM25 直接检索；
- 现有 Evidence、Source Version、Asset Path 继续有效；
- 现有本地 HTTP Renderer 负责页面和图片展示。

因此后续如果启用向量检索，扩展的是已有索引能力，而不是再维护一套多模态数据链路。

### 4.4 对失败有明确边界

单张图片可能出现：

- 图片不存在；
- 图片路径越界；
- 图片是远程 URL；
- 图片经过裁剪、压缩或加水印，SHA-256 不一致；
- MinerU 没有 Caption。

当前处理策略是“局部失败、全局可用”：记录错误，保留原 alt 或空 alt，不阻塞其他页面导入，也不自动猜测内容。

### 4.5 来源可解释

每个派生资产至少可回溯到：

```text
用户 Wiki 图片
    → 图片 SHA-256
    → MinerU Asset
    → MinerU Caption
    → 派生 Markdown alt
    → BM25 命中页面
```

这条链路比“模型直接看图后给出一个无法复核的答案”更适合工程验收和答辩展示。

## 5. 当前产品优势：用户角度

### 5.1 用户不用理解多模态管线

用户只需要提供：

```text
已有 Wiki 目录 + MinerU Package
```

不需要手动选择：

- OCR；
- 图片解析；
- Caption；
- Embedding；
- 向量库；
- 前端图片存储方式。

系统内部自动完成能力判断，保留一个“导入已有 Wiki”的入口。

### 5.2 不要求重建用户知识库

对于非技术用户，最难接受的不是某个检索指标下降，而是：

> “为了支持图片，请重新生成全部 Wiki。”

当前方案使用派生 Wiki，不改变用户原文件。用户可以继续使用原 Wiki，也可以查看增强后的派生版本，降低迁移心理成本和数据风险。

### 5.3 图片语义变得可搜索，但不冒充事实

例如原始图片是：

```markdown
![](images/architecture.png)
```

派生页面可以变为：

```markdown
![系统总体架构，包括接入层、服务层和数据层](../assets/asset-xxxx.png)
```

用户可以搜索“系统总体架构”，但系统仍然把 Caption 标记为检索代理，而不是把 Caption 当作原图的全部事实。这一点能降低用户对 AI 描述的误解。

### 5.4 查询速度和成本更容易预期

默认 Caption + BM25 不需要等待 VLM，也不需要等待向量服务。用户可以先使用文本 Wiki，再针对确实需要的场景开启增强能力，体验上更接近“先可用，后增强”。

### 5.5 结果更容易解释

查询结果可以同时展示：

- 命中的 Wiki 页面；
- 原始/派生图片；
- Caption；
- 来源路径；
- Source Version；
- 实际检索模式；
- 是否发生回退。

用户不仅得到“答案是什么”，也能知道“系统为什么这样回答”。

## 6. 核心竞争力定位

### 6.1 建议对外表述

不建议表述为：

> 我们实现了比业界更强的多模态理解。

建议表述为：

> 我们实现了一条面向已有文本 Wiki 的渐进式多模态接入路线：以 MinerU Caption 作为低成本语义代理，将图片能力物化为 Markdown alt，复用现有文本 Wiki 检索和 Evidence 流程，并通过 VLM/向量开关控制性能与成本。

### 6.2 核心优势组合

```text
已有 Wiki 兼容
    + 原始数据只读
    + Caption-first 低成本检索
    + VLM/向量按需开启
    + Asset SHA-256 可追溯
    + 局部失败、全局可用
    + OpenCode Skill 化入口
```

这套组合的特点是工程和产品同时成立：

- 对开发者：改动集中、复用现有流程、服务少、容易测试；
- 对产品：用户不用迁移、不必先理解模型、结果能解释、成本可控制；
- 对后续平台：可以作为自研 Agent 办公平台中的一个 Wiki 能力模块，而不是独立的视觉系统。

## 7. 当前实现与产品要求的符合性

| 要求 | 当前是否满足 | 说明 |
|---|---|---|
| 基于 OpenCode Skill/CLI | 满足 | 复用 `multimodal-wiki` Skill，增加 CLI 和导入操作 |
| 基于已有文本 Wiki 增量增加多模态 | 满足 | 派生 Wiki 注册为外部文本来源，不新增独立多模态系统 |
| 默认低成本 | 满足 | 默认 VLM 和向量检索关闭 |
| 使用 MinerU Caption | 满足 | 通过图片 SHA-256 匹配 MinerU Asset 并物化到 alt |
| 原始 Wiki 不被修改 | 满足 | 派生写入 `runtime/vault/wiki/external/` |
| 图片本地存储 | 满足 | 派生资产本地复制并在 Manifest 中记录 |
| 图片引用与前端渲染 | 满足 | 支持 Markdown 图片、Obsidian 图片和本地 HTTP Renderer |
| 视觉理解能力 | 有条件满足 | 预留现有 VLM 能力，但默认不调用，需要显式开启 |
| 向量检索 | 有条件满足 | 复用现有向量索引，默认不使用，不删除旧索引 |
| Caption Cache | 暂不满足 | 当前明确不做，后续可独立增加 |
| 远程图片/对象存储 | 暂不满足 | 当前只支持本地相对路径，降低工程复杂度 |
| 区域级视觉证据 | 暂不满足 | 当前不做 bbox 和区域前端 |

## 8. 后续优化建议

后续不建议立即复制完整多模态产品的所有能力，而应按收益和风险逐步推进：

### P0：完成 Demo 闭环

- 使用真实用户已有 Markdown Wiki 和 MinerU Package 做一次导入；
- 展示原始 Markdown 未变化；
- 展示派生 Markdown 的 Caption alt；
- 展示用 Caption 搜索到图片相关页面；
- 展示错误图片不会导致整库失败；
- 展示默认 VLM/向量调用次数为 0。

### P1：增强可观察性

- 在 `manifest.json` 增加导入时间、页面数、资产数、Caption 命中率；
- 在 `wiki-status` 中显示 `caption_ready/caption_missing` 统计；
- 在查询结果中统一显示“Caption 命中”还是“原图视觉理解”；
- 为用户提供“重新导入派生 Wiki”的明确入口。

### P2：手动 VLM 增强

- 保留现有 Caption-first 默认路径；
- 只允许用户手动选择图片触发 VLM 重识别；
- 将 VLM 结果作为新的派生 Evidence，不覆盖 MinerU Caption；
- 记录模型、时间、输入图片 SHA-256 和结果版本；
- 问答默认不隐式触发 VLM，避免不可控延迟和成本。

### P3：Caption Cache 与增量更新

- 增加基于图片 SHA-256 的 Caption Cache；
- 只处理新增或变化图片；
- 资产删除时清理孤立派生文件；
- 对用户 Wiki 重命名和目录移动增加稳定 `wiki_id` 管理。

## 9. 答辩汇报建议

### 当前任务目标

完成面向用户已有文本 Wiki 的轻量增量多模态接入验证，重点验证 Caption 关联、派生 Markdown、现有文本检索复用以及默认成本控制。

### 本次进展与产出

1. 新增 `ingest-wiki` 入口，原始 Wiki 保持只读；
2. 通过 SHA-256 关联 MinerU Caption；
3. 将 Caption 写入派生 Markdown 图片 alt；
4. 将派生页面注册进现有 BM25/Evidence 流程；
5. 增加 VLM、向量检索统一开关及安全回退；
6. Renderer 支持标准 Markdown 图片；
7. 新增配置、Caption 匹配、路径安全、幂等导入和渲染测试，当前 75 项测试通过，项目 lint 通过。

### 当前问题与下一步

当前尚未实现自动 VLM Caption、Caption Cache、远程图片、对象存储、区域级视觉证据和独立视觉向量库。下一步优先完成真实数据 Demo 闭环，再决定是否增加手动 VLM 重识别和 Caption Cache。

## 10. 参考资料

- [nashsu/llm_wiki README](https://github.com/nashsu/llm_wiki/blob/main/README.md)
- [nashsu/llm_wiki 中文 README](https://github.com/nashsu/llm_wiki/blob/main/README_CN.md)
- [nashsu/llm_wiki 图片摄入实现](https://github.com/nashsu/llm_wiki/blob/main/src/lib/ingest.ts)
- [nashsu/llm_wiki 图片 Caption Pipeline](https://github.com/nashsu/llm_wiki/blob/main/src/lib/image-caption-pipeline.ts)
- [nashsu/llm_wiki Source Ingest Cache](https://github.com/nashsu/llm_wiki/blob/main/src/lib/ingest-cache.ts)
- [nashsu/llm_wiki Skill](https://github.com/nashsu/llm_wiki_skill)
- 本项目 [README](../README.md)
- 本项目 [多模态 Wiki Skill](../.opencode/skills/multimodal-wiki/SKILL.md)
