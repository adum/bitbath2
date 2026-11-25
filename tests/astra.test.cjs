// Run after scripts/build_web.py. Exercise the shipped worker and both Wasm modules.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("Micro Astra returns orders with the same replies as the live referee", async () => {
  const web = path.resolve(__dirname, "../web");
  let response;
  const worker = vm.createContext({
    location: new URL("https://bitbath.test/reference-worker.js"),
    WebAssembly, URL, TextEncoder, TextDecoder, atob, performance, console,
    postMessage(message) { response = message; },
    fetch(url) {
      assert.equal(new URL(url).pathname, "/astra.wasm");
      return Promise.resolve(new Response(fs.readFileSync(path.join(web, "astra.wasm")), {
        headers: { "Content-Type": "application/wasm" },
      }));
    },
  });
  worker.self = worker;
  worker.importScripts = (...names) => {
    for (const name of names) {
      vm.runInContext(fs.readFileSync(path.join(web, name), "utf8"), worker, { filename: name });
    }
  };
  worker.importScripts("reference-worker.js");
  await worker.onmessage({ data: { action: "load", id: "gpt-6-astra" } });
  assert.ifError(response.error);
  const referee = await vm.runInContext("BitbathEngine.create()", worker);
  const config = fs.readFileSync(path.resolve(__dirname, "../configs/micro.toml"), "utf8");
  const setup = referee.setup(config + "\nseed = 1001");
  referee.load(setup.join("\n"), "");
  await worker.onmessage({ data: { action: "init", setup, lines: referee.initMessage(1) } });
  assert.ifError(response.error);
  referee.push("T 1");
  await worker.onmessage({ data: { action: "turn", steps: ["T 1"], lines: referee.turnMessage(6000, 5000) } });
  assert.ifError(response.error);
  assert.ok(response.orders.length > 0);
  for (const { line, reply } of response.orders) {
    assert.equal(referee.push("O " + line).join("\n"), reply);
    assert.doesNotMatch(reply, /^ERR\b/m);
  }
});
