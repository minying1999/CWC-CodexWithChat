// Run: node tests/test_host_relay.js. No Host calls, subprocesses, or production state.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const relay = (0, eval)(fs.readFileSync(path.join(__dirname, "../skills/cwc/scripts/host_relay.js"), "utf8"));
const SEND = "mcp__codex_app__send_message_to_thread", READ = "mcp__codex_app__read_thread";
const ID = "req_" + "a".repeat(32), TARGET = "chat-1";
const SCOPE = { host_id: "local", thread_id: "root-1", workspace: "D:/项目" };
const BODY = "中文咨询保持原文：路径 D:\\资料\\a，单双引号 ' \"、反引号 `、字面量 $(never_execute)、\\n。\n".repeat(80);
const OPTIONS = { controller: "D:/插件's folder/cwc.py", inputPath: "D:/临时/input.json", outputDir: "D:/临时/evidence" };
const copy = value => JSON.parse(JSON.stringify(value));

function fixture(input = { action: "prepare", scope: SCOPE, body: BODY }) {
  const files = new Map([[OPTIONS.inputPath, JSON.stringify(input)]]);
  const f = { files, calls: [], sends: [], reads: [], state: "PREPARED", sentRaw: { content: [{ type: "text", text: "raw send" }] },
    readRaw: { content: [{ type: "text", text: "unparsed raw answer" }] }, accept: { status: "CONSUMED", request_id: ID, answer: "controller-validated answer" } };
  f.packet = { status: "SEND_ONCE", request_id: ID, tool: SEND, arguments: { threadId: TARGET, prompt: `CWC REQUEST ${ID}\n${BODY}` } };
  f.readPacket = () => ({ status: "READ_PENDING", request_id: ID, tool: READ, arguments: { threadId: TARGET, turnLimit: 10, includeOutputs: true, maxOutputCharsPerItem: 20000 } });
  const unquote = value => value.slice(1, -1).replace(/''/g, "'");
  f.tools = {
    exec_command: async ({ cmd }) => {
      const quoted = "('(?:[^']|'')*')";
      const read = cmd.match(new RegExp("^Get-Content -LiteralPath " + quoted + " -Raw -Encoding UTF8$"));
      if (read) {
        const file = unquote(read[1]);
        return files.has(file) ? { exit_code: 0, output: files.get(file) } : { exit_code: 1, output: "file absent" };
      }
      const run = cmd.match(new RegExp("^& python -X utf8 -B " + quoted + " --input " + quoted + "$"));
      assert.ok(run, "only the fixed local controller command is allowed");
      assert.equal(unquote(run[1]), OPTIONS.controller);
      const data = JSON.parse(files.get(unquote(run[2])));
      f.calls.push(data);
      let result;
      if (data.action === "prepare") result = { status: "PREPARED", request_id: ID };
      else if (data.action === "claim") {
        if (f.state !== "PREPARED") result = { status: "ALREADY_CLAIMED", request_id: ID, state: f.state };
        else { f.state = "UNKNOWN"; result = copy(f.packet); }
      } else if (data.action === "record-send") {
        assert.deepEqual(data.called_with, f.sends[0]);
        assert.deepEqual(data.result, f.sentRaw, "raw send envelope must survive unchanged");
        f.state = data.result.isError ? "UNKNOWN" : "WAITING";
        result = { status: f.state, request_id: ID, next: f.readPacket() };
      } else if (data.action === "read") result = f.readPacket();
      else if (data.action === "accept") {
        assert.deepEqual(data.called_with, f.reads.at(-1));
        assert.deepEqual(data.result, f.readRaw, "raw read envelope must survive unchanged");
        result = typeof f.accept === "function" ? f.accept() : f.accept;
      } else throw new Error("Unexpected action: " + data.action);
      return { exit_code: 0, output: JSON.stringify(result) };
    },
    apply_patch: async patch => {
      const lines = patch.split("\n");
      assert.equal(lines[0], "*** Begin Patch");
      assert.ok(lines[1].startsWith("*** Add File: "));
      assert.equal(lines.at(-1), "*** End Patch");
      const file = lines[1].slice("*** Add File: ".length), content = lines.slice(2, -1).map(line => { assert.equal(line[0], "+"); return line.slice(1); }).join("\n");
      if (f.failEvidence && file.includes("record-send")) throw new Error("disk full");
      files.set(file, content);
      return {};
    },
    [SEND]: async args => { assert.ok(Object.isFrozen(args)); f.sends.push(args); if (f.sendThrow) throw new Error("uncertain transport"); return f.sentRaw; },
    [READ]: async args => { assert.ok(Object.isFrozen(args)); f.reads.push(args); if (f.readThrow) throw new Error("read unavailable"); return f.readRaw; }
  };
  return f;
}

