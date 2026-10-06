// SPDX-FileCopyrightText: Copyright (c) 2026 Alexey Sedoykin
// SPDX-License-Identifier: MIT

// Installed into the pinned OpenCode package by scripts/test-native-compaction.py.
// Only the provider is fake: prompt, compaction, tools, persistence and capture are native.
import { afterEach, expect, spyOn, test } from "bun:test"
import { asSchema } from "ai"
import type { LanguageModelV2StreamPart } from "@ai-sdk/provider"
import path from "node:path"
import { Session } from "../../src/session"
import { SessionPrompt } from "../../src/session/prompt"
import { LLM } from "../../src/session/llm"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID } from "../../src/session/schema"
import { Provider } from "../../src/provider/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"

const baseline = process.env.NATIVE_COMPACTION_BASELINE === "1"
const output = process.env.NATIVE_COMPACTION_ARTIFACT_DIR!
const stream = LLM.stream
const save = Session.updateMessage
const spies: { mockRestore(): void }[] = []
afterEach(() => spies.splice(0).forEach((spy) => spy.mockRestore()))

function contract(label: string): MessageV2.OutputFormat {
  return {
    type: "json_schema",
    retryCount: 0,
    schema: {
      $schema: "http://json-schema.org/draft-07/schema#",
      type: "object",
      properties: {
        label: { type: "string", enum: [label] },
        evidence: {
          type: "object",
          properties: { files: { type: "array", items: { type: "string" }, minItems: 1 } },
          required: ["files"],
          additionalProperties: false,
        },
      },
      required: ["label", "evidence"],
      additionalProperties: false,
    },
  }
}

