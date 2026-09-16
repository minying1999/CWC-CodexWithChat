// Run: node tests/test_host_relay_integration.js (Windows, Python and PowerShell).
// Real relay, files, CLI, SQLite and Host parser; only Host transport is synthetic.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFile } = require("node:child_process");
const { createHash } = require("node:crypto");

const SOURCE = path.resolve(__dirname, "..");
const relay = (0, eval)(fs.readFileSync(path.join(SOURCE, "skills/cwc/scripts/host_relay.js"), "utf8"));
const entry = (0, eval)(fs.readFileSync(path.join(SOURCE, "skills/cwc/scripts/entry_relay.js"), "utf8"));
const SEND = "mcp__codex_app__send_message_to_thread", READ = "mcp__codex_app__read_thread";
const VERSION = JSON.parse(fs.readFileSync(path.join(SOURCE, ".codex-plugin/plugin.json"), "utf8").replace(/^\uFEFF/, "")).version;
const BODY = "中文咨询原文：路径 D:\\资料\\a，单双引号 ' \"、反引号 `、字面量 $(throw 'MUST_NOT_EXECUTE')、\\n。\n".repeat(160);
const ANSWER = "合成审查结果：保留冻结目标与原文；先检查实际工作，再继续。";
const sha256 = value => createHash("sha256").update(value).digest("hex");
const ROOT = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), "cwc-relay-integration-")));
let fixtureCount = 0, checkCount = 0, syntheticSends = 0;

function inside(file, root = ROOT) {
  const relative = path.relative(fs.realpathSync(root), path.resolve(file));
  assert.ok(relative && relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative), "only disposable test paths are allowed: " + JSON.stringify({file,root}));
  return file;
}

function child(command, args, cwd, env) {
  inside(cwd);
  inside(env.CODEX_HOME, cwd);
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd, env, windowsHide: true, encoding: "utf8", timeout: 30000, maxBuffer: 8 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (error && !Number.isInteger(error.code)) return reject(error);
      resolve({ exit_code: error ? error.code : 0, output: stdout + stderr });
    });
  });
}

