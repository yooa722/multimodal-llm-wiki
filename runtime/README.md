# 可迁移演示 Runtime

当前活动知识库位于 `runtime/official-image-text/wiki-runtime/`，由 10 份新数据 Source Package 构建。项目通过 `MMWIKI_RUNTIME_ROOT` 让 OpenCode、CLI 和本地 HTTP 服务读取同一 Runtime。GitHub 可迁移快照只保留这 10 个来源，原有 5 来源 Runtime 不再发布。

## 快照内容

| 内容 | 数量或位置 |
|---|---|
| 当前来源 | 10 个 |
| Wiki 页面 | 38 个稳定知识页 |
| 文本 Evidence 索引 | 4,473 条 |
| 视觉 Evidence 索引 | 73 条 |
| 活动目录 | `official-image-text/wiki-runtime/` |
| 来源副本 | `official-image-text/wiki-runtime/raw/<source>/<version>/` |
| Page Index 与检索索引 | `page-index.json`、`retrieval-index.json` |

## 有意不上传的内容

- `.env`、API Key 和 OpenCode 个人凭据；
- `queries.jsonl`、`curation-log.jsonl`、批处理日志和 `state.json` 中的本机查询历史；
- `.obsidian/`、编辑器工作区和本机界面状态；
- 不属于当前 10 个来源的历史来源副本；
- 临时文件和本地服务进程状态。

发布的 `state.json` 已清空查询历史，但完整保留来源、知识页和 Evidence 状态。这不影响 Wiki 页面、原图、Page Index 和检索索引的迁移。真实问答仍会调用配置的模型服务，因此新电脑必须使用自己的授权凭据。

完整迁移步骤见仓库根目录的 [OPENCODE_NEW_COMPUTER_GUIDE.md](../OPENCODE_NEW_COMPUTER_GUIDE.md)。
