const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const script = fs.readFileSync(path.join(__dirname, "../scripts/export_session.js"), "utf8");

function storage(items) {
  const entries = Object.entries(items);
  return { length: entries.length, key: (index) => entries[index][0], getItem: (key) => items[key] };
}

function context(result) {
  const captured = { requests: [], copied: null, errors: 0 };
  return {
    captured,
    globals: {
      location: { origin: "https://sports.sjtu.edu.cn" },
      document: { cookie: "preference=example" },
      localStorage: storage({ example: "示例" }),
      sessionStorage: storage({ token: "EXAMPLE-SESSION-ID" }),
      fetch: async (url, options) => {
        captured.requests.push({ url, options });
        return { ok: true, json: async () => result };
      },
      copy: (text) => { captured.copied = text; },
      console: { info: () => {}, error: () => { captured.errors += 1; } },
    },
  };
}

test("exports only after server confirmation and preserves browser storage", async () => {
  const scenario = context({ code: 0, data: { loginName: "example-user", params: { JSESSIONID: "EXAMPLE-SESSION-ID" } } });
  await vm.runInNewContext(script, scenario.globals);
  const exported = JSON.parse(scenario.captured.copied);
  assert.equal(exported.session_storage.token, "EXAMPLE-SESSION-ID");
  assert.equal(exported.local_storage.example, "示例");
  assert.equal(exported.server_session_id, "EXAMPLE-SESSION-ID");
  assert.equal(exported.loginName, undefined);
  assert.equal(scenario.captured.requests.length, 1);
  assert.equal(scenario.captured.requests[0].url, "https://sports.sjtu.edu.cn/system/user/currentUser");
  assert.equal(scenario.captured.requests[0].options.redirect, "error");
});

test("does not copy an unauthenticated session", async () => {
  const scenario = context({ code: 401, data: null });
  await vm.runInNewContext(script, scenario.globals);
  assert.equal(scenario.captured.copied, null);
  assert.equal(scenario.captured.errors, 1);
});

test("does not make requests on another website", async () => {
  const scenario = context({ code: 0, data: { loginName: "example-user" } });
  scenario.globals.location.origin = "https://unrelated.example";
  await vm.runInNewContext(script, scenario.globals);
  assert.equal(scenario.captured.requests.length, 0);
  assert.equal(scenario.captured.copied, null);
});
