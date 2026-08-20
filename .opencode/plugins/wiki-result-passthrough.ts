import type { Plugin } from "@opencode-ai/plugin"


const WIKI_TOOL_PREFIX = "wiki_"


/**
 * Make the final OpenCode answer byte-for-byte identical to the Markdown
 * returned by a typed Wiki tool.
 *
 * OpenCode normally asks the model for another text turn after a tool call.
 * Even with temperature 0, that extra turn can summarize the answer or alter
 * a link, formula, Evidence ID, or model name. The Wiki backend has already
 * produced the final source-grounded Markdown, so the display layer replaces
 * generated text with the original tool output when text generation completes.
 */
export const WikiResultPassthrough: Plugin = async () => {
  const pendingBySession = new Map<string, string>()

  return {
    "tool.execute.after": async (input, output) => {
      if (!input.tool.startsWith(WIKI_TOOL_PREFIX)) return
      pendingBySession.set(input.sessionID, output.output)
    },

    "experimental.text.complete": async (input, output) => {
      const wikiOutput = pendingBySession.get(input.sessionID)
      if (wikiOutput === undefined) return

      output.text = wikiOutput
      pendingBySession.delete(input.sessionID)
    },
  }
}
