"use strict";

// Run with Node. No browser, application server, filesystem mutation, or network.
// The actual app.js runs inside a VM with a deliberately small DOM and fetch stub.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const markup = fs.readFileSync(path.join(root, "web/index.html"), "utf8");
const source = fs.readFileSync(path.join(root, "web/app.js"), "utf8");
const script = new vm.Script(source, { filename: "web/app.js" });
const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });

function harness(jobs = [], theme = "light") {
  let document;
  class Element {
    constructor(tag = "div") {
      this.tagName = tag.toLowerCase();
      this.children = [];
      this.dataset = {};
      this.attrs = {};
      this.listeners = {};
      this.className = "";
      this.value = "";
      this.textContent = "";
      this.disabled = false;
      this.hidden = false;
      this.checked = false;
      this.open = false;
      this.classList = {
        add: (...values) => { this.className = [...new Set([...this.className.split(" "), ...values])].filter(Boolean).join(" "); },
        toggle: (value, force) => {
          const values = new Set(this.className.split(" ").filter(Boolean));
          const next = force === undefined ? !values.has(value) : force;
          if (next) values.add(value); else values.delete(value);
          this.className = [...values].join(" ");
          return next;
        },
      };
    }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    removeAttribute(key) { delete this.attrs[key]; }
    append(...nodes) { nodes.forEach((node) => { node.parent = this; this.children.push(node); }); }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
    async fire(type) {
      const event = { target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      await Promise.all((this.listeners[type] || []).map((handler) => handler(event)));
      return event;
    }
    matches(selector) {
      if (selector.startsWith(".")) return this.className.split(" ").includes(selector.slice(1));
      if (selector === "input:checked") return this.tagName === "input" && this.checked;
      return this.tagName === selector;
    }
    querySelectorAll(selector) {
      return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector) || null; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  const ids = new Map();
  for (const match of markup.matchAll(/<([\w-]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const node = new Element(match[1]);
    node.id = match[3];
    node.className = /\bclass="([^"]*)"/.exec(match[2])?.[1] || "";
    node.hidden = /\bhidden\b/.test(match[2]);
    node.disabled = /\bdisabled\b/.test(match[2]);
    ids.set(node.id, node);
  }
  const $ = (id) => { assert(ids.has(id), `Unexpected DOM id ${id}`); return ids.get(id); };
  $("download-button").append(new Element("span"));
  const radios = ["lichess", "chess.com"].map((value) => Object.assign(new Element("input"), { value, checked: value === "lichess" }));
  const nav = ["download", "history", "settings"].map((view) => Object.assign(new Element("button"), { dataset: { view } }));
  const brand = new Element("a");
  document = {
    documentElement: Object.assign(new Element("html"), { dataset: { theme: "light" } }),
    activeElement: null,
    hidden: false,
    getElementById: $,
    createElement: (tag) => new Element(tag),
    createElementNS: (_, tag) => new Element(tag),
    createTextNode: (text) => Object.assign(new Element("#text"), { textContent: text }),
    addEventListener() {},
    querySelector(selector) { if (selector === ".brand") return brand; throw new Error(`Unexpected selector ${selector}`); },
    querySelectorAll(selector) {
      if (selector === "input[name=server]") return radios;
      if (selector === "[data-view]") return nav;
      if (selector === ".view") return [$("view-download"), $("view-history"), $("view-settings")];
      if (selector === "#mode-options input:checked") return $("mode-options").querySelectorAll("input:checked");
      if (["[data-go-download]", "[data-browse]", "[data-browse], [data-native]"].includes(selector)) return [];
      throw new Error(`Unexpected selector ${selector}`);
    },
  };
  const bootstrap = {
    settings: { theme, output_dir: "C:\\OfflineFixture", last_request: { username: "offline-player", color: "white", since: "2024", until: "2025" } },
    jobs,
    paths: { data_dir: "C:\\OfflineFixture\\data" },
  };
  const expected = [{ method: "GET", url: "/api/bootstrap", value: response(bootstrap) }];
  const requests = [];
  const unexpected = [];
  const timers = new Map();
  let nextTimer = 0;
  const sandbox = {
    document,
    window: { addEventListener() {} },
    location: { hash: "#token=offline-stub-token", pathname: "/", search: "" },
    history: { replaceState() {} },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    URLSearchParams,
    AbortController,
    setTimeout(callback, delay) { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      const method = options.method || "GET";
      const call = { url, method, body: options.body };
      requests.push(call);
      // No real fetch is exposed. Even an app bug can only reach this stub.
      const item = expected.shift();
      if (!item || item.url !== url || item.method !== method || (url === "/api/jobs" && method === "POST") || !url.startsWith("/api/")) {
        unexpected.push(call);
        return Promise.reject(new Error(`Forbidden or unexpected stub request ${method} ${url}`));
      }
      return Promise.resolve(item.value);
    },
  };
  script.runInNewContext(sandbox, { timeout: 2000 });
  return {
    $, document, radios, requests,
    expect(method, url, value) { expected.push({ method, url, value }); },
    poll() {
      const timer = [...timers.entries()].find(([, item]) => item.callback.name === "pollJobs");
      assert(timer, "Expected polling timer");
      timers.delete(timer[0]);
      return timer[1].callback();
    },
    verify() { assert.deepEqual(unexpected, [], "No unexpected or live requests"); assert.equal(expected.length, 0, "All stub requests consumed"); },
  };
}

const completed = {
  id: "fixture-completed", username: "offline-player", server: "lichess", status: "completed", games: 5,
  output_path: "C:\\OfflineFixture\\saved.pgn", created_at: "2026-09-03T00:00:00Z", modes: ["rapid"],
};

async function main() {
  let checks = 0;
  {
    const h = harness([completed]);
    await settle();
    const before = h.requests.length;
    h.$("filename").value = "keep.pgn";
    await h.$("clear-form").fire("click");
    assert.equal(h.$("username").value, "");
    assert.equal(h.$("mode-options").querySelectorAll("input:checked").length, 0);
    await h.radios[1].fire("change");
    assert.equal(h.$("mode-options").querySelectorAll("input:checked").length, 0, "Clear affects other platform too");
    await h.radios[0].fire("change");
    assert.equal(h.$("mode-options").querySelectorAll("input:checked").length, 0);
    assert.equal(h.$("color").value, "white");
    assert.equal(h.$("since").value, "2024");
    assert.equal(h.$("until").value, "2025");
    assert.equal(h.$("output-dir").value, "C:\\OfflineFixture");
    assert.equal(h.$("filename").value, "keep.pgn");
    assert.equal(h.$("history-count").textContent, "1");
    await h.$("download-form").fire("submit");
    assert.equal(h.requests.length, before, "Form clear and invalid submit make no request");
    await h.$("confirm-clear-history").fire("click");
    await h.$("clear-history").fire("click");
    assert.equal(h.$("clear-history-dialog").open, true);
    await h.$("cancel-clear-history").fire("click");
    assert.equal(h.$("clear-history-dialog").open, false);
    assert.equal(h.requests.length, before, "Opening/cancelling history makes no DELETE");
    h.verify();
    checks += 2;
  }
  {
    const h = harness([completed]);
    await settle();
    const pending = deferred();
    h.expect("PUT", "/api/appearance", pending.promise);
    const save = h.$("theme-toggle").fire("click");
    assert.equal(h.document.documentElement.dataset.theme, "dark");
    assert.equal(h.$("theme-toggle").disabled, true);
    await h.$("theme-toggle").fire("click");
    assert.equal(h.requests.length, 2, "Duplicate theme click does not send another request");
    assert.deepEqual(JSON.parse(h.requests[1].body), { theme: "dark" });
    pending.resolve(response({ theme: "dark" }));
    await save;
    assert.equal(h.$("theme-toggle").disabled, false);
    assert.equal(h.$("theme-toggle").attrs["aria-pressed"], "true");
    h.expect("PUT", "/api/appearance", response({ detail: "Fixture save failure" }, 500));
    await h.$("theme-toggle").fire("click");
    assert.equal(h.document.documentElement.dataset.theme, "dark", "Failed save reverts theme");
    assert.equal(h.$("theme-toggle").disabled, false);
    assert.match(h.$("toast").textContent, /Could not save/);
    h.verify();
    const reload = harness([], "dark");
    await settle();
    assert.equal(reload.document.documentElement.dataset.theme, "dark", "Theme restored from bootstrap settings");
    reload.verify();
    checks += 3;
  }
  {
    const h = harness([completed]);
    await settle();
    const stale = deferred();
    h.expect("GET", "/api/jobs", stale.promise);
    const oldPoll = h.poll();
    await h.$("clear-history").fire("click");
    const deletion = deferred();
    h.expect("DELETE", "/api/jobs", deletion.promise);
    const clearing = h.$("confirm-clear-history").fire("click");
    assert.equal(h.$("download-button").disabled, true);
    assert.equal(h.$("confirm-clear-history").disabled, true);
    assert.equal(h.$("cancel-clear-history").disabled, true);
    await h.$("confirm-clear-history").fire("click");
    await h.$("cancel-clear-history").fire("click");
    assert.equal(h.$("clear-history-dialog").open, true, "Cannot dismiss pending deletion");
    assert.equal((await h.$("clear-history-dialog").fire("cancel")).defaultPrevented, true);
    assert.equal(h.requests.filter((request) => request.method === "DELETE").length, 1);
    deletion.resolve(response({ cleared: 1, jobs: [] }));
    await clearing;
    assert.equal(h.$("clear-history-dialog").open, false);
    assert.equal(h.$("history-list").children.length, 0);
    assert.equal(h.$("activity-detail").hidden, true);
    stale.resolve(response([completed]));
    await oldPoll;
    assert.equal(h.$("history-count").textContent, "0", "Old GET cannot restore removed history");
    assert.equal(h.$("history-list").children.length, 0);
    assert.equal(h.$("clear-history").disabled, true);
    assert.equal(h.$("download-button").disabled, false);
    assert.equal(h.$("username").value, "offline-player", "History clear preserves form");
    h.verify();
    checks += 2;
  }
  for (const status of ["queued", "running", "waiting"]) {
    const h = harness([{ ...completed, status }]);
    await settle();
    assert.equal(h.$("clear-history").disabled, true);
    assert.equal(h.$("confirm-clear-history").disabled, true);
    await h.$("clear-history").fire("click");
    assert.equal(h.$("clear-history-dialog").open, false);
    await h.$("confirm-clear-history").fire("click");
    assert.equal(h.requests.length, 1, "Active job makes no DELETE");
    h.verify();
    checks += 1;
  }
  {
    const h = harness([completed]);
    await settle();
    await h.$("clear-history").fire("click");
    h.expect("GET", "/api/jobs", response([{ ...completed, status: "waiting" }]));
    await h.poll();
    assert.equal(h.$("confirm-clear-history").disabled, true, "Job becoming active blocks open dialog confirmation");
    await h.$("confirm-clear-history").fire("click");
    h.verify();
    checks += 1;
  }
  {
    const h = harness([completed]);
    await settle();
    await h.$("clear-history").fire("click");
    h.expect("DELETE", "/api/jobs", response({ detail: "Fixture deletion failure" }, 500));
    await h.$("confirm-clear-history").fire("click");
    assert.equal(h.$("clear-history-dialog").open, true);
    assert.equal(h.$("history-list").children.length, 1);
    assert.equal(h.$("clear-history-error").hidden, false);
    assert.match(h.$("clear-history-error").textContent, /Fixture deletion failure/);
    assert.equal(h.$("confirm-clear-history").disabled, false);
    assert.equal(h.$("cancel-clear-history").disabled, false);
    h.verify();
    checks += 1;
  }
  console.log(`${checks} offline frontend scenarios passed. Every fetch was stubbed; no application, browser, or network was used.`);
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
