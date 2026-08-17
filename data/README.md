# Wiki 构建数据

本目录保存当前项目用于构建和评测多模态 LLM Wiki 的 5 份 `mmwiki-0.1` Source Package。它们是文档解析模块的标准化输出，不包含 API Key、查询日志、向量索引或生成后的 Wiki 页面。

## 数据清单

| Package ID | Source Version | Item | Chunk | Asset | 主要模态 |
|---|---|---:|---:|---:|---|
| `中文_006_chinese_mixed` | `52790e363edc` | 13 | 12 | 1 | 中文文本、表格 |
| `报纸_001_the_global_herald` | `cc7688b9b6f5` | 42 | 41 | 2 | 文本、表格 |
| `杂志_001_tech_frontier` | `4fa795b8a87f` | 34 | 21 | 1 | 文本、表格 |
| `表格_018_irs_7004_extension_corp` | `3cffac6e9f35` | 5 | 1 | 1 | 复杂表格、表格截图 |
| `论文_002_cs_LG` | `1d9dabf3e92b` | 118 | 107 | 23 | 论文文本、表格、公式、图片、图表 |
| **合计** | — | **212** | **182** | **28** | 文本 + 表格 + 公式 + 图片 |

机器可读的版本、校验和与规模信息见 [index.json](index.json)。

## 目录结构

```text
data/source_packages/<package-id>/<source-version>/
├── manifest.json
├── items.jsonl
├── chunks.jsonl
├── assets.json
├── assets/
└── raw/
```

`source-version` 是完整 Package 内容校验和的前 12 位。构建后，管线会把同版本数据复制到 `runtime/raw/` 作为不可变运行副本。

## 验证与构建

验证单个 Package：

```bash
python3 app.py validate \
  data/source_packages/论文_002_cs_LG/1d9dabf3e92b
```

先构建文本 Wiki，再增量加入多模态能力：

```bash
PACKAGE=data/source_packages/论文_002_cs_LG/1d9dabf3e92b

python3 app.py ingest "$PACKAGE" --provider api --stage text
python3 app.py build-index --text-only

python3 app.py ingest "$PACKAGE" --provider api --stage multimodal
python3 app.py build-index --source-id 论文_002_cs_LG
```

同一 Package 和同一版本重复摄入应返回 `unchanged`。不要直接修改已经摄入到 `runtime/raw/` 的副本；如需更新数据，应生成新的 Source Package 和新的版本校验和。

## 数据边界

- 本目录用于项目内部研究、复现实验和私有仓库协作。
- 原始 PDF 未包含在 Source Package 中；仓库保存结构化 Item、Chunk、解析参考输出和关联视觉资产。
- 使用或再分发数据前，应确认原始资料对应的授权和许可范围。
- 合同、简历和财报等已从当前活跃 Wiki 排除的 Raw 数据不在本目录中。
