import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const html = fs.readFileSync("data/智拓商机作战助手-开发版.html", "utf8");
const start = html.indexOf("async function downloadSharedDocument(");
const end = html.indexOf("async function documentVersionsModal(", start);
assert.ok(start >= 0 && end > start, "download function must exist");
const source = html.slice(start, end);

async function run(refreshSucceeds) {
  let token = "expired";
  const calls = [];
  const context = {
    BACKEND_API_BASE: "http://localhost/api/v1",
    ACCESS_TOKEN_KEY: "access-token",
    sessionStorage: {getItem: () => token},
    refreshBackendAccessToken: async () => {
      if (refreshSucceeds) token = "refreshed";
      return refreshSucceeds;
    },
    fetch: async (_url, options) => {
      calls.push(options.headers.Authorization);
      return calls.length === 1
        ? {status: 401, ok: false}
        : {status: 200, ok: true, blob: async () => new Uint8Array([1])};
    },
    URL: {createObjectURL: () => "blob:test", revokeObjectURL: () => {}},
    document: {createElement: () => ({click: () => {}})},
    setTimeout: () => {},
  };
  const promise = vm.runInNewContext(`${source}\ndownloadSharedDocument("doc", "demo.txt")`, context);
  if (refreshSucceeds) {
    await promise;
    assert.deepEqual(calls, ["Bearer expired", "Bearer refreshed"]);
  } else {
    await assert.rejects(promise, /登录已过期/);
    assert.deepEqual(calls, ["Bearer expired"]);
  }
}

await run(true);
await run(false);
console.log("Document download refresh tests passed");
