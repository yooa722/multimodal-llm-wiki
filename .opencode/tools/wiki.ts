import { tool } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import http from "node:http"
import path from "path"


const WIKI_SERVER_URL = "http://127.0.0.1:19828"
const PRESENTATION_VERSION = "split-query-v1"


function markdownResult(
  context: { metadata(input: { title?: string; metadata?: Record<string, unknown> }): void },
  title: string,
  output: string,
  metadata: Record<string, unknown> = {},
) {
  context.metadata({ title, metadata })
  return { title, output, metadata }
}


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


async function runCli(
  worktree: string,
  args: string[],
): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn("python3", ["app.py", ...args], {
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
    child.on("error", reject)
    child.on("close", (exitCode) => {
      if (exitCode !== 0) {
        reject(new Error(stderr.trim() || `multimodal Wiki command exited with ${exitCode}`))
        return
      }
      resolve(stdout.trim())
    })
  })
}


async function wikiServerStatus(
  worktree: string,
): Promise<"ready" | "outdated" | "offline"> {
  return new Promise((resolve) => {
    const request = http.get(`${WIKI_SERVER_URL}/api/v1/health`, (response) => {
      let body = ""
      response.setEncoding("utf8")
      response.on("data", (chunk: string) => {
        body += chunk
      })
      response.on("end", () => {
        if (response.statusCode !== 200 && response.statusCode !== 503) {
          resolve("offline")
          return
        }
        try {
          const value = JSON.parse(body) as {
            presentation_version?: string
            project_root?: string
          }
          const currentProject = value.project_root
            ? path.resolve(value.project_root)
            : ""
          resolve(
            value.presentation_version === PRESENTATION_VERSION &&
              currentProject === path.resolve(worktree)
              ? "ready"
              : "outdated",
          )
        } catch {
          resolve("outdated")
        }
      })
    })
    request.setTimeout(500, () => {
      request.destroy()
      resolve("offline")
    })
    request.on("error", () => resolve("offline"))
  })
}


async function capture(command: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
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
    child.on("error", reject)
    child.on("close", (exitCode) => {
      if (exitCode !== 0 && !stdout.trim()) {
        reject(new Error(stderr.trim() || `${command} exited with ${exitCode}`))
        return
      }
      resolve(stdout.trim())
    })
  })
}


async function stopOutdatedWikiServer(worktree: string): Promise<void> {
  let listeners = ""
  try {
    listeners = await capture("lsof", ["-tiTCP:19828", "-sTCP:LISTEN"])
  } catch {
    throw new Error("检测到旧版 Wiki 展示服务，但无法定位其进程。请退出旧服务后重试。")
  }
  const pids = listeners
    .split(/\s+/)
    .map((value) => Number.parseInt(value, 10))
    .filter((value) => Number.isInteger(value) && value > 0)
  let stopped = false
  for (const pid of pids) {
    let cwd = ""
    try {
      const details = await capture("lsof", ["-a", "-p", String(pid), "-d", "cwd", "-Fn"])
      const cwdLine = details.split("\n").find((line) => line.startsWith("n"))
      cwd = cwdLine ? cwdLine.slice(1) : ""
    } catch {
      continue
    }
    if (!cwd || path.resolve(cwd) !== path.resolve(worktree)) continue
    try {
      process.kill(pid, "SIGTERM")
      stopped = true
    } catch (error) {
      throw new Error(`无法结束本项目的旧版 Wiki 展示服务（PID ${pid}）：${String(error)}`)
    }
  }
  if (!stopped) {
    throw new Error("端口 19828 被其他目录的服务占用，已停止自动替换以保护其他项目。")
  }
  for (let attempt = 0; attempt < 30; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100))
    if (await wikiServerStatus(worktree) === "offline") return
  }
  throw new Error("本项目的旧版 Wiki 展示服务未能正常退出。")
}


async function ensureWikiServer(worktree: string): Promise<void> {
  const initialStatus = await wikiServerStatus(worktree)
  if (initialStatus === "ready") return
  if (initialStatus === "outdated") {
    await stopOutdatedWikiServer(worktree)
  }
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
    if (await wikiServerStatus(worktree) === "ready") return
  }
  throw new Error("无法启动本地 Wiki 展示服务 127.0.0.1:19828")
}


export const start = tool({
  description: "显示多模态 LLM Wiki 的中文零基础入口、当前数据规模和推荐命令。用户说看不懂 OpenCode 或第一次演示时调用。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    const output = await runPython(context.worktree, ["start"])
    return markdownResult(context, "Wiki 使用入口", output)
  },
})


export const status = tool({
  description: "只读检查 Wiki 页面级索引、文本/视觉 Evidence 索引、模型、OpenCode Desktop 和本地展示服务状态。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    const output = await runPython(context.worktree, ["status"])
    return markdownResult(context, "Wiki 演示就绪检查", output)
  },
})


const importWiki = tool({
  description: "只读导入用户已有 Markdown Wiki，使用 MinerU Caption 生成派生图片 alt 并接入现有文本 Wiki 检索。",
  args: {
    wiki_root: tool.schema
      .string()
      .min(1)
      .describe("用户已有 Wiki 的本地目录绝对路径"),
    caption_package: tool.schema
      .string()
      .min(1)
      .describe("MinerU mmwiki-0.1 Source Package 的本地目录绝对路径"),
  },
  async execute(args, context) {
    const output = await runCli(context.worktree, [
      "ingest-wiki",
      args.wiki_root,
      "--caption-package",
      args.caption_package,
    ])
    return markdownResult(context, "已有 Wiki 导入结果", output, {
      wiki_root: args.wiki_root,
    })
  },
})


export { importWiki as import }


export const tour = tool({
  description: "生成多模态 Wiki 的完整中文导览，展示构建路线、查询路线、真实完整表格、Figure 4 原图和 Evidence ID。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    const output = await runPython(context.worktree, ["tour"])
    return markdownResult(context, "多模态 Wiki 导览", output)
  },
})


export const compare = tool({
  description: "读取项目已有评测，比较文本 LLM Wiki 基线和多模态增量的向量复用、检索、问答与延迟指标。",
  args: {},
  async execute(_args, context) {
    await ensureWikiServer(context.worktree)
    const output = await runPython(context.worktree, ["compare"])
    return markdownResult(context, "Wiki 效果与成本对比", output)
  },
})


export const questions = tool({
  description: "显示推荐的表格、图片、拒答和自由问答演示问题。",
  args: {},
  async execute(_args, context) {
    const output = await runPython(context.worktree, ["questions"])
    return markdownResult(context, "Wiki 推荐演示问题", output)
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
      .describe("默认按配置自动选择；默认是 BM25 + MinerU Caption，只有显式开启开关才使用向量或多模态检索"),
    provider: tool.schema
      .enum(["api", "baseline"])
      .default("api")
      .describe("现场问答用 api；baseline 只用于离线诊断"),
  },
  async execute(args, context) {
    await ensureWikiServer(context.worktree)
    const output = await runPython(context.worktree, [
      "live",
      "--question",
      args.question,
      "--mode",
      args.mode,
      "--provider",
      args.provider,
    ])
    return markdownResult(context, "Wiki 完整回答", output, {
      requested_mode: args.mode,
      provider: args.provider,
    })
  },
})
