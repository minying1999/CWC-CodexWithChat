// Trusted entry dispatcher. Exchange sending/reading remains in host_relay.js.
(async function entry(tools, config) {
  const started = Date.now(), metrics = {controller_calls:0, host_list_calls:0, host_list_ms:0};
  const {controller, transport, transport_source_path, expectedVersion, scope, outputDir, command, menu_token, onEvent} = config;
  const quote = value => {
    if (typeof value !== "string" || !/^(?:[A-Za-z]:[\\/]|\/)/.test(value) || /[\0\r\n]/.test(value)) throw new Error("Expected an absolute installed path");
    return "'" + value.replace(/'/g, "''") + "'";
  };
  [controller, transport, transport_source_path, outputDir].forEach(quote);
  if (typeof command !== "string" || !scope || !/^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$/.test(expectedVersion)) throw new Error("Invalid entry arguments");
  if (typeof tools.exec_command !== "function" || typeof tools.apply_patch !== "function") throw new Error("CWC entry requires local file/command tools");
  const stamp = Date.now().toString(36) + "-" + Math.random().toString(36).slice(2,10);
  const readText = async path => {
    const result = await tools.exec_command({cmd:`Get-Content -LiteralPath ${quote(path)} -Raw -Encoding UTF8`,max_output_tokens:20000});
    if (result.exit_code !== 0 || result.session_id) throw new Error("CWC entry file read failed");
    return result.output.replace(/^\uFEFF/, "").trim();
  };
  const act = async data => {
    const input = {scope,expected_version:expectedVersion,...data};
    const json = JSON.stringify(input,null,2);
    if (json.length > 2*1024*1024) throw new Error("Entry input exceeds local evidence limit");
    const file = outputDir.replace(/[\\/]+$/,"") + "/" + stamp + "-" + metrics.controller_calls++ + ".json";
    const saved = await tools.apply_patch("*** Begin Patch\n*** Add File: " + file + "\n+" + json.split("\n").join("\n+") + "\n*** End Patch");
    if (saved?.isError || saved?.error || await readText(file) !== json) throw new Error("Entry input was not saved exactly");
    const result = await tools.exec_command({cmd:`& python -X utf8 -B ${quote(controller)} --input ${quote(file)}`,max_output_tokens:20000});
    if (result.session_id) throw new Error("Entry controller still running; inspect before retrying");
    const packet = JSON.parse(result.output);
    if (result.exit_code !== 0 || packet.status === "ERROR") {
      const detail = data.result?.isError ? data.result.content?.find(x=>x.type==="text")?.text : packet.error;
      throw new Error((data.action === "listed" || data.action === "selected" ? "候选列表读取/校验失败：" : "CWC入口失败：") + String(detail || "Unknown error").slice(0,1000));
    }
    return packet;
  };
  const list = async () => {
    if (typeof tools.mcp__codex_app__list_threads !== "function") throw new Error("Official Chat listing is unavailable");
    const at = Date.now(); metrics.host_list_calls++;
    const result = await tools.mcp__codex_app__list_threads({limit:50});
    metrics.host_list_ms += Date.now() - at;
    return result;
  };
  let result = await act({action:"command",command,menu_token,known_hashes:config.known_hashes,full_resources:config.full_resources});
  if (result.status === "NEED_LIST") {
    result = await act({action:"listed",expected_binding:result.expected_binding,result:await list()});
  } else if (result.status === "VALIDATE_SELECTION") {
    result = await act({action:"selected",selection:result.selection,menu_token:result.menu_token,result:await list()});
  } else if (result.status === "VERIFY_REQUEST") {
    if (onEvent) { try { await onEvent({phase:"VERIFICATION_PENDING",message:result.message}); } catch {} }
    const relay = (0,eval)(await readText(transport_source_path));
    const observed = await relay(tools,{controller:transport,expectedVersion,inputPath:result.input_path,
      outputDir,mode:"send",maxReads:config.maxReads ?? 64,onEvent});
    const verified = ["CONSUMED","RECOVERED"].includes(observed.status) && observed.answer_complete === true;
    result = {status:verified ? "VERIFIED" : "VERIFICATION_PENDING",
      message:verified ? "通信核验通过。" : "绑定已保存，通信尚未核验；保留原请求继续查收。",
      question_id:observed.question_id,answer_id:observed.answer_id,request_id:observed.request_id,
      host_sends:observed.host_sends,exchange_status:observed.status,evidence:observed.evidence};
  }
  return {...result,timings:{...metrics,entry_ms:Date.now()-started}};
})