async function fixture(host = "chatgpt", paired = true) {
  const root = path.join(ROOT, `case-${++fixtureCount}`, "中文's $(throw 'PATH_EXECUTED')");
  fs.mkdirSync(root, { recursive: true });
  const pkg = path.join(root, "package's folder"), controller = path.join(pkg, "skills/cwc/scripts/cwc.py");
  // Copy code/resources, never package state: legacy.py resolves its own package root.
  for (const relative of [".codex-plugin/plugin.json", "skills/cwc/SKILL.md", "skills/cwc/REASONER.md", "skills/cwc/CWC_AGENT_CONTRACT.md", "skills/cwc/references/HOST_WORKFLOW.md",
    "skills/cwc/references/WORKFLOW.md", "skills/cwc/references/CONTINUATION.md",
    ...["cwc.py", "store.py", "host.py", "legacy.py", "host_relay.js", "progress.py", "entry.py", "entry_relay.js"].map(file => "skills/cwc/scripts/" + file)]) {
    const target = inside(path.join(pkg, relative));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(SOURCE, relative), target);
    assert.equal(sha256(fs.readFileSync(target)), sha256(fs.readFileSync(path.join(SOURCE, relative))));
  }
  const home = path.join(root, "home"), workspace = path.join(root, "workspace");
  fs.mkdirSync(workspace);
  const f = { root, home, host, target: "synthetic-chat-" + fixtureCount, sends: [], reads: [], commands: [], events: [], inputs: [], readResults: [] };
  f.scope = { host_id: "local", thread_id: "synthetic-task-" + fixtureCount, workspace };
  f.env = { ...process.env, CODEX_HOME: home, CODEX_THREAD_ID: f.scope.thread_id, PYTHONIOENCODING: "utf-8", PYTHONDONTWRITEBYTECODE: "1" };
  f.options = { controller, inputPath: path.join(root, "input's file.json"), outputDir: path.join(root, "evidence's folder"), delayMs: 0, expectedVersion: VERSION };
  f.route = { threadId: f.target, ...(host === "chatgpt" ? {} : { hostId: host }) };
  const write = (file, value) => {
    inside(file, root);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(value, null, 2), "utf8");
  };
  f.cli = async (action, data = {}) => {
    const input = { scope: f.scope, action, ...(["prepare", "consult"].includes(action) ? {waiting: {initial_delay_ms: 0, poll_interval_ms: 0}} : {}), ...data }, file = path.join(root, `setup-${f.inputs.length}.json`);
    f.inputs.push(input);
    write(file, input);
    const result = await child("python", ["-X", "utf8", "-B", controller, "--input", file], root, f.env);
    assert.equal(result.exit_code, 0, result.output);
    return JSON.parse(result.output);
  };
  f.row = async requestId => {
    const database = inside(path.join(home, "cwc/state/cwc.sqlite3"), root);
    const result = await child("python", ["-X", "utf8", "-B", "-c",
      "import json,sqlite3,sys; from pathlib import Path; db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True); db.row_factory=sqlite3.Row; row=db.execute('SELECT * FROM exchanges WHERE request_id=?',(sys.argv[2],)).fetchone(); print(json.dumps(dict(row) if row else None,ensure_ascii=False)); db.close()",
      database, requestId], root, f.env);
    assert.equal(result.exit_code, 0, result.output);
    return JSON.parse(result.output);
  };
  f.snapshot = (requestId, status = "completed") => ({ content: [{ type: "text", text: JSON.stringify({
    thread: { id: f.target, kind: "chatgpt" }, turns: [{ id: "synthetic-turn", status, items: [
      { type: "agentMessage", id: "synthetic-reply", text: `CWC RESPONSE ${requestId}\n${ANSWER}` }
    ] }]
  }) }] });
  f.sendResult = { structuredContent: { ...f.route, status: "queued", success: true, turnId: "synthetic-send-turn" } };
  f.tools = {
    exec_command: async ({ cmd }) => {
      f.commands.push(cmd);
      // Run the actual shell command; parsing here only fences its file arguments.
      const quoted = "('(?:[^']|'')*')", unquote = value => value.slice(1, -1).replace(/''/g, "'");
      const read = cmd.match(new RegExp("^Get-Content -LiteralPath " + quoted + " -Raw -Encoding UTF8$"));
      const run = cmd.match(new RegExp("^& python -X utf8 -B " + quoted + " --input " + quoted + "(?: --expected-version " + quoted + ")?$"));
      assert.ok(read || run, "only relay file reads and the real controller command may execute");
      if (read) inside(unquote(read[1]), root);
      if (run) { assert.ok([controller,path.join(pkg,"skills/cwc/scripts/entry.py")].includes(unquote(run[1]))); inside(unquote(run[2]), root); }
      const result = await child("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command",
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + cmd], root, f.env);
      if (run) f.events.push({ action: JSON.parse(fs.readFileSync(unquote(run[2]), "utf8")).action, at: Date.now(), exit_code: result.exit_code });
      return result;
    },
    apply_patch: async patch => {
      const lines = patch.split("\n");
      assert.equal(lines.shift(), "*** Begin Patch");
      const add = lines.shift();
      assert.ok(add.startsWith("*** Add File: "), "only one Add File operation is allowed");
      assert.equal(lines.pop(), "*** End Patch");
      const file = inside(add.slice("*** Add File: ".length), root);
      f.events.push({ file, at: Date.now() });
      const content = lines.map(line => { assert.equal(line[0], "+"); return line.slice(1); }).join("\n");
      if (f.failEvidence && file.includes("record-send")) throw new Error("synthetic disk failure");
      fs.mkdirSync(path.dirname(file), { recursive: true });
      inside(fs.realpathSync(path.dirname(file)), root);
      fs.writeFileSync(file, content, { encoding: "utf8", flag: "wx" });
      return {};
    },
    [SEND]: async args => {
      assert.ok(Object.isFrozen(args));
      const requestId = args.prompt.split("\n", 1)[0].slice("CWC REQUEST ".length);
      const frozen = await f.row(requestId);
      assert.equal(frozen.state, "UNKNOWN", "claim must commit before transport");
      assert.equal(frozen.payload, args.prompt);
      assert.equal(sha256(frozen.payload), sha256(args.prompt));
      assert.equal(frozen.target_id, f.target); assert.equal(frozen.target_host, host);
      assert.deepEqual({ ...args, prompt: undefined }, { ...f.route, prompt: undefined });
      f.sends.push({ ...args }); syntheticSends++;
      if (f.sendThrow) throw new Error("synthetic interrupted send");
      return f.sendResult;
    },
    [READ]: async args => {
      assert.ok(Object.isFrozen(args));
      assert.equal(args.threadId, f.target); assert.equal(args.hostId, f.route.hostId);
      f.reads.push({ args: { ...args }, at: Date.now() });
      if (f.readThrow) throw new Error("synthetic interrupted read");
      if (f.readResults.length) return f.readResults.shift();
      const state = await f.cli("status");
      return f.snapshot((state.exchange || state.last_exchange).request_id);
    }
  };
  f.run = async (input, options = {}) => {
    write(f.options.inputPath, { scope: f.scope, ...(["prepare", "consult"].includes(input.action) ? {waiting: {initial_delay_ms: 0, poll_interval_ms: 0}} : {}), ...input });
    return relay(f.tools, { ...f.options, ...options });
  };
  f.tools.mcp__codex_app__list_threads = async args => { assert.ok(args.limit <= 50); return {threads:[
    {id:f.target,kind:"chatgpt",title:"Synthetic target",...(host==="chatgpt"?{}:{hostId:host})}
  ]}; };
  f.entry = (command, options={}) => entry(f.tools, {controller:path.join(pkg,"skills/cwc/scripts/entry.py"),
    transport:controller,transport_source_path:path.join(pkg,"skills/cwc/scripts/host_relay.js"),expectedVersion:VERSION,
    scope:f.scope,outputDir:f.options.outputDir,command,...options});
  if (paired) assert.equal((await f.cli("pair", { listing: { threads: [{ id: f.target, kind: "chatgpt", title: "Synthetic target", ...(host === "chatgpt" ? {} : { hostId: host }) }] },
    conversation_id: f.target, target_host_id: host })).status, "SELECTED");
  return f;
}

