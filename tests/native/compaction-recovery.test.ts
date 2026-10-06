// SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
// SPDX-License-Identifier: MIT

// Installed only in a disposable pristine 4ee426ba checkout. No native source patch.
// Provider output is fake; HTTP, prompt, abort, compaction, tools and structured capture are native.
import { afterEach, expect, spyOn, test } from "bun:test"
import type { LanguageModelV2StreamPart } from "@ai-sdk/provider"
import path from "node:path"
import { Server } from "../../src/server/server"
import { LLM } from "../../src/session/llm"
import { Provider } from "../../src/provider/provider"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"

const original = LLM.stream
const spies: { mockRestore(): void }[] = []
afterEach(() => spies.splice(0).forEach((spy) => spy.mockRestore()))

for (const [count, streaming] of [[1, false], [1, true], [2, true], [3, true]] as const) {
  test(`stock HTTP recovery ${count}, streaming=${streaming}`, async () => {
    await using tmp = await tmpdir({
      config: {
        model: "fixture/research",
        provider: { fixture: { npm: "@ai-sdk/openai-compatible", options: { apiKey: "local-fixture" },
          models: { research: { name: "Fixture", limit: { context: 100_000, output: 10_000 }, tool_call: true } } } },
        agent: { audit: { mode: "primary", permission: { "*": "deny", read: "allow", StructuredOutput: "allow" } } },
      },
      init: (dir) => Bun.write(path.join(dir, "evidence.txt"), "Local evidence.\n"),
    })
    let current: LLM.StreamInput
    let summaries = 0
    let captured = 0
    let aborts = 0
    let active = 0
    let posts = 0
    const trace: unknown[] = []
    const observer = spyOn(LLM, "stream").mockImplementation(async (input) => {
      current = input
      return original(input)
    })
    const language = spyOn(Provider, "getLanguage").mockResolvedValue({
      specificationVersion: "v2", provider: "fixture", modelId: "research", supportedUrls: {},
      doGenerate: async () => { throw new Error("Unexpected generation") },
      doStream: async (options) => {
        const compacting = current.agent.name === "compaction"
        const structured = current.user.format?.type === "json_schema" && !compacting
        expect(options.tools?.some((x) => x.type === "function" && x.name === "StructuredOutput") ?? false).toBe(structured)
        if (structured) expect(options.toolChoice).toEqual({ type: "required" })
        const research = structured && summaries < count
        const chunks: LanguageModelV2StreamPart[] = [{ type: "stream-start", warnings: [] }]
        if (compacting || !structured) {
          chunks.push({ type: "text-start", id: "text" },
            { type: "text-delta", id: "text", delta: compacting ? "Summary: evidence read. Continue the task." : "Discarded text." })
          if (compacting) summaries++
        } else {
          const name = research ? "read" : "StructuredOutput"
          const input = JSON.stringify(research ? { filePath: path.join(tmp.path, "evidence.txt") } : { ok: true })
          const id = `call-${summaries}`
          chunks.push({ type: "tool-input-start", id, toolName: name },
            { type: "tool-input-delta", id, delta: input }, { type: "tool-input-end", id },
            { type: "tool-call", toolCallId: id, toolName: name, input })
          if (!research) captured++
        }
        const hang = !compacting && !structured && streaming
        if (!hang) {
          if (compacting || !structured) chunks.push({ type: "text-end", id: "text" })
          chunks.push({ type: "finish", finishReason: structured ? "tool-calls" : "stop",
            usage: { inputTokens: research ? 91_000 : 100, outputTokens: 20, totalTokens: research ? 91_020 : 120 } })
        }
        return { stream: new ReadableStream({ start(controller) {
          chunks.forEach((chunk) => controller.enqueue(chunk))
          if (!hang) controller.close()
          else options.abortSignal?.addEventListener("abort", () => {
            trace.push({ event: "provider_aborted", time: Date.now() })
            controller.error(new DOMException("Fixture cancelled", "AbortError"))
          }, { once: true })
        } }) }
      },
    })
    const app = Server.createApp({})
    spies.push(observer, language)
    const server = Bun.serve({ hostname: "127.0.0.1", port: 0, idleTimeout: 30,
      async fetch(request) {
        const url = new URL(request.url)
        url.searchParams.set("directory", tmp.path)
        const prompt = request.method === "POST" && url.pathname.endsWith("/message")
        const abort = url.pathname.endsWith("/abort")
        if (prompt) { expect(active).toBe(0); active++; posts++; trace.push({ event: "post", time: Date.now() }) }
        const response = await app.fetch(new Request(url, request))
        if (abort) {
          aborts++
          const status = new URL(url)
          status.pathname = "/session/status"
          const idle = await (await app.fetch(new Request(status))).json()
          trace.push({ event: "abort_returned", time: Date.now(), status: idle })
          expect(Object.values(idle).every((value: any) => value.type === "idle")).toBe(true)
        }
        if (!prompt) return response
        // Retain native bytes, delay only transport completion after native abort.
        const body = await response.arrayBuffer()
        if (streaming && posts <= count) await Bun.sleep(200)
        active--
        trace.push({ event: "post_completed", time: Date.now() })
        return new Response(body, { status: response.status, headers: response.headers })
      },
    })
    const output = path.join(process.env.NATIVE_COMPACTION_ARTIFACT_DIR!, `http-${count}-${streaming}`)
    try {
      const child = Bun.spawn([process.env.NATIVE_COMPACTION_PYTHON!, "-B",
        process.env.NATIVE_COMPACTION_CLIENT!, String(server.port), output, String(count)],
        { stdout: "pipe", stderr: "pipe" })
      const [code, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()])
      expect(stderr).toBe("")
      expect(code).toBe(0)
      expect(posts).toBe(Math.min(count, 2) + 1)
      expect(captured).toBe(count <= 2 ? 1 : 0)
      if (streaming) expect(aborts).toBeGreaterThanOrEqual(Math.min(count, 2))
      await Bun.write(path.join(output, "native-trace.json"), JSON.stringify({ trace, posts, aborts, captured }))
    } finally {
      server.stop(true)
      observer.mockRestore(); language.mockRestore()
      await Instance.provide({ directory: tmp.path, fn: () => Instance.dispose() })
    }
  })
}