(async () => {
  let count = 0;
  const test = async (name, check) => { await check(); count++; process.stdout.write("PASS " + name + "\n"); };
  await test("Chinese body, exact target, raw evidence, parsed answer and safe path quoting", async () => {
    assert.ok(Buffer.byteLength(BODY, "utf8") > 3000);
    const f = fixture(), result = await relay(f.tools, OPTIONS);
    assert.equal(f.sends.length, 1); assert.deepEqual(f.sends[0], f.packet.arguments);
    assert.equal(f.reads[0].threadId, TARGET); assert.equal(result.answer, "controller-validated answer");
    assert.equal(result.status, "CONSUMED"); assert.equal(result.host_sends, 1);
    assert.ok(result.evidence.some(file => file.includes("record-send")));
  });
  await test("missing send or read capability does not prepare or claim", async () => {
    for (const missing of [SEND, READ, "apply_patch", "exec_command"]) {
      const f = fixture(); delete f.tools[missing];
      await assert.rejects(relay(f.tools, OPTIONS), /Required tool unavailable/);
      assert.equal(f.calls.length, 0); assert.equal(f.sends.length, 0);
    }
  });
  await test("empty body does not prepare", async () => {
    const f = fixture({ action: "prepare", scope: SCOPE, body: " \n " });
    await assert.rejects(relay(f.tools, OPTIONS), /substantive/); assert.equal(f.calls.length, 0);
  });
  await test("prepare cannot reuse a request identifier", async () => {
    const f = fixture({ action: "prepare", scope: SCOPE, body: BODY, request_id: ID });
    await assert.rejects(relay(f.tools, OPTIONS), /Use resume/);
    assert.equal(f.calls.length, 0); assert.equal(f.sends.length, 0);
  });
  await test("wrong request, malformed target, empty prompt and extra fields never send", async () => {
    for (const mutate of [p => p.request_id = "req_" + "b".repeat(32), p => p.arguments.threadId = "", p => p.arguments.threadId = "chat\nwrong", p => p.arguments.prompt = "", p => p.arguments.prompt = `CWC REQUEST ${ID}\n`, p => p.arguments.prompt = "CWC REQUEST wrong\nbody", p => p.arguments.model = "unexpected", p => p.tool = READ]) {
      const f = fixture(); mutate(f.packet);
      await assert.rejects(relay(f.tools, OPTIONS)); assert.equal(f.sends.length, 0);
    }
  });
  await test("repeating a claimed request cannot send again", async () => {
    const f = fixture({ action: "claim", scope: SCOPE, request_id: ID });
    await relay(f.tools, { ...OPTIONS, maxReads: 0 });
    const result = await relay(f.tools, { ...OPTIONS, maxReads: 0 });
    assert.equal(f.sends.length, 1); assert.equal(result.status, "ALREADY_CLAIMED"); assert.equal(result.host_sends, 0);
  });
  await test("raw tool error is retained without reinterpretation or retry", async () => {
    const f = fixture(); f.sentRaw = { isError: true, content: [{ type: "text", text: "Invalid arguments" }], structuredContent: { status: "ERROR" } };
    const result = await relay(f.tools, { ...OPTIONS, maxReads: 0 });
    assert.equal(result.status, "UNKNOWN"); assert.equal(f.sends.length, 1);
    assert.deepEqual(f.calls.find(c => c.action === "record-send").result, f.sentRaw);
  });
  await test("thrown send saves exception, invents no receipt, never retries", async () => {
    const f = fixture(); f.sendThrow = true;
    const result = await relay(f.tools, OPTIONS);
    assert.equal(result.status, "UNKNOWN"); assert.equal(f.sends.length, 1); assert.equal(f.reads.length, 0);
    assert.equal(f.calls.filter(c => c.action === "record-send").length, 0);
    const saved = JSON.parse(f.files.get(result.evidence.at(-1)));
    assert.equal(saved.exception.message, "uncertain transport"); assert.equal(Object.hasOwn(saved, "result"), false);
  });
  await test("evidence write failure cannot advance receipt or resend", async () => {
    const f = fixture(); f.failEvidence = true;
    await assert.rejects(relay(f.tools, OPTIONS), /disk full/);
    assert.equal(f.sends.length, 1); assert.equal(f.calls.filter(c => c.action === "record-send").length, 0); assert.equal(f.state, "UNKNOWN");
  });
  await test("changed read target is rejected before reading", async () => {
    const f = fixture(), original = f.readPacket;
    f.readPacket = () => { const packet = original(); packet.arguments.threadId = "other-chat"; return packet; };
    await assert.rejects(relay(f.tools, OPTIONS), /frozen request/); assert.equal(f.sends.length, 1); assert.equal(f.reads.length, 0);
  });
  await test("read-only mode needs no send tool and returns no unaccepted answer", async () => {
    const f = fixture({ action: "read", scope: SCOPE, request_id: ID }); delete f.tools[SEND];
    f.accept = { status: "NO_MATCH", request_id: ID, state: "WAITING" };
    const result = await relay(f.tools, { ...OPTIONS, mode: "read" });
    assert.equal(result.status, "NO_MATCH"); assert.equal(result.answer, undefined); assert.equal(result.host_sends, 0); assert.equal(f.sends.length, 0);
  });
  await test("read limit cannot exceed the actual Host ceiling", async () => {
    const f = fixture({ action: "read", scope: SCOPE, request_id: ID }), original = f.readPacket;
    f.readPacket = () => { const packet = original(); packet.arguments.maxOutputCharsPerItem = 20001; return packet; };
    await assert.rejects(relay(f.tools, { ...OPTIONS, mode: "read" }), /Invalid Host operation arguments/);
    assert.equal(f.reads.length, 0);
  });
  await test("bounded subsequent reads use the same target and accept only once", async () => {
    const f = fixture({ action: "read", scope: SCOPE, request_id: ID }); let reads = 0;
    f.accept = () => ++reads === 1 ? { status: "NO_MATCH", request_id: ID } : { status: "CONSUMED", request_id: ID, answer: "validated after wait" };
    const result = await relay(f.tools, { ...OPTIONS, mode: "read", maxReads: 2, delayMs: 0 });
    assert.equal(f.reads.length, 2); assert.ok(f.reads.every(args => args.threadId === TARGET)); assert.equal(result.answer, "validated after wait");
  });
  await test("invalid limits or mode fail before local actions", async () => {
    for (const options of [{ maxReads: 121 }, { maxReads: -1 }, { mode: "unknown" }, { mode: "read", maxReads: 0 }, { delayMs: 30001 }]) {
      const f = fixture(); await assert.rejects(relay(f.tools, { ...OPTIONS, ...options }), /bounded relay options/); assert.equal(f.calls.length, 0);
    }
  });
  process.stdout.write(`${count} relay checks passed; real Host calls: 0\n`);
})().catch(error => { console.error(error); process.exitCode = 1; });
