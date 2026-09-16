// Trusted code-mode entrypoint: evaluate this file, then call the returned function.
(async function relay(tools, { controller, inputPath, outputDir, expectedVersion, mode = "send", maxReads = 64, delayMs = 0, onEvent }) {
  const SEND = "mcp__codex_app__send_message_to_thread", READ = "mcp__codex_app__read_thread";
  if (!["send", "read"].includes(mode) || !Number.isInteger(maxReads) || maxReads < (mode === "read" ? 1 : 0) || maxReads > 120 ||
      !Number.isInteger(delayMs) || delayMs < 0 || delayMs > 30000 || (onEvent !== undefined && typeof onEvent !== "function")) throw new Error("Invalid bounded relay options");
  const requireTools = (...names) => {
    for (const name of names) if (typeof tools[name] !== "function") throw new Error(`Required tool unavailable: ${name}`);
  };
  requireTools("exec_command");
  const requireTransport = sending => requireTools("apply_patch", ...(sending ? [SEND] : []), ...(maxReads ? [READ] : []));
  const quote = value => {
    if (typeof value !== "string" || !/^(?:[A-Za-z]:[\\/]|\/)/.test(value) || /[\0\r\n]/.test(value)) throw new Error("Expected an absolute local path");
    return "'" + value.replace(/'/g, "''") + "'";
  };
  [controller, inputPath, outputDir].forEach(quote);
  if (expectedVersion !== undefined && (typeof expectedVersion !== "string" || !/^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$/.test(expectedVersion))) throw new Error("Invalid expected plugin version");
  const stamp = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10), evidence = [];
  let request_id, host_sends = 0, labels = {};
  const events = [];
  const emit = async (phase, message) => {
    const event = { phase, message, request_id, ...labels, at: new Date().toISOString() };
    events.push(event);
    // Progress rendering must not interrupt a claimed exchange.
    if (onEvent && phase !== "WAITING") { try { await onEvent(event); } catch {} }
  };
  const finish = result => ({ ...result, ...labels, request_id: result.request_id || request_id, host_sends, evidence, events, project_complete: false });
  const shell = async cmd => {
    const result = await tools.exec_command({ cmd, max_output_tokens: 100000 });
    if (result.exit_code !== 0 || result.session_id) throw new Error("Local relay step did not finish; inspect state before continuing: " + (result.output || "").slice(0, 1500));
    return result.output;
  };
  const readText = async path => (await shell(`Get-Content -LiteralPath ${quote(path)} -Raw -Encoding UTF8`)).replace(/^\uFEFF/, "").trim();
  const save = async (name, data) => {
    const json = JSON.stringify(data, null, 2);
    // ponytail: cap evidence at 2M UTF-16 units; use streamed local storage if Host results grow beyond this.
    if (!json || json.length > 2 * 1024 * 1024) throw new Error("Relay JSON exceeds the local evidence limit");
    const path = outputDir.replace(/[\\/]+$/, "") + "/" + stamp + "-" + name + ".json";
    const result = await tools.apply_patch("*** Begin Patch\n*** Add File: " + path + "\n+" + json.split("\n").join("\n+") + "\n*** End Patch");
    if (result?.isError || result?.error || await readText(path) !== json) throw new Error("Evidence write failed; do not repeat a Host send");
    evidence.push(path);
    return path;
  };
  const runFile = async path => {
    const version = expectedVersion === undefined ? "" : ` --expected-version '${expectedVersion}'`;
    const raw = await tools.exec_command({ cmd: `& python -X utf8 -B ${quote(controller)} --input ${quote(path)}${version}`, max_output_tokens: 100000 });
    if (raw.session_id) throw new Error("Controller is still running; inspect state before continuing");
    let result;
    try { result = JSON.parse(raw.output); } catch { throw new Error("Controller output is not complete JSON; inspect the existing request"); }
    if (raw.exit_code !== 0 || !result || typeof result !== "object" || Array.isArray(result) || result.status === "ERROR") {
      throw Object.assign(new Error(result?.error || "Invalid controller result"), {
        code: result?.error_code || "LOCAL_ERROR", request_id, host_sends, evidence: [...evidence]
      });
    }
    if (result.question_id) labels = { question_id: result.question_id, answer_id: result.answer_id,
      parent_request_id: result.parent_request_id, parent_answer_id: result.parent_answer_id };
    return result;
  };
  const input = JSON.parse(await readText(inputPath));
  if (!input || typeof input !== "object" || Array.isArray(input) || !input.scope ||
      !(mode === "send" ? ["prepare", "claim", "resume"] : ["read", "resume"]).includes(input.action)) throw new Error("Relay input action does not match its mode");
  if (input.action === "prepare" && (typeof input.body !== "string" || !input.body.trim())) throw new Error("A substantive consultation body is required");
  if (["prepare", "claim"].includes(input.action)) requireTransport(true);
  const act = async (name, data) => runFile(await save(name, { scope: input.scope, request_id, ...data }));
  const identity = value => typeof value === "string" && value.trim().length > 0 && value.length <= 2048 && !/[\x00-\x1f\x7f]/.test(value);
  const request = value => typeof value === "string" && /^req_[0-9a-f]{32}$/.test(value);
  const validate = (packet, sending, expected) => {
    const args = packet.arguments, keys = sending ? ["threadId", "hostId", "prompt"] : ["threadId", "hostId", "turnLimit", "includeOutputs", "maxOutputCharsPerItem", "cursor"];
    if (packet.request_id !== request_id || packet.tool !== (sending ? SEND : READ) || !args || typeof args !== "object" || Array.isArray(args) ||
        !identity(args.threadId) || (Object.hasOwn(args, "hostId") && !identity(args.hostId)) || Object.keys(args).some(key => !keys.includes(key)) ||
        (expected && (args.threadId !== expected.threadId || args.hostId !== expected.hostId))) throw new Error("Host operation does not match the frozen request");
    if (sending ? (typeof args.prompt !== "string" || !args.prompt.startsWith(`CWC REQUEST ${request_id}\n`) || !args.prompt.slice(`CWC REQUEST ${request_id}\n`.length).trim() || args.prompt.length > 180 * 1024) :
        (!Number.isInteger(args.turnLimit) || args.turnLimit < 1 || args.turnLimit > 100 || args.includeOutputs !== true ||
         !Number.isInteger(args.maxOutputCharsPerItem) || args.maxOutputCharsPerItem < 1 || args.maxOutputCharsPerItem > 20000 ||
         (Object.hasOwn(args, "cursor") && (typeof args.cursor !== "string" || args.cursor.length > 4096)))) throw new Error("Invalid Host operation arguments");
    return Object.freeze(args);
  };
  request_id = input.request_id;
  if (request_id !== undefined && (!request(request_id) || input.action === "prepare")) throw new Error("Use resume for an existing request; prepare requires a new semantic question");
  let packet = await runFile(inputPath), expected;
  request_id = request_id || packet.request_id;
  if (packet.status === "IDLE" && !request_id) return finish(packet);
  if (!request(request_id) || (packet.request_id && packet.request_id !== request_id)) throw new Error("Invalid controller request identity");
  if (input.action === "prepare" || (input.action === "resume" && mode === "send" && packet.status === "PREPARED")) {
    if (packet.status !== "PREPARED") throw new Error("Controller did not prepare this request");
    requireTransport(true);
    packet = await act("claim", { action: "claim" });
  }
  if (mode === "send" && packet.status !== "SEND_ONCE" && input.action !== "resume") return finish(packet);
  if (mode === "send" && packet.status === "SEND_ONCE") {
    const called_with = validate(packet, true);
    expected = { threadId: called_with.threadId, hostId: called_with.hostId };
    let result;
    try {
      host_sends++;
      result = await tools[SEND](called_with);
    } catch (error) {
      await save("send-exception", { scope: input.scope, request_id, called_with, exception: { name: String(error.name), message: String(error.message) } });
      return finish({ status: "UNKNOWN", notice: "Host send threw; read the existing request, never resend." });
    }
    const topic = typeof input.body === "string" ? input.body.trim().split(/\r?\n/)[0].slice(0, 160) : "继续原咨询";
    await emit("SEND_ATTEMPTED", `${labels.question_id || request_id}：${topic}。已调用发送工具，正在核对回执。`);
    if (result === undefined) throw new Error("Host send returned no result; inspect the existing request, never resend");
    packet = await act("record-send", { action: "record-send", called_with, result });
    if (!maxReads || !packet.next) {
      const { next, ...outcome } = packet;
      return finish(outcome);
    }
    packet = packet.next;
  }
  const localResult = async result => {
    if (["CONSUMED", "RECOVERED"].includes(result.status)) {
      const state = result.answer_complete === false ? "部分内容已接纳，仍需后续章节，暂不能执行依赖步骤" : result.status === "RECOVERED" ? "已恢复：先核对此前执行效果" : "有效回复已接纳";
      await emit(result.status, `${labels.answer_id || request_id} ${state}。继续检查剩余目标、说明取舍并执行或回传复核。`);
    }
    return finish(result);
  };
  for (let attempt = 0; attempt < maxReads; attempt++) {
    if (packet.status !== "READ_PENDING") return localResult(packet);
    requireTransport(false);
    validate(packet, false, expected);
    while (packet.wait_ms > 0) {
      if (!Number.isInteger(packet.wait_ms) || packet.wait_ms > 300000) throw new Error("Invalid controller read schedule");
      await emit("WAITING", `${labels.question_id || request_id} 等待查收；已等待 ${Math.floor((packet.elapsed_ms || 0) / 1000)} 秒，下一次读取约 ${Math.ceil(packet.wait_ms / 1000)} 秒后。`);
      await new Promise(resolve => setTimeout(resolve, Math.min(packet.wait_ms, 30000)));
      packet = await act(`read-plan-${attempt}-${events.length}`, { action: "read" });
      if (packet.status !== "READ_PENDING") return localResult(packet);
      validate(packet, false, expected);
    }
    // Reserve a due read in the controller, so concurrent resumes share the cadence.
    if (packet.wait_ms !== undefined) {
      packet = await act(`read-start-${attempt}`, { action: "read-start", late_probe: packet.late_probe === true });
      if (packet.status !== "READ_PENDING") return localResult(packet);
      if (packet.wait_ms > 0) continue;
    }
    const called_with = validate(packet, false, expected);
    expected = { threadId: called_with.threadId, hostId: called_with.hostId };
    let result;
    try {
      result = await tools[READ](called_with);
    } catch (error) {
      await save(`read-exception-${attempt}`, { scope: input.scope, request_id, called_with, exception: { name: String(error.name), message: String(error.message) } });
      return finish({ status: "READ_FAILED", notice: "The existing request is retained; no resend occurred." });
    }
    if (result === undefined) throw new Error("Host read returned no result; no answer was accepted");
    const outcome = await act(`accept-${attempt}`, { action: "accept", called_with, result });
    if (outcome.status !== "NO_MATCH" || outcome.reason === "RESPONSE_FORMAT_MISMATCH") return localResult(outcome);
    if (packet.late_probe) return localResult(await act(`after-late-${attempt}`, { action: "read" }));
    await emit("WAITING", `${labels.question_id || request_id} ${outcome.reason === "REPLY_PENDING" ? "回复仍在生成" : "尚无可接纳回复"}，保留原请求继续查收。`);
    if (attempt + 1 === maxReads) return finish(outcome);
    if (delayMs) await new Promise(resolve => setTimeout(resolve, delayMs));
    packet = await act(`read-${attempt + 1}`, { action: "read" });
  }
  return localResult(packet);
})
