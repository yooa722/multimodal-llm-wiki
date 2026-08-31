# Wiki 构建数据

本目录保存与当前 OpenCode 演示 Runtime 对齐的 10 份 `mmwiki-0.1` Source Package。它们是 MinerU 解析结果标准化后的输入，包含文本、表格、公式、图片和页码定位信息，不包含 API Key、查询日志、向量索引或生成后的 Wiki 页面。

## 数据清单

| Package ID | Source Version | Item | Chunk | Asset |
|---|---|---:|---:|---:|
| `104页-ERP财务供应链解决方案` | `386617a95792` | 784 | 665 | 158 |
| `110页-供应链解决方案` | `2131a4bf9628` | 815 | 815 | 35 |
| `20230507-小红书-服饰潮流行业-好产品赢战618` | `7943c2dd3155` | 666 | 517 | 129 |
| `2023年618厨卫刚需品类市场总结-烟-灶-热--12页` | `d655322463bb` | 110 | 78 | 43 |
| `23年开年冰洗小结及五一-618预测-8页` | `4df2a4272bb5` | 56 | 40 | 21 |
| `3-类典型株型草本植物对沙面风蚀抑制作用的研究` | `f6bdeb42a3b8` | 152 | 110 | 12 |
| `618新生活购物趋势洞察报告-37页` | `e92cbcfca65e` | 741 | 649 | 119 |
| `厚叶卷瓣兰_中国兰科一新记录种` | `f375999282d8` | 52 | 45 | 1 |
| `服饰潮流行业-闭环全攻略` | `e471f79c739e` | 1,055 | 962 | 407 |
| `果集数据-抖音618好物节电商报告-62页` | `7204bf81d8da` | 690 | 495 | 157 |
| **合计** | — | **5,121** | **4,376** | **1,082** |

机器可读的版本、完整校验和、模态类型和规模信息见 [index.json](index.json)。这 10 份来源也是当前发布 Runtime 中实际编译为稳定 Wiki 页面并用于 OpenCode 问答的来源。

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

`source-version` 是完整 Package 内容校验和的前 12 位。构建后，管线会把同版本数据复制到 `runtime/raw/`，作为不可变来源副本。

## 验证与构建

验证单个 Package：

```bash
python3 app.py validate \
  'data/source_packages/厚叶卷瓣兰_中国兰科一新记录种/f375999282d8'
```

从 Source Package 构建文本 Wiki，再增量加入多模态 Evidence：

```bash
PACKAGE='data/source_packages/厚叶卷瓣兰_中国兰科一新记录种/f375999282d8'

python3 app.py ingest "$PACKAGE" --provider api --stage text
python3 app.py build-index --text-only --vector-retrieval on

python3 app.py ingest "$PACKAGE" --provider api --stage multimodal --vlm on
python3 app.py build-index --source-id '厚叶卷瓣兰_中国兰科一新记录种' \
  --vlm on --vector-retrieval on
```

`.env.example` 默认关闭高成本模型开关，避免无意外发数据或产生调用费用。上述命令显式打开 OCR/VLM Caption、文本向量和视觉向量链路；只验证低成本文本 Wiki 时，可省略多模态阶段。

同一 Package 和同一版本重复摄入应返回 `unchanged`。不要修改已经摄入到 `runtime/raw/` 的副本；数据发生变化时，应生成新的 Source Package 和新的版本校验和。

## 数据边界

- `data/source_packages/` 是可重新构建 Wiki 的标准输入；`runtime/official-image-text/wiki-runtime/` 是当前已构建、可直接演示的运行结果。
- 原始 PDF 未包含在 Source Package 中；仓库保存结构化 Item、Chunk、解析参考输出和关联视觉资产。
- 本目录不包含任何 API Key。
- 使用或再分发数据前，应确认原始资料的授权和许可范围。