async function check(name, work) {
  if (process.env.CWC_TEST_FILTER && !name.includes(process.env.CWC_TEST_FILTER)) return;
  await work(); checkCount++;
  process.stdout.write("PASS " + name + "\n");
}

(async () => {
  await check("real shell quoting, long Chinese body, frozen target/hash and delayed acceptance", async () => {
    assert.ok(Buffer.byteLength(BODY, "utf8") > 16000);
    const f = await fixture();
    f.readResults.push({ content: [{ type: "text", text: JSON.stringify({ thread: { id: f.target, kind: "chatgpt" }, turns: [] }) }] });
    const result = await f.run({ action: "prepare", body: BODY }, { maxReads: 2, delayMs: 80 });
    assert.equal(result.status, "CONSUMED"); assert.equal(result.answer, ANSWER);
    assert.equal(result.host_sends, 1); assert.equal(f.sends.length, 1); assert.equal(f.reads.length, 2);
    const acceptedAt = f.events.find(event => event.action === "accept").at;
    const nextReadAt = f.events.find(event => event.file?.endsWith("-read-1.json")).at;
    assert.ok(nextReadAt - acceptedAt >= 70, "bounded retry must wait after accept, independently of subprocess time");
    const row = await f.row(result.request_id);
    assert.equal(row.state, "CONSUMED"); assert.equal(row.answer_hash, sha256(ANSWER));
    assert.ok(row.payload.includes("\n\n" + BODY + "\n\n"));
    assert.equal(row.payload, f.sends[0].prompt);
    const evidence = result.evidence.map(file => { inside(file, f.root); return JSON.parse(fs.readFileSync(file, "utf8")); });
    const sent = evidence.find(data => data.action === "record-send");
    assert.deepEqual(sent.called_with, f.sends[0]); assert.deepEqual(sent.result, f.sendResult);
    assert.equal(evidence.filter(data => data.action === "accept").length, 2);
    process.stdout.write("  frozen payload SHA-256 " + sha256(row.payload) + "\n");
  });

  await check("legacy claim API commits once; repeated claim sends zero messages", async () => {
    const f = await fixture("local"), prepared = await f.cli("prepare", { body: BODY });
    const first = await f.run({ action: "claim", request_id: prepared.request_id }, { maxReads: 0 });
    const again = await f.run({ action: "claim", request_id: prepared.request_id }, { maxReads: 0 });
    assert.equal(first.status, "WAITING"); assert.equal(again.status, "ALREADY_CLAIMED");
    assert.equal(again.host_sends, 0); assert.equal(f.sends.length, 1);
    assert.equal((await f.row(prepared.request_id)).state, "WAITING");
  });

  await check("send interruption leaves UNKNOWN; scope resume reads without resending", async () => {
    const f = await fixture(); f.sendThrow = true;
    const interrupted = await f.run({ action: "prepare", body: BODY });
    assert.equal(interrupted.status, "UNKNOWN"); assert.equal((await f.row(interrupted.request_id)).state, "UNKNOWN");
    delete f.tools[SEND];
    const resumed = await f.run({ action: "resume" }, { mode: "read" });
    assert.equal(resumed.request_id, interrupted.request_id); assert.equal(resumed.status, "CONSUMED");
    assert.equal(resumed.answer, ANSWER); assert.equal(resumed.host_sends, 0); assert.equal(f.sends.length, 1);
  });

  await check("scope resume keeps PREPARED untouched in read mode and claims once in send mode", async () => {
    const f = await fixture(), prepared = await f.cli("prepare", { body: BODY });
    const transport = { ...f.tools }; f.tools = { exec_command: transport.exec_command };
    const pending = await f.run({ action: "resume" }, { mode: "read" });
    assert.equal(pending.status, "PREPARED"); assert.equal(pending.host_sends, 0); assert.equal(f.reads.length, 0);
    assert.deepEqual(pending.evidence, []);
    assert.equal((await f.row(prepared.request_id)).state, "PREPARED");
    for (const missing of [SEND, READ, "apply_patch"]) {
      f.tools = { ...transport }; delete f.tools[missing];
      await assert.rejects(f.run({ action: "resume" }, { mode: "send" }), /Required tool unavailable/);
      assert.equal((await f.row(prepared.request_id)).state, "PREPARED", "missing transport must fail before claim");
      assert.equal(f.sends.length, 0); assert.equal(f.reads.length, 0);
    }
    f.tools = transport;
    const sent = await f.run({ action: "resume" }, { mode: "send" });
    assert.equal(sent.status, "CONSUMED"); assert.equal(sent.host_sends, 1); assert.equal(f.sends.length, 1);
  });

  await check("READY and CONSUMED recovery requires only the real exec capability", async () => {
    const f = await fixture(), prepared = await f.cli("prepare", { body: BODY });
    await f.cli("claim", { request_id: prepared.request_id });
    const received = await f.cli("receive", { request_id: prepared.request_id, called_with: f.route, result: f.snapshot(prepared.request_id) });
    assert.equal(received.status, "READY");
    f.tools = { exec_command: f.tools.exec_command };
    const ready = await f.run({ action: "resume" }, { mode: "read" });
    assert.equal(ready.status, "CONSUMED"); assert.equal(ready.answer, ANSWER); assert.equal(ready.host_sends, 0);
    assert.deepEqual(ready.evidence, []);
    const before = await f.row(prepared.request_id);
    for (const mode of ["read", "send"]) {
      const recovered = await f.run({ action: "resume" }, { mode });
      assert.equal(recovered.status, "RECOVERED"); assert.equal(recovered.answer, ANSWER);
      assert.equal(recovered.effect_check_required, true); assert.equal(recovered.host_sends, 0);
      assert.deepEqual(recovered.evidence, []);
    }
    assert.deepEqual(await f.row(prepared.request_id), before);
    assert.equal(f.sends.length, 0); assert.equal(f.reads.length, 0);
  });

  await check("IDLE scope recovery requires only exec and creates no local state", async () => {
    const f = await fixture("chatgpt", false);
    f.tools = { exec_command: f.tools.exec_command };
    for (const mode of ["read", "send"]) {
      const result = await f.run({ action: "resume" }, { mode });
      assert.equal(result.status, "IDLE"); assert.equal(result.host_sends, 0); assert.deepEqual(result.evidence, []);
    }
    assert.equal(fs.existsSync(f.home), false); assert.equal(f.sends.length, 0); assert.equal(f.reads.length, 0);
  });

  await check("legacy read API resumes WAITING without the send capability", async () => {
    const f = await fixture(), sent = await f.run({ action: "prepare", body: BODY }, { maxReads: 0 });
    delete f.tools[SEND];
    const result = await f.run({ action: "read", request_id: sent.request_id }, { mode: "read" });
    assert.equal(result.status, "CONSUMED"); assert.equal(result.host_sends, 0); assert.equal(f.sends.length, 1);
  });

  await check("Host error receipts and disk interruption retain UNKNOWN with no second send", async () => {
    for (const fail of ["receipt", "evidence"]) {
      const f = await fixture();
      if (fail === "receipt") f.sendResult = { isError: true, content: [{ type: "text", text: "Synthetic transport failure" }] };
      else f.failEvidence = true;
      if (fail === "receipt") assert.equal((await f.run({ action: "prepare", body: BODY }, { maxReads: 0 })).status, "UNKNOWN");
      else await assert.rejects(f.run({ action: "prepare", body: BODY }), /synthetic disk failure/);
      const state = await f.cli("status"), requestId = state.exchange.request_id;
      assert.equal(state.exchange.state, "UNKNOWN"); assert.equal(f.sends.length, 1);
      const repeated = await f.run({ action: "claim", request_id: requestId }, { maxReads: 0 });
      assert.equal(repeated.host_sends, 0); assert.equal(f.sends.length, 1);
    }
  });

  await check("error, truncated and wrong-target read envelopes never become accepted answers", async () => {
    for (const failure of ["error", "truncated", "wrong-target", "malformed-json"]) {
      const f = await fixture(), sent = await f.run({ action: "prepare", body: BODY }, { maxReads: 0 });
      let raw = f.snapshot(sent.request_id);
      if (failure === "error") raw.isError = true;
      if (failure === "truncated") raw.truncated = true;
      if (failure === "wrong-target") raw.threadId = "synthetic-other-chat";
      if (failure === "malformed-json") raw.content[0].text = raw.content[0].text.slice(0, -8);
      f.readResults.push(raw); delete f.tools[SEND];
      await assert.rejects(f.run({ action: "read", request_id: sent.request_id }, { mode: "read" }), error => {
        const codes = { error: "HOST_REPORTED_ERROR", truncated: "HOST_PARTIAL", "wrong-target": "HOST_ROUTE_MISMATCH", "malformed-json": "HOST_DATA_INVALID" };
        assert.equal(error.code, codes[failure]); assert.equal(error.request_id, sent.request_id); assert.equal(error.host_sends, 0);
        const observed = error.evidence.filter(file => file.includes("-accept-"));
        assert.equal(observed.length, 1);
        const saved = JSON.parse(fs.readFileSync(inside(observed[0], f.root), "utf8"));
        assert.equal(saved.action, "accept"); assert.equal(saved.request_id, sent.request_id); assert.deepEqual(saved.result, raw);
        assert.equal(f.events.findLast(event => event.action === "accept").exit_code, 1, "the real CLI error exit must remain distinguishable");
        return true;
      });
      const row = await f.row(sent.request_id);
      assert.equal(row.state, "WAITING"); assert.equal(row.answer, null); assert.equal(row.answer_hash, null);
      assert.equal(f.sends.length, 1); assert.equal(f.reads.length, 1);
    }
  });

  await check("an error after a send preserves its request, send count and raw evidence", async () => {
    const f = await fixture(), raw = f.snapshot("req_" + "0".repeat(32));
    raw.threadId = "synthetic-wrong-target"; f.readResults.push(raw);
    await assert.rejects(f.run({ action: "prepare", body: BODY }), error => {
      assert.equal(error.code, "HOST_ROUTE_MISMATCH"); assert.match(error.request_id, /^req_[0-9a-f]{32}$/);
      assert.equal(error.host_sends, 1); assert.equal(f.sends.length, 1);
      assert.ok(f.sends[0].prompt.startsWith(`CWC REQUEST ${error.request_id}\n`));
      const evidence = error.evidence.map(file => JSON.parse(fs.readFileSync(inside(file, f.root), "utf8")));
      assert.deepEqual(evidence.find(item => item.action === "record-send").called_with, f.sends[0]);
      assert.deepEqual(evidence.find(item => item.action === "accept").result, raw);
      assert.equal(f.events.findLast(event => event.action === "accept").exit_code, 1);
      return true;
    });
    assert.equal((await f.cli("status")).exchange.state, "WAITING"); assert.equal(f.reads.length, 1);
  });

  await check("a complete response with the wrong format stops polling and stays unaccepted", async () => {
    const f = await fixture(), sent = await f.run({ action: "prepare", body: BODY }, { maxReads: 0 });
    const invalid = { structuredContent: { thread: { id: f.target, kind: "chatgpt" }, turns: [{ status: "completed", items: [
      { id: "synthetic-unmarked-reply", type: "agentMessage", text: `**CWC RESPONSE ${sent.request_id}**\n${ANSWER}` }
    ] }] } };
    f.readResults.push(invalid, f.snapshot(sent.request_id)); delete f.tools[SEND];
    const result = await f.run({ action: "resume" }, { mode: "read", maxReads: 2 });
    assert.equal(result.status, "NO_MATCH"); assert.equal(result.reason, "RESPONSE_FORMAT_MISMATCH");
    assert.equal(result.answer, undefined); assert.equal(result.host_sends, 0);
    assert.equal(f.reads.length, 1); assert.equal(f.readResults.length, 1); assert.equal(f.sends.length, 1);
    const row = await f.row(sent.request_id);
    assert.equal(row.state, "WAITING"); assert.equal(row.answer, null);
  });

  await check("version mismatch blocks prepare and claim without creating or advancing state", async () => {
    for (const alreadyPrepared of [false, true]) {
      const f = await fixture("chatgpt", alreadyPrepared);
      const prepared = alreadyPrepared ? await f.cli("prepare", { body: BODY }) : null;
      const manifestPath = inside(path.join(path.dirname(f.options.controller), "../../../.codex-plugin/plugin.json"), f.root);
      const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8").replace(/^\uFEFF/, ""));
      manifest.version = "0.0.0-synthetic-version-change";
      fs.writeFileSync(manifestPath, JSON.stringify(manifest), "utf8");
      await assert.rejects(f.run(prepared ? { action: "claim", request_id: prepared.request_id } : { action: "prepare", body: BODY }), error => {
        assert.match(error.message, /version differs/);
        assert.equal(error.code, "LOCAL_ERROR");
        assert.equal(error.request_id, prepared ? prepared.request_id : undefined);
        return true;
      });
      assert.equal(f.sends.length, 0); assert.equal(f.reads.length, 0);
      if (prepared) assert.equal((await f.row(prepared.request_id)).state, "PREPARED");
      else assert.equal(fs.existsSync(f.home), false, "version check must precede all local state creation");
    }
  });
  await check("persisted first-read delay survives resume and emits the real Q/A labels", async () => {
    const f = await fixture(), visible = [];
    const sent = await f.run({ action: "prepare", body: BODY,
      waiting: { initial_delay_ms: 1200, poll_interval_ms: 1200, wait_budget_ms: 20000, max_wait_ms: 20000 } }, { maxReads: 0 });
    const frozen = await f.row(sent.request_id), timing = JSON.parse(frozen.timing);
    assert.match(sent.question_id, /^Q-[A-F0-9]{8,32}$/);
    const accepted = await f.run({ action: "resume" }, { mode: "read", maxReads: 1, onEvent: event => visible.push(event) });
    assert.equal(accepted.status, "CONSUMED"); assert.equal(accepted.host_sends, 0);
    assert.ok(f.reads[0].at >= timing.send_returned_at + 1200, "the actual Host read must respect the persisted due time");
    assert.equal(JSON.parse((await f.row(sent.request_id)).timing).claimed_at, timing.claimed_at);
    assert.ok(visible.some(event => event.phase === "CONSUMED" && event.answer_id === accepted.answer_id));
    const child = await f.run({ action: "prepare", body: "Return the result review with the new evidence.", parent_request_id: sent.request_id }, { maxReads: 0 });
    assert.equal(child.parent_answer_id, accepted.answer_id);
    assert.notEqual(child.question_id, accepted.question_id);
    assert.equal(f.sends.length, 2, "one request for each distinct linked question");
  });
  await check("expired resume probes once, accepts late replies and never sends again", async () => {
    for (const hasAnswer of [false, true]) {
      const f = await fixture();
      const sent = await f.run({ action: "prepare", body: BODY,
        waiting: { initial_delay_ms: 0, poll_interval_ms: 0, wait_budget_ms: 1, max_wait_ms: 1 } }, { maxReads: 0 });
      const before = JSON.parse((await f.row(sent.request_id)).timing);
      if (!hasAnswer) f.readResults.push({ thread: { id: f.target, kind: "chatgpt" }, turns: [] });
      delete f.tools[SEND];
      const result = await f.run({ action: "resume" }, { mode: "read", maxReads: 64 });
      assert.equal(result.status, hasAnswer ? "CONSUMED" : "WAIT_EXPIRED");
      assert.equal(f.sends.length, 1); assert.equal(result.host_sends, 0); assert.equal(f.reads.length, 1);
      assert.equal(result.request_id, sent.request_id); assert.equal(result.question_id, sent.question_id);
      const after = JSON.parse((await f.row(sent.request_id)).timing);
      for (const key of ["claimed_at", "deadline", "max_deadline"]) assert.equal(after[key], before[key]);
      if (!hasAnswer) {
        const immediate = await f.run({ action: "resume" }, { mode: "read", maxReads: 64 });
        assert.equal(immediate.status, "WAIT_EXPIRED"); assert.equal(f.reads.length, 1);
      }
    }
  });
  await check("fixed entry shows choices first and binds the exact displayed target", async () => {
    const f=await fixture("chatgpt",false);
    const help=await f.entry(" /CWC  HELP ");
    assert.equal(help.status,"HELP"); assert.equal(help.timings.host_list_calls,0); assert.equal(fs.existsSync(f.home),false);
    const menu=await f.entry("cwc");
    assert.equal(menu.status,"CHOOSE_TARGET"); assert.equal(menu.timings.host_list_calls,1);
    assert.equal(fs.existsSync(path.join(f.home,"cwc/state/cwc.sqlite3")),false);
    const bound=await f.entry("1",{menu_token:menu.menu_token});
    assert.equal(bound.status,"BOUND"); assert.match(bound.message,/Synthetic target/);
    assert.equal(f.sends.length,0); assert.equal(bound.timings.host_list_calls,1);
    const business=await f.entry("Do the authorized task.");
    assert.equal(business.status,"BUSINESS");
    assert.equal((await f.entry("1",{menu_token:bound.menu_token})).status,"BUSINESS");
    assert.equal(f.sends.length,0);
    const refreshed=await f.entry("cwc refresh");
    assert.equal(refreshed.status,"REFRESHED"); assert.ok(JSON.stringify(refreshed).length<16000);
    assert.equal(refreshed.timings.host_list_calls,0);
  });
  await check("optional verification grants one send and repeated input resumes its original request", async () => {
    const f=await fixture("chatgpt",false);
    const menu=await f.entry("cwc bind"), bound=await f.entry("1",{menu_token:menu.menu_token});
    f.sendThrow=true;
    const first=await f.entry("1",{menu_token:bound.menu_token,maxReads:0});
    assert.equal(first.exchange_status,"UNKNOWN"); assert.equal(f.sends.length,1);
    const repeated=await f.entry("1",{menu_token:bound.menu_token,maxReads:0});
    assert.equal(repeated.request_id,first.request_id); assert.equal(f.sends.length,1); assert.equal(repeated.host_sends,0);
    await f.cli("accept",{request_id:first.request_id,called_with:f.route,result:f.snapshot(first.request_id)});
    const recovered=await f.entry("1",{menu_token:bound.menu_token,maxReads:0});
    assert.equal(recovered.status,"VERIFIED"); assert.equal(recovered.request_id,first.request_id); assert.equal(f.sends.length,1);
    assert.match(f.sends[0].prompt,/通信核验/); assert.match(f.sends[0].prompt,/交互编号：Q-/);
  });
  await check("refresh offers optional verification without sending and pending refresh cannot restart it", async () => {
    const f=await fixture();
    const refreshed=await f.entry("cwc refresh");
    assert.equal(refreshed.status,"REFRESHED"); assert.equal(refreshed.verification_available,true);
    assert.equal(f.sends.length,0); assert.equal(refreshed.timings.host_list_calls,0);
    const sent=await f.entry("1",{menu_token:refreshed.menu_token,maxReads:0});
    assert.equal(f.sends.length,1); assert.match(f.sends[0].prompt,/CWC CONTRACT v3/);
    const again=await f.entry("1",{menu_token:refreshed.menu_token,maxReads:0});
    assert.equal(again.request_id,sent.request_id); assert.equal(f.sends.length,1);
    const waiting=await f.entry("cwc refresh");
    assert.equal(waiting.verification_available,false); assert.equal(waiting.options,undefined);
    assert.equal((await f.entry("1",{menu_token:refreshed.menu_token})).status,"BUSINESS");
    assert.equal(f.sends.length,1); assert.equal((await f.row(sent.request_id)).state,"WAITING");
  });
  process.stdout.write(`${checkCount} integration checks passed; real Host calls: 0; synthetic sends: ${syntheticSends}; all state disposable\n`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  const temp = fs.realpathSync.native(os.tmpdir()), resolved = fs.realpathSync.native(ROOT), relative = path.relative(temp, resolved);
  assert.ok(relative.startsWith("cwc-relay-integration-") && !relative.includes(path.sep) && !path.isAbsolute(relative));
  fs.rmSync(resolved, { recursive: true, force: true });
});
