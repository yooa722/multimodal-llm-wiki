import { tool } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import http from "node:http"
import path from "path"


async function runPython(
  worktree: string,
  args: string[],
): Promise<string> {
  const script = path.join(worktree, "tools", "opencode_demo.py")
  return new Promise((resolve, reject) => {
    const child = spawn("python3", [script, ...args], {
      cwd: worktree,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk
    })
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk
    })
    child.on("error", (error) => {
      reject(error)
    })
    child.on("close", (exitCode) => {
      if (exitCode !== 0) {
        reject(
          new Error(
            stderr.trim() || `multimodal Wiki command exited with ${exitCode}`,
          ),
        )
        return
      }
      resolve(stdout.trim())
    })
  })
}


async function wikiServerReady(): Promise<boolean> {
  return new Promise((resolve) => {
    const request = http.get("http://127.0.0.1:19828/api/v1/health", (response) => {
      response.resume()
      resolve(response.statusCode === 200 || response.statusCode === 503)
    })
    request.setTimeout(500, () => {
      request.destroy()
      resolve(false)
    })
    request.on("error", () => resolve(false))
  })
}


async function ensureWikiServer(worktree: string): Promise<void> {
  if (await wikiServerReady()) return
  const child = spawn(
    "python3",
    ["app.py", "api", "--host", "127.0.0.1", "--port", "19828"],
    {
      cwd: worktree,
      detached: true,
      shell: false,
      stdio: "ignore",
    },
  )
  child.unref()
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100))
    if (await wikiServerReady()) return
  }
  throw new Error("无法启动本地 Wiki 展示服务 127.0.0.1:19828")
}


export const start = tool({
  description: "显示多模态 LLM Wiki 的中文零基础入口、当前数据规模和推荐命令。用户说看不懂 OpenCode 或第一次演示时调用。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    return runPython(context.worktree, ["start"])
  },
})


export const status = tool({
  description: "只读检查 Wiki 页面级索引、文本/视觉 Evidence 索引、模型、OpenCode Desktop 和本地展示服务状态。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    return runPython(context.worktree, ["status"])
  },
})


export const tour = tool({
  description: "生成多模态 Wiki 的完整中文导览，展示构建路线、查询路线、真实完整表格、Figure 4 原图和 Evidence ID。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    return runPython(context.worktree, ["tour"])
  },
})


export const compare = tool({
  description: "读取项目已有评测，比较文本 LLM Wiki 基线和多模态增量的向量复用、检索、问答与延迟指标。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    return runPython(context.worktree, ["compare"])
  },
})


export const query = tool({
  description: "按 Wiki 页面定位→原始 Evidence 回读的顺序安全查询，返回已完成排版的最终 Markdown。调用方必须逐字展示返回值，不得概括、改写、删链接或删原图。不要用 Bash 拼接用户问题。",
  args: {
    question: tool.schema
      .string()
      .min(1)
      .max(4000)
      .describe("用户的自然语言问题；仅作为数据传给 Python，不作为 Shell 命令"),
    mode: tool.schema
      .enum(["auto", "lexical", "hybrid", "multimodal"])
      .default("auto")
      .describe("普通问题用 hybrid；颜色、布局、箭头、曲线或图片细节用 multimodal"),
    provider: tool.schema
      .enum(["api", "baseline"])
      .default("api")
      .describe("现场问答用 api；baseline 只用于离线诊断"),
  },
  async execute(args, context) {
    await ensureWikiServer(context.worktree)
    return runPython(context.worktree, [
      "live",
      "--question",
      args.question,
      "--mode",
      args.mode,
      "--provider",
      args.provider,
    ])
  },
})