async function run(count: number, threshold = false, text: false | "explicit" | "implicit" = false) {
  const model = { providerID: ProviderID.make("fixture"), modelID: ModelID.make("research") }
  await using tmp = await tmpdir({
    config: {
      model: "fixture/research",
      provider: {
        fixture: {
          npm: "@ai-sdk/openai-compatible",
          models: { research: { name: "Fixture", limit: { context: 100_000, output: 10_000 }, tool_call: true } },
          options: { apiKey: "local-fixture" },
        },
      },
      agent: { audit: { mode: "primary", permission: { "*": "deny", read: "allow", StructuredOutput: "allow" } } },
    },
    init: (dir) => Bun.write(path.join(dir, "evidence.txt"), "Native read tool evidence.\n"),
  })

  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      // Distinct contracts share one runtime instance; text tests use the same process.
      for (const label of text ? ["plain"] : ["first", "second"]) {
        const format = text === "implicit" ? undefined : text ? ({ type: "text" } as const) : contract(label)
        const answer = { label, evidence: { files: ["evidence.txt"] } }
        const session = await Session.create({ title: "Native compaction regression" })
        const body = {
          messageID: MessageID.ascending(), sessionID: session.id, agent: "audit", model, format,
          parts: [{ type: "text" as const, text: "Read evidence.txt and report the evidence." }],
        }
        const snapshots: MessageV2.WithParts[][] = []
        const first = new Map<string, MessageV2.Info>()
        const providers: { agent: string; structured: boolean; choice: unknown }[] = []
        let current: LLM.StreamInput
        let summaries = 0
        let reads = 0

        const persistence = spyOn(Session, "updateMessage").mockImplementation(Object.assign((info: MessageV2.Info) => {
          if (!first.has(info.id)) first.set(info.id, structuredClone(info))
          return save(info)
        }, { force: save.force, schema: save.schema }))
        const observer = spyOn(LLM, "stream").mockImplementation(async (input) => {
          current = input
          snapshots.push(structuredClone(await Session.messages({ sessionID: session.id })))
          const compacting = input.agent.name === "compaction"
          const lost = baseline && input.user.id !== body.messageID
          expect(input.user.format).toEqual(lost ? undefined : format)
          // Observe the original contract before provider/schema transformations.
          if (!compacting && !text && !lost) {
            expect(input.user.format?.type).toBe("json_schema")
            if (input.user.format?.type !== "json_schema") throw new Error("COMPACTION_FORMAT_MISSING before provider")
            if (format?.type !== "json_schema") throw new Error("Expected structured fixture contract")
            expect(input.user.format.schema).toEqual(format.schema)
            expect(input.user.format.retryCount).toBe(0)
            expect(typeof input.user.format.retryCount).toBe("number")
            expect(input.tools.StructuredOutput).toBeDefined()
            expect(input.toolChoice).toBe("required")
          }
          if (compacting) {
            expect(input.tools).toEqual({})
            expect(input.toolChoice).toBeUndefined()
            expect(input.system.join("\n")).not.toContain("MUST use the StructuredOutput")
          }
          return stream(input)
        })
        const language = spyOn(Provider, "getLanguage").mockResolvedValue({
          specificationVersion: "v2", provider: "fixture", modelId: "research", supportedUrls: {},
          doGenerate: async () => { throw new Error("Only streamed native requests are expected") },
          doStream: async (options) => {
            const compacting = current.agent.name === "compaction"
            const lost = baseline && current.user.id !== body.messageID
            const structured = !compacting && !text && !lost
            const tool = options.tools?.find((item) => item.type === "function" && item.name === "StructuredOutput")
            providers.push({ agent: current.agent.name, structured: !!tool, choice: options.toolChoice })
            expect(!!tool).toBe(structured)
            const system = options.prompt.filter((item) => item.role === "system").map((item) => item.content).join("\n")
            expect(system.includes("MUST use the StructuredOutput")).toBe(structured)
            expect(options.toolChoice).toEqual(structured ? { type: "required" } : compacting ? undefined : { type: "auto" })
            if (structured && tool?.type === "function" && format?.type === "json_schema") {
              const { $schema, ...schema } = format.schema
              expect(tool.inputSchema).toEqual(schema)
              expect(tool.description).toContain("Complete all necessary research")
              expect(asSchema(current.tools.StructuredOutput.inputSchema).jsonSchema).toEqual(schema)
            }
            const chunks: LanguageModelV2StreamPart[] = [{ type: "stream-start", warnings: [] }]
            // One real read before each processor compaction and another after
            // the final continuation, then the actual native StructuredOutput.
            const research = !compacting && reads < count + 1 - Number(threshold)
            const compact = research && summaries < count
            if (compacting || (!research && !structured)) {
              chunks.push(
                { type: "text-start", id: "text" },
                { type: "text-delta", id: "text", delta: compacting ? "Summary of evidence; continue research." : "Plain answer." },
                { type: "text-end", id: "text" },
              )
              if (compacting) summaries++
            } else {
              const name = research ? "read" : "StructuredOutput"
              expect(options.tools?.some((item) => item.type === "function" && item.name === name)).toBe(true)
              const id = `call-${reads}-${summaries}`
              const input = JSON.stringify(research ? { filePath: path.join(tmp.path, "evidence.txt") } : answer)
              chunks.push(
                { type: "tool-input-start", id, toolName: name },
                { type: "tool-input-delta", id, delta: input },
                { type: "tool-input-end", id },
                { type: "tool-call", toolCallId: id, toolName: name, input },
              )
              if (research) reads++
            }
            chunks.push({ type: "finish", finishReason: research || structured ? "tool-calls" : "stop",
              usage: { inputTokens: compact ? 91_000 : 100, outputTokens: 20, totalTokens: compact ? 91_020 : 120 } })
            return { stream: new ReadableStream({ start(controller) { chunks.forEach((chunk) => controller.enqueue(chunk)); controller.close() } }) }
          },
        })
        spies.push(persistence, observer, language)
        try {
          await SessionPrompt.prompt({ ...body, noReply: true })
          if (threshold) {
            // Seed a completed prior step through supported native APIs. The
            // pre-loop threshold branch must compact it before any stage call.
            await Session.updateMessage({
              id: MessageID.ascending(), sessionID: session.id, role: "assistant", parentID: body.messageID,
              agent: "audit", mode: "audit", providerID: model.providerID, modelID: model.modelID,
              path: { cwd: tmp.path, root: tmp.path }, cost: 0,
              tokens: { input: 91_000, output: 20, reasoning: 0, cache: { read: 0, write: 0 } },
              time: { created: Date.now(), completed: Date.now() }, finish: "tool-calls",
            })
          }
          const final = await SessionPrompt.loop({ sessionID: session.id })
          const history = await Session.messages({ sessionID: session.id })
          snapshots.push(structuredClone(history))
          expect(final.info.role).toBe("assistant")
          if (final.info.role !== "assistant") throw new Error("Expected native assistant")
          expect(final.info.error).toBeUndefined()
          expect(history.at(-1)).toEqual(final)
          expect(summaries).toBe(count)
          const services = history.filter((item) => item.parts.some((part) => part.type === "compaction"))
          expect(services).toHaveLength(count)
          for (const [index, service] of services.entries()) {
            const part = service.parts.find((part) => part.type === "compaction")!
            if (part.type !== "compaction") throw new Error("Expected native compaction part")
            expect(part.auto).toBe(true)
            // The upstream threshold path omits overflow; never relabel it.
            expect(part.overflow).toBe(threshold && index === 0 ? undefined : false)
          }
          const users = history.filter((item) => item.info.role === "user")
          expect(users).toHaveLength(1 + count * 2)
          expect(new Set(users.map((item) => item.info.id)).size).toBe(users.length)
          for (const user of users) {
            if (user.info.role !== "user") continue
            const expected = baseline && user.info.id !== body.messageID ? undefined : format
            expect(user.info.format).toEqual(expected)
            const saved = first.get(user.info.id)!
            if (saved.role !== "user") throw new Error("Expected first user save")
            expect(saved.format).toEqual(expected)
            expect(saved.time.created).toBe(user.info.time.created)
            expect(saved.agent).toBe(body.agent)
            expect(saved.model).toEqual(body.model)
          }
          for (const summary of history.filter((item) => item.info.role === "assistant" && item.info.summary)) {
            if (summary.info.role !== "assistant") continue
            expect(summary.info.structured).toBeUndefined()
            expect(summary.parts.some((part) => part.type === "text" && part.text.startsWith("Summary"))).toBe(true)
          }
          const calls = history.flatMap((item) => item.parts).filter((part) => part.type === "tool")
          expect(calls.filter((part) => part.tool === "read")).toHaveLength(reads)
          for (const call of calls) {
            expect(call.state.status).toBe("completed")
            if (call.tool === "read" && call.state.status === "completed") expect(call.state.output).toContain("Native read tool evidence")
          }
          if (text || (baseline && count)) {
            expect(final.info.structured).toBeUndefined()
            expect(calls.some((part) => part.tool === "StructuredOutput")).toBe(false)
          } else {
            expect(final.info.structured).toEqual(answer)
            const captured = calls.filter((part) => part.tool === "StructuredOutput")
            expect(captured).toHaveLength(1)
            expect(captured[0].state.input).toEqual(final.info.structured)
          }
          await Bun.write(path.join(output, `${baseline ? "baseline" : "fixed"}-${count}-${threshold}-${text}-${label}.json`),
            JSON.stringify({ body, snapshots, final, providers, count, threshold, text, baseline }))
        } finally {
          persistence.mockRestore(); observer.mockRestore(); language.mockRestore()
          // tmpdir/preload own all data; no user's runtime database is involved.
        }
      }
      await Instance.dispose()
    },
  })
}

test.skipIf(!baseline)("stock runtime produces native structured output without compaction", () => run(0))
test.skipIf(!baseline)("original runtime loses the native contract after automatic compaction", () => run(1))
test.skipIf(!baseline)("stock threshold compaction omits overflow and loses the native contract", () => run(1, true))
for (const count of [0, 1, 2]) {
  test.skipIf(baseline)(`native structured result after ${count} processor compactions`, () => run(count))
}
test.skipIf(baseline)("native contract survives threshold then processor compaction", () => run(2, true))
for (const mode of ["explicit", "implicit"] as const) {
  test.skipIf(baseline)(`${mode} text requests remain text after two compactions`, () => run(2, false, mode))
}
