# 新电脑使用说明

项目使用已经构建好的 10 来源新数据 Wiki。配置环境和 API Key 后，可以直接在 OpenCode 中提问，不需要重新构建这批数据。

## 1. 安装环境

需要提前安装：

- Python 3.11 或更高版本
- OpenCode Desktop
- 可用的百炼 API Key

从 GitHub 下载 ZIP 并解压，或克隆仓库。打开终端进入项目根目录，以下示例假设目录名为 `multimodal-llm-wiki`：

```bash
cd ~/Desktop/multimodal-llm-wiki
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
```

Windows 激活环境时使用：

```powershell
.\.venv\Scripts\Activate.ps1
```

## 2. 配置 API

用文本编辑器打开项目根目录下的 `.env`，填写：

```dotenv
MMWIKI_API_BASE_URL=你的百炼接口地址
MMWIKI_API_KEY=你的API-Key
MMWIKI_ENABLE_VLM=true
MMWIKI_ENABLE_VECTOR_RETRIEVAL=true
MMWIKI_RUNTIME_ROOT=runtime/official-image-text/wiki-runtime
```

其余配置保持默认。录屏时不要展示 API Key。

配置完成后，在终端检查 Wiki：

```bash
source .venv/bin/activate
python3 app.py lint
```

看到 `status: passed`、`sources: 10` 和 `pages: 38`，说明当前新数据 Wiki 可以正常读取。

## 3. 在 OpenCode 中打开

1. 启动 OpenCode Desktop。
2. 选择“打开文件夹”。
3. 选择整个 `multimodal-llm-wiki-portable` 文件夹。
4. 等待 OpenCode 自动安装项目工具依赖。
5. 输入 `/connect`，选择 `bailian`，填写 API Key。
6. 完全退出 OpenCode，再重新打开当前项目。

必须打开项目根目录，不要只打开 `runtime/` 或它的上级目录。

## 4. 开始使用

依次输入：

```text
/wiki-check
/wiki-demo
```

表格问题：

```text
/wiki-ask ERP方案第18页中，销售管理参数 S001、S004、S007 的参数名称、参数值和取值范围分别是什么？
```

图片问题：

```text
/wiki-ask 请观察厚叶卷瓣兰第2页的原图，说明 A、B、C、D 四个分图分别展示什么，并概括花朵的颜色和斑点特征。
```

正常回答应包含结论、Wiki 链接、Evidence ID，以及对应的原图或完整表格。

## 5. 常见问题

| 问题 | 处理方法 |
|---|---|
| 看不到 `/wiki-*` | 确认打开的是项目根目录，然后完全重启 OpenCode |
| 提示模型未配置 | 检查 `.env`，并重新执行 `/connect` |
| Wiki 链接或图片打不开 | 先再次执行 `/wiki-check` 触发自动启动；仍失败时查看 `.opencode/wiki-server.log` |

完成 `/wiki-check` 后即可正常演示，无需重新 Ingest 当前 10 个来源。

需要加入新 PDF 时，再使用仓库中的 MinerU 云入口 `tools/mineru_cloud_parse.py`。这一步会把原始文件发送到 MinerU，必须由数据拥有者配置自己的 `MINERU_API_TOKEN` 并确认允许外发；现有 10 来源演示不需要执行该步骤。
