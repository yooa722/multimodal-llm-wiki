# 待标注评测集

该目录保存已经取得问题和标准答案、但尚未完成来源与 Evidence 标注的评测数据。

- 这里的数据不得参与 Wiki 构建、索引或查询上下文。
- `expected_answer` 只用于离线评分，不能在回答生成阶段传给模型。
- `source_id`、`page_refs`、`evidence_item_ids` 和 `wiki_page_paths` 补齐前，不得作为正式检索评测集使用。
- 完成 MinerU 解析和 Wiki 构建后，先自动生成候选来源，再由人工核验后迁移到正式评测目录。
- 候选生成允许在**离线标注阶段**使用标准答案提高召回，但输出只能保留在本目录，不能进入正式查询上下文。

导入命令：

```bash
python3 tools/import_evaluation_results.py \
  /path/to/图文回答_Results.xlsx \
  --output evaluation/pending/official_image_text_50.jsonl \
  --summary-output evaluation/pending/official_image_text_50_baseline.json
```

生成待人工核验的 Wiki、页码、段落与图片 Evidence 候选：

```bash
python3 tools/map_evaluation_candidates.py \
  evaluation/pending/official_image_text_50.jsonl \
  --runtime-root runtime/official-image-text/wiki-runtime \
  --output evaluation/pending/official_image_text_50_candidates.jsonl \
  --summary-output evaluation/pending/official_image_text_50_candidates_summary.json
```
