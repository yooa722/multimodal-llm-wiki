# OpenCode × 多模态 LLM Wiki：快速开始

> 使用 OpenCode Desktop 打开仓库根目录，然后输入 `/wiki-start`。OpenCode 是操作台，Wiki 页面和原始多模态 Evidence 才是知识本体。

## 第一次使用

1. 按 README 完成 Python 依赖和 `.env` 配置。
2. 使用 OpenCode Desktop 打开克隆后的仓库根目录，不要打开它的上级目录。
3. 首次使用时输入 `/connect`，为项目配置中的 `bailian` Provider 添加个人 API Key；不要把 Key 写进仓库文件。
4. 完全退出并重新打开 OpenCode Desktop，使项目级 Skill、类型化工具、专用 Agent 和结果透传插件加载。
5. 在对话框输入 `/`，确认能够看到 `wiki-start`、`wiki-check`、`wiki-demo` 和 `wiki-ask`。
6. 依次输入：

   ```text
   /wiki-start
   /wiki-check
   /wiki-demo
   ```

如果尚未摄入 Source Package，`/wiki-check` 会提示 Wiki 数据或索引未就绪；这不是 OpenCode 安装失败，需要先按 README 完成文本阶段和多模态阶段构建。

完整图文演示需要在本机 `.env` 打开增强开关：

```dotenv
MMWIKI_ENABLE_VLM=true
MMWIKI_ENABLE_VECTOR_RETRIEVAL=true
```

开启后，`auto` 对普通问题使用 Hybrid，对明确的图片问题使用 Multimodal；关闭时沿用低成本的 Page BM25、Evidence BM25 与 MinerU Caption 路径。

## 常用命令

| 命令 | 用途 |
|---|---|
| `/wiki-start` | 解释 OpenCode、Wiki 页面和 Evidence 的分工 |
| `/wiki-check` | 检查 Wiki、模型、索引和本地展示服务 |
| `/wiki-demo` | 查看构建路线、查询路线和原始多模态 Evidence |
| `/wiki-compare` | 查看文本 Wiki 基线与多模态增量的效果和成本 |
| `/wiki-questions` | 查看适合现场演示的问题清单 |
| `/wiki-table` / `/wiki-image` | 分别演示完整表格和原图问答 |
| `/wiki-refuse` | 演示证据不足时拒答 |
| `/wiki-ask <问题>` | 以 Auto 查询；默认 BM25 + Caption，增强能力按开关启用 |

## 如何提问

普通文字、事实和表格语义问题直接提问：

```text
/wiki-ask 开发测试阶段的工期、人力和预算分别是多少？
```

需要读取图片颜色、布局、箭头或曲线时，在问题中明确说明：

```text
/wiki-ask 请结合 Figure 4 原图，按顺序解释箭头表示的数据流，并给出 Evidence ID。
```

系统会优先定位 Wiki 页面，再检索和回读原始 Evidence。自由问答由专用 `wiki-query-presenter` 调用 `wiki_query`，其余固定演示命令由 `wiki-presenter` 选择对应工具；两个 Agent 都不负责重新生成事实。`wiki-result-passthrough` 插件在最终显示阶段直接采用工具返回的 Markdown，防止模型复述时改动链接、表格、公式、Evidence ID 或模型名。正式问答固定展示“结论—知识入口—证据依据—运行信息”：正文 `〔1〕` 直接对应下方 `〔1〕` 证据卡片，图片卡片先显示原图，再分别展示 VLM 理解、OCR 文字和 MinerU 原始 Caption。

斜杠命令本质上是 OpenCode 的提示词模板，因此命令正文会作为一条用户消息出现。项目中的全部 `/wiki-*` 命令正文只保留用户可读的自然语言；工具名、`mode` 和 `provider` 等路由规则写在 Presenter Agent 配置中，不使用会出现在消息气泡里的 HTML 注释。如果仍看到旧的 `mmwiki-action`、`wiki_query`、`mode` 或 `provider`，说明正在查看历史消息或 Desktop 尚未重新加载 `.opencode`：请完全退出应用、重新打开仓库并重新执行命令。

## 蓝色链接或原图无法打开

项目使用仅监听本机的 `http://127.0.0.1:19828` 服务展示 Wiki 页面和原图。类型化工具会按需启动服务，也可以手动运行：

```bash
python3 app.py api --host 127.0.0.1 --port 19828
```

然后重新执行 `/wiki-check`。如果刚更新过 `.opencode` 下的工具或命令，请完全退出并重开 OpenCode Desktop。

## 回答中的公式显示异常

项目会把行内公式转换为 OpenCode 支持的 `\(...\)`，把独立公式规范化为单独成行的 `$$`，并修复模型 JSON 中未正确转义的 LaTeX 反斜杠。旧回答不会自动刷新，重新执行原问题即可。

## 使用边界

- 不要把 Caption 当作原图，也不要把线性化文本当作完整表格。
- Evidence 不足时应拒答，不应根据常识补猜。
- 查询结果不会自动写入稳定 Wiki 页面。
- `.env`、API Key、运行数据和原始来源文档不得提交到 Git。
