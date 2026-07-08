import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import http from "node:http";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const url = process.env.CODEXHUB_PERF_URL ?? "http://127.0.0.1:1420/";
const iterations = Number.parseInt(process.env.CODEXHUB_PERF_ITERATIONS ?? "24", 10);
const warmupDiscard = Number.parseInt(process.env.CODEXHUB_PERF_WARMUP_DISCARD ?? "2", 10);
const dwellMs = Number.parseInt(process.env.CODEXHUB_PERF_DWELL_MS ?? "0", 10);
const initialSettleMs = Number.parseInt(process.env.CODEXHUB_PERF_INITIAL_SETTLE_MS ?? "2000", 10);
const inputMode = process.env.CODEXHUB_PERF_INPUT ?? "dom";
const manualDurationMs = Number.parseInt(process.env.CODEXHUB_PERF_MANUAL_MS ?? "45000", 10);
const mode = process.env.CODEXHUB_PERF_MODE ?? "browser";
const headless = process.env.CODEXHUB_PERF_HEADLESS !== "0";
const appPath = resolve(
  process.cwd(),
  process.env.CODEXHUB_PERF_APP_PATH ?? "../src-tauri/target/release/codexhub.exe",
);
const outputPath = resolve(
  process.cwd(),
  process.env.CODEXHUB_PERF_OUTPUT ?? "../output/perf/tab-switch-latest.json",
);
const defaultMouseHookOutputPath = outputPath.toLowerCase().endsWith(".json")
  ? `${outputPath.slice(0, -5)}.mouse.json`
  : `${outputPath}.mouse.json`;
const mouseHookOutputPath = resolve(
  process.cwd(),
  process.env.CODEXHUB_PERF_MOUSE_HOOK_OUTPUT ?? defaultMouseHookOutputPath,
);
const mouseHookScriptPath = resolve(process.cwd(), "scripts/perf-mouse-hook.ps1");
const osMouseHookEnabled = (
  process.platform === "win32" &&
  mode === "app" &&
  inputMode === "manual" &&
  process.env.CODEXHUB_PERF_OS_HOOK !== "0"
);

const browserCandidates = [
  process.env.CODEXHUB_CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);

function percentile(values, p) {
  if (values.length === 0) {
    return 0;
  }
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[index];
}

function summarize(samples, field) {
  const values = samples
    .map((sample) => sample[field])
    .filter((value) => typeof value === "number" && Number.isFinite(value));
  return {
    avg: values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length),
    max: Math.max(0, ...values),
    p50: percentile(values, 50),
    p95: percentile(values, 95),
  };
}

function startOsMouseHook(durationMs) {
  if (!existsSync(mouseHookScriptPath)) {
    throw new Error(`OS mouse hook script was not found: ${mouseHookScriptPath}`);
  }
  const powershellPath = process.env.CODEXHUB_POWERSHELL_PATH ?? "powershell.exe";
  return spawn(
    powershellPath,
    [
      "-NoProfile",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      mouseHookScriptPath,
      "-OutputPath",
      mouseHookOutputPath,
      "-DurationMs",
      String(durationMs),
    ],
    { stdio: "ignore" },
  );
}

async function waitForProcessExit(childProcess, timeoutMs = 3000) {
  if (!childProcess || childProcess.exitCode !== null) {
    return;
  }
  await new Promise((resolve) => {
    const timeout = setTimeout(resolve, timeoutMs);
    childProcess.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

async function readMouseHookEvents() {
  if (!existsSync(mouseHookOutputPath)) {
    return [];
  }
  const raw = await readFile(mouseHookOutputPath, "utf8");
  const parsed = JSON.parse(raw.replace(/^\uFEFF/, ""));
  return Array.isArray(parsed) ? parsed : [];
}

function annotateSamplesWithOsMouse(samples, mouseEvents) {
  const mouseDownEvents = mouseEvents
    .filter((event) => (
      event?.type === "down" &&
      typeof event.epochMs === "number" &&
      Number.isFinite(event.epochMs)
    ))
    .sort((left, right) => left.epochMs - right.epochMs);
  const matchCounts = new Map();
  return samples.map((sample) => {
    if (typeof sample.pointerEpochMs !== "number" || !Number.isFinite(sample.pointerEpochMs)) {
      return sample;
    }
    const screenX = typeof sample.pointerScreenX === "number" ? sample.pointerScreenX : null;
    const screenY = typeof sample.pointerScreenY === "number" ? sample.pointerScreenY : null;
    const candidates = mouseDownEvents
      .map((event, index) => {
        const osToPointerMs = sample.pointerEpochMs - event.epochMs;
        if (osToPointerMs < -80 || osToPointerMs > 5000) {
          return null;
        }
        const coordinateDistancePx = screenX === null || screenY === null
          ? null
          : Math.hypot(event.x - screenX, event.y - screenY);
        if (coordinateDistancePx !== null && coordinateDistancePx > 80) {
          return null;
        }
        return {
          coordinateDistancePx,
          event,
          index,
          osToPointerMs,
          score: Math.abs(osToPointerMs) + (coordinateDistancePx ?? 0) * 4,
        };
      })
      .filter(Boolean)
      .sort((left, right) => left.score - right.score);
    const match = candidates[0];
    if (!match) {
      return { ...sample, osToPointerMs: null };
    }
    const mouseDown = match.event;
    const priorUseCount = matchCounts.get(match.index) ?? 0;
    matchCounts.set(match.index, priorUseCount + 1);
    return {
      ...sample,
      osMouseCallbackEpochMs: mouseDown.callbackEpochMs ?? null,
      osMouseDownEpochMs: mouseDown.epochMs,
      osMouseFlags: mouseDown.flags,
      osMouseMatchDistancePx: match.coordinateDistancePx,
      osMouseReused: priorUseCount > 0,
      osMouseX: mouseDown.x,
      osMouseY: mouseDown.y,
      osToPointerMs: match.osToPointerMs,
    };
  });
}

function findBrowserPath() {
  const candidate = browserCandidates.find((item) => item && existsSync(item));
  if (!candidate) {
    throw new Error("Chrome or Edge was not found. Set CODEXHUB_CHROME_PATH to a Chromium browser executable.");
  }
  return candidate;
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close(() => {
        if (!port) {
          reject(new Error("Failed to allocate a local debug port."));
          return;
        }
        resolve(port);
      });
    });
  });
}

function getJson(targetUrl) {
  return new Promise((resolve, reject) => {
    const request = http.get(targetUrl, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`GET ${targetUrl} failed with ${response.statusCode}: ${body}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(error);
        }
      });
    });
    request.on("error", reject);
    request.setTimeout(1000, () => {
      request.destroy(new Error(`GET ${targetUrl} timed out`));
    });
  });
}

async function waitForJson(targetUrl, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      return await getJson(targetUrl);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }
  throw lastError ?? new Error(`Timed out waiting for ${targetUrl}`);
}

async function waitForPageTarget(port, timeoutMs = 15_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const targets = await waitForJson(`http://127.0.0.1:${port}/json/list`, 1000).catch(() => []);
    const pageTarget = targets.find((target) => (
      target.type === "page" &&
      target.webSocketDebuggerUrl &&
      !String(target.url ?? "").startsWith("devtools://")
    ));
    if (pageTarget) {
      return pageTarget;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for a CDP page target on port ${port}.`);
}

class CdpClient {
  constructor(wsUrl) {
    this.nextId = 1;
    this.pending = new Map();
    this.ws = new WebSocket(wsUrl);
  }

  async open() {
    if (this.ws.readyState === WebSocket.OPEN) {
      return;
    }
    await new Promise((resolve, reject) => {
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onError = (event) => {
        cleanup();
        reject(event.error ?? new Error("WebSocket connection failed"));
      };
      const cleanup = () => {
        this.ws.removeEventListener("open", onOpen);
        this.ws.removeEventListener("error", onError);
      };
      this.ws.addEventListener("open", onOpen);
      this.ws.addEventListener("error", onError);
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { reject, resolve } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) {
          reject(new Error(`${message.error.message}: ${message.error.data ?? ""}`));
          return;
        }
        resolve(message.result);
      }
    });
  }

  send(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) {
      payload.sessionId = sessionId;
    }
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { reject, resolve });
    });
    this.ws.send(JSON.stringify(payload));
    return promise;
  }

  close() {
    this.ws.close();
  }
}

function metricsByName(metrics) {
  return Object.fromEntries(metrics.map((metric) => [metric.name, metric.value]));
}

async function evaluate(cdp, sessionId, expression) {
  const result = await cdp.send(
    "Runtime.evaluate",
    {
      awaitPromise: true,
      expression,
      returnByValue: true,
    },
    sessionId,
  );
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text ?? "Runtime.evaluate failed");
  }
  return result.result.value;
}

async function dispatchMouseClick(cdp, x, y) {
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
}

async function waitForAppTabs(cdp, sessionId) {
  const deadline = Date.now() + 20_000;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      return await evaluate(
        cdp,
        sessionId,
        `new Promise((resolve, reject) => {
          const deadline = performance.now() + 3000;
          const check = () => {
            const ready = document.readyState !== "loading";
            const hasTabs = document.querySelectorAll("nav button").length >= 2;
            if (ready && hasTabs) {
              resolve(true);
              return;
            }
            if (performance.now() > deadline) {
              reject(new Error("Timed out waiting for app tabs"));
              return;
            }
            requestAnimationFrame(check);
          };
          check();
        })`,
      );
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  throw lastError ?? new Error("Timed out waiting for app tabs");
}

async function main() {
  if (typeof WebSocket === "undefined") {
    throw new Error("This script requires a Node.js runtime with global WebSocket support.");
  }

  const port = await getFreePort();
  let browserPath = null;
  let profileDir = null;
  let launchedProcess = null;
  let mouseHookProcess = null;
  let mouseHookEvents = [];
  let mouseHookError = null;
  let targetInfo = null;

  if (mode === "app") {
    if (!existsSync(appPath)) {
      throw new Error(`CodexHub executable was not found: ${appPath}`);
    }
    const webviewArgs = [
      process.env.WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS,
      `--remote-debugging-port=${port}`,
      "--remote-allow-origins=*",
    ].filter(Boolean).join(" ");
    launchedProcess = spawn(appPath, [], {
      env: {
        ...process.env,
        WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: webviewArgs,
      },
      stdio: "ignore",
    });
  } else {
    browserPath = findBrowserPath();
    profileDir = await mkdtemp(join(tmpdir(), "codexhub-perf-profile-"));
    const browserArgs = [
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profileDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-extensions",
      "--disable-sync",
      "--window-size=1280,820",
      headless ? "--headless=new" : undefined,
      "about:blank",
    ].filter(Boolean);
    launchedProcess = spawn(browserPath, browserArgs, { stdio: "ignore" });
  }

  let cdp;
  try {
    targetInfo = await waitForPageTarget(port);
    cdp = new CdpClient(targetInfo.webSocketDebuggerUrl);
    await cdp.open();
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");
    await cdp.send("Performance.enable");
    if (mode !== "app") {
      await cdp.send("Page.navigate", { url });
    }
    await waitForAppTabs(cdp, undefined);
    if (initialSettleMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, initialSettleMs));
    }

    const beforeMetrics = metricsByName((await cdp.send("Performance.getMetrics")).metrics);
    let samples;
    if (inputMode === "manual") {
      if (osMouseHookEnabled) {
        mouseHookProcess = startOsMouseHook(manualDurationMs + 1000);
        console.log(`Manual recording armed with OS mouse hook for ${manualDurationMs}ms.`);
      } else {
        console.log(`Manual recording armed for ${manualDurationMs}ms.`);
      }
      samples = await evaluate(
        cdp,
        undefined,
        `(async () => {
          const durationMs = ${JSON.stringify(manualDurationMs)};
          const samples = [];
          const eventTimings = [];
          const longTasks = [];
          const longAnimationFrames = [];
          const frameGaps = [];
          let active = null;
          let lastFrame = performance.now();
          let rafRunning = true;
          const labelForButton = (button) => {
            const text = (button.textContent || "").trim().toLowerCase();
            if (text.includes("gateway")) {
              return "gateway";
            }
            if (text.includes("codexhub") || text.includes("codex hub")) {
              return "codexhub";
            }
            return null;
          };
          const finishActive = (end) => {
            if (!active) {
              return;
            }
            const activeLongTasks = longTasks.filter((task) => task.startTime >= active.start && task.startTime <= end);
            const activeEventTimings = eventTimings.filter((entry) => (
              entry.startTime >= active.start - 100 &&
              entry.startTime <= end &&
              ["click", "mousedown", "mouseup", "pointerdown", "pointerup"].includes(entry.name)
            ));
            const activeLongAnimationFrames = longAnimationFrames.filter((frame) => (
              frame.startTime >= active.start - 100 &&
              frame.startTime <= end
            ));
            const activeFrameGaps = frameGaps.filter((gap) => gap.startTime >= active.start && gap.startTime <= end);
            samples.push({
              dwellFrameCount: activeFrameGaps.length,
              dwellMaxFrameGapMs: Math.max(0, ...activeFrameGaps.map((gap) => gap.duration)),
              eventTimingCount: activeEventTimings.length,
              eventTimingInputDelayMaxMs: Math.max(0, ...activeEventTimings.map((entry) => entry.inputDelay)),
              eventTimingMaxMs: Math.max(0, ...activeEventTimings.map((entry) => entry.duration)),
              eventTimingPresentationDelayMaxMs: Math.max(0, ...activeEventTimings.map((entry) => entry.presentationDelay)),
              eventTimingProcessingMaxMs: Math.max(0, ...activeEventTimings.map((entry) => entry.processingTime)),
              frame1Ms: null,
              frame2Ms: null,
              input: "manual",
              longAnimationFrameCount: activeLongAnimationFrames.length,
              longAnimationFrameMaxMs: Math.max(0, ...activeLongAnimationFrames.map((frame) => frame.duration)),
              longTaskCount: activeLongTasks.length,
              longTaskMaxMs: Math.max(0, ...activeLongTasks.map((task) => task.duration)),
              pointerButton: active.pointerButton,
              pointerButtons: active.pointerButtons,
              pointerClientX: active.pointerClientX,
              pointerClientY: active.pointerClientY,
              pointerEpochMs: active.pointerEpochMs,
              pointerId: active.pointerId,
              pointerIsTrusted: active.pointerIsTrusted,
              pointerScreenX: active.pointerScreenX,
              pointerScreenY: active.pointerScreenY,
              pointerTimeStamp: active.pointerTimeStamp,
              pointerType: active.pointerType,
              sampleMs: end - active.start,
              target: active.target,
              targetText: active.targetText,
              visibleMs: active.visibleAt === null ? null : active.visibleAt - active.start,
            });
            active = null;
          };
          const updateVisible = () => {
            if (!active || active.visibleAt !== null) {
              return;
            }
            const pane = document.querySelector('[data-tab-pane="' + active.target + '"]');
            if (pane?.getAttribute("aria-hidden") === "false") {
              active.visibleAt = performance.now();
            }
          };
          const performanceObservers = [];
          const supportedEntryTypes = PerformanceObserver.supportedEntryTypes || [];
          if ("PerformanceObserver" in window) {
            try {
              const longTaskObserver = new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                  longTasks.push({
                    duration: entry.duration,
                    name: entry.name,
                    startTime: entry.startTime,
                  });
                }
              });
              longTaskObserver.observe({ entryTypes: ["longtask"] });
              performanceObservers.push(longTaskObserver);
            } catch {}
            if (supportedEntryTypes.includes("event")) {
              try {
                const eventObserver = new PerformanceObserver((list) => {
                  for (const entry of list.getEntries()) {
                    const processingTime = Math.max(0, entry.processingEnd - entry.processingStart);
                    const inputDelay = Math.max(0, entry.processingStart - entry.startTime);
                    eventTimings.push({
                      duration: entry.duration,
                      inputDelay,
                      interactionId: entry.interactionId || 0,
                      name: entry.name,
                      presentationDelay: Math.max(0, entry.duration - inputDelay - processingTime),
                      processingTime,
                      startTime: entry.startTime,
                    });
                  }
                });
                eventObserver.observe({ type: "event", buffered: true, durationThreshold: 16 });
                performanceObservers.push(eventObserver);
              } catch {}
            }
            if (supportedEntryTypes.includes("long-animation-frame")) {
              try {
                const longAnimationFrameObserver = new PerformanceObserver((list) => {
                  for (const entry of list.getEntries()) {
                    longAnimationFrames.push({
                      duration: entry.duration,
                      renderStart: entry.renderStart || 0,
                      startTime: entry.startTime,
                      styleAndLayoutStart: entry.styleAndLayoutStart || 0,
                    });
                  }
                });
                longAnimationFrameObserver.observe({ type: "long-animation-frame", buffered: true });
                performanceObservers.push(longAnimationFrameObserver);
              } catch {}
            }
          }
          const frameLoop = (now) => {
            frameGaps.push({ duration: now - lastFrame, startTime: lastFrame });
            lastFrame = now;
            if (rafRunning) {
              requestAnimationFrame(frameLoop);
            }
          };
          requestAnimationFrame(frameLoop);
          const onPointerDown = (event) => {
            if (!event.isTrusted || event.pointerType !== "mouse" || event.button !== 0) {
              return;
            }
            const button = event.target instanceof Element ? event.target.closest("nav button") : null;
            if (!button) {
              return;
            }
            const target = labelForButton(button);
            if (!target) {
              return;
            }
            const now = performance.now();
            finishActive(now);
            active = {
              pointerButton: event.button,
              pointerButtons: event.buttons,
              pointerClientX: event.clientX,
              pointerClientY: event.clientY,
              pointerEpochMs: Date.now(),
              pointerId: event.pointerId,
              pointerIsTrusted: event.isTrusted,
              pointerScreenX: event.screenX,
              pointerScreenY: event.screenY,
              pointerTimeStamp: event.timeStamp,
              pointerType: event.pointerType,
              start: now,
              target,
              targetText: (button.textContent || "").trim(),
              visibleAt: null,
            };
            updateVisible();
          };
          document.addEventListener("pointerdown", onPointerDown, true);
          const observer = new MutationObserver(updateVisible);
          document.querySelectorAll("[data-tab-pane]").forEach((pane) => {
            observer.observe(pane, { attributes: true, attributeFilter: ["aria-hidden"] });
          });
          await new Promise((resolve) => setTimeout(resolve, durationMs));
          const end = performance.now();
          finishActive(end);
          rafRunning = false;
          performanceObservers.forEach((observer) => observer.disconnect());
          observer.disconnect();
          document.removeEventListener("pointerdown", onPointerDown, true);
          return samples;
        })()`,
      );
      if (osMouseHookEnabled) {
        await waitForProcessExit(mouseHookProcess, 3000);
        try {
          mouseHookEvents = await readMouseHookEvents();
          samples = annotateSamplesWithOsMouse(samples, mouseHookEvents);
        } catch (error) {
          mouseHookError = error instanceof Error ? error.message : String(error);
        }
      }
    } else if (inputMode === "mouse") {
      await evaluate(
        cdp,
        undefined,
        `(() => {
          window.__codexhubPerf = { longTasks: [] };
          if ("PerformanceObserver" in window) {
            try {
              const observer = new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                  window.__codexhubPerf.longTasks.push({
                    duration: entry.duration,
                    name: entry.name,
                    startTime: entry.startTime,
                  });
                }
              });
              observer.observe({ entryTypes: ["longtask"] });
            } catch {}
          }
          return true;
        })()`,
      );
      samples = [];
      for (let index = 0; index < iterations; index += 1) {
        const target = index % 2 === 0 ? "gateway" : "codexhub";
        const begin = await evaluate(
          cdp,
          undefined,
          `((targetPane) => {
            const labels = targetPane === "gateway" ? ["gateway"] : ["codexhub", "codex hub"];
            const buttons = Array.from(document.querySelectorAll("nav button"));
            const button = buttons.find((candidate) => {
              const text = (candidate.textContent || "").trim().toLowerCase();
              return labels.some((label) => text.includes(label));
            });
            if (!button) {
              throw new Error("Missing tab button: " + targetPane);
            }
            const rect = button.getBoundingClientRect();
            return {
              longTaskStartIndex: window.__codexhubPerf.longTasks.length,
              start: performance.now(),
              x: rect.left + rect.width / 2,
              y: rect.top + rect.height / 2,
            };
          })(${JSON.stringify(target)})`,
        );
        await dispatchMouseClick(cdp, begin.x, begin.y);
        const sample = await evaluate(
          cdp,
          undefined,
          `(async (input) => {
            const raf = () => new Promise((resolve) => requestAnimationFrame(() => resolve(performance.now())));
            const monitorFrames = async (durationMs) => {
              if (durationMs <= 0) {
                return { frameCount: 0, maxFrameGapMs: 0 };
              }
              const end = performance.now() + durationMs;
              let frameCount = 0;
              let last = performance.now();
              let maxFrameGapMs = 0;
              while (performance.now() < end) {
                const now = await raf();
                maxFrameGapMs = Math.max(maxFrameGapMs, now - last);
                last = now;
                frameCount += 1;
              }
              return { frameCount, maxFrameGapMs };
            };
            const waitForVisiblePane = (targetPane) => new Promise((resolve, reject) => {
              const deadline = performance.now() + 5000;
              const check = () => {
                const pane = document.querySelector('[data-tab-pane="' + targetPane + '"]');
                if (pane?.getAttribute("aria-hidden") === "false") {
                  resolve(performance.now());
                  return;
                }
                if (performance.now() > deadline) {
                  reject(new Error("Timed out waiting for " + targetPane));
                  return;
                }
                requestAnimationFrame(check);
              };
              requestAnimationFrame(check);
            });
            const clickEnd = performance.now();
            const visibleAt = await waitForVisiblePane(input.target);
            const frame1 = await raf();
            const frame2 = await raf();
            const dwell = await monitorFrames(input.dwellMs);
            const sampleEnd = performance.now();
            const longTasks = window.__codexhubPerf.longTasks
              .slice(input.longTaskStartIndex)
              .filter((task) => task.startTime >= input.start && task.startTime <= sampleEnd);
            return {
              clickEvalMs: clickEnd - input.start,
              dwellFrameCount: dwell.frameCount,
              dwellMaxFrameGapMs: dwell.maxFrameGapMs,
              frame1Ms: frame1 - input.start,
              frame2Ms: frame2 - input.start,
              longTaskCount: longTasks.length,
              longTaskMaxMs: Math.max(0, ...longTasks.map((task) => task.duration)),
              sampleMs: sampleEnd - input.start,
              target: input.target,
              visibleMs: visibleAt - input.start,
            };
          })(${JSON.stringify({
            dwellMs,
            longTaskStartIndex: begin.longTaskStartIndex,
            start: begin.start,
            target,
          })})`,
        );
        samples.push(sample);
      }
    } else {
      samples = await evaluate(
        cdp,
        undefined,
        `(async () => {
        window.__codexhubPerf = { longTasks: [] };
        if ("PerformanceObserver" in window) {
          try {
            const observer = new PerformanceObserver((list) => {
              for (const entry of list.getEntries()) {
                window.__codexhubPerf.longTasks.push({
                  duration: entry.duration,
                  name: entry.name,
                  startTime: entry.startTime,
                });
              }
            });
            observer.observe({ entryTypes: ["longtask"] });
          } catch {}
        }
        const iterations = ${JSON.stringify(iterations)};
        const dwellMs = ${JSON.stringify(dwellMs)};
        const samples = [];
        const raf = () => new Promise((resolve) => requestAnimationFrame(() => resolve(performance.now())));
        const monitorFrames = async (durationMs) => {
          if (durationMs <= 0) {
            return { frameCount: 0, maxFrameGapMs: 0 };
          }
          const end = performance.now() + durationMs;
          let frameCount = 0;
          let last = performance.now();
          let maxFrameGapMs = 0;
          while (performance.now() < end) {
            const now = await raf();
            maxFrameGapMs = Math.max(maxFrameGapMs, now - last);
            last = now;
            frameCount += 1;
          }
          return { frameCount, maxFrameGapMs };
        };
        const waitForVisiblePane = (targetPane) => new Promise((resolve, reject) => {
          const deadline = performance.now() + 5000;
          const check = () => {
            const pane = document.querySelector('[data-tab-pane="' + targetPane + '"]');
            if (pane?.getAttribute("aria-hidden") === "false") {
              resolve(performance.now());
              return;
            }
            if (performance.now() > deadline) {
              reject(new Error("Timed out waiting for " + targetPane));
              return;
            }
            requestAnimationFrame(check);
          };
          requestAnimationFrame(check);
        });
        const clickTab = (targetPane) => {
          const labels = targetPane === "gateway" ? ["gateway"] : ["codexhub", "codex hub"];
          const buttons = Array.from(document.querySelectorAll("nav button"));
          const button = buttons.find((candidate) => {
            const text = (candidate.textContent || "").trim().toLowerCase();
            return labels.some((label) => text.includes(label));
          });
          if (!button) {
            throw new Error("Missing tab button: " + targetPane);
          }
          button.click();
        };
        for (let index = 0; index < iterations; index += 1) {
          const target = index % 2 === 0 ? "gateway" : "codexhub";
          const longTaskStartIndex = window.__codexhubPerf.longTasks.length;
          const start = performance.now();
          clickTab(target);
          const clickEnd = performance.now();
          const visibleAt = await waitForVisiblePane(target);
          const frame1 = await raf();
          const frame2 = await raf();
          const dwell = await monitorFrames(dwellMs);
          const sampleEnd = performance.now();
          const longTasks = window.__codexhubPerf.longTasks
            .slice(longTaskStartIndex)
            .filter((task) => task.startTime >= start && task.startTime <= sampleEnd);
          samples.push({
            clickEvalMs: clickEnd - start,
            dwellFrameCount: dwell.frameCount,
            dwellMaxFrameGapMs: dwell.maxFrameGapMs,
            frame1Ms: frame1 - start,
            frame2Ms: frame2 - start,
            longTaskCount: longTasks.length,
            longTaskMaxMs: Math.max(0, ...longTasks.map((task) => task.duration)),
            sampleMs: sampleEnd - start,
            target,
            visibleMs: visibleAt - start,
          });
        }
        return samples;
      })()`,
      );
    }
    const afterMetrics = metricsByName((await cdp.send("Performance.getMetrics")).metrics);
    const metricDelta = Object.fromEntries(
      ["TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "JSHeapUsedSize"].map((name) => [
        name,
        (afterMetrics[name] ?? 0) - (beforeMetrics[name] ?? 0),
      ]),
    );
    const steadySamples = samples.slice(Math.max(0, warmupDiscard));
    const summarizeSet = (sampleSet) => ({
      clickEvalMs: summarize(sampleSet, "clickEvalMs"),
      eventTimingInputDelayMaxMs: summarize(sampleSet, "eventTimingInputDelayMaxMs"),
      eventTimingMaxMs: summarize(sampleSet, "eventTimingMaxMs"),
      eventTimingPresentationDelayMaxMs: summarize(sampleSet, "eventTimingPresentationDelayMaxMs"),
      eventTimingProcessingMaxMs: summarize(sampleSet, "eventTimingProcessingMaxMs"),
      frame1Ms: summarize(sampleSet, "frame1Ms"),
      frame2Ms: summarize(sampleSet, "frame2Ms"),
      longAnimationFrameMaxMs: summarize(sampleSet, "longAnimationFrameMaxMs"),
      longTaskCount: summarize(sampleSet, "longTaskCount"),
      longTaskMaxMs: summarize(sampleSet, "longTaskMaxMs"),
      osToPointerMs: summarize(sampleSet, "osToPointerMs"),
      dwellMaxFrameGapMs: summarize(sampleSet, "dwellMaxFrameGapMs"),
      sampleMs: summarize(sampleSet, "sampleMs"),
      visibleMs: summarize(sampleSet, "visibleMs"),
    });
    const result = {
      appPath: mode === "app" ? appPath : null,
      browserPath,
      generatedAt: new Date().toISOString(),
      headless,
      iterations,
      dwellMs,
      initialSettleMs,
      inputMode,
      manualDurationMs,
      metricDelta,
      mode,
      osMouseHook: osMouseHookEnabled ? {
        downCount: mouseHookEvents.filter((event) => event.type === "down").length,
        enabled: true,
        eventCount: mouseHookEvents.length,
        error: mouseHookError,
        outputPath: mouseHookOutputPath,
      } : { enabled: false },
      samples,
      steadySummary: summarizeSet(steadySamples),
      summary: summarizeSet(samples),
      target: targetInfo ? {
        title: targetInfo.title,
        type: targetInfo.type,
        url: targetInfo.url,
      } : null,
      url,
      viewport: { width: 1280, height: 820 },
      warmupDiscard,
    };
    await mkdir(dirname(outputPath), { recursive: true });
    await writeFile(outputPath, `${JSON.stringify(result, null, 2)}\n`, "utf8");
    console.log(JSON.stringify({ metricDelta, steadySummary: result.steadySummary, summary: result.summary }, null, 2));
    console.log(`Wrote ${outputPath}`);
  } finally {
    cdp?.close();
    mouseHookProcess?.kill();
    launchedProcess?.kill();
    if (launchedProcess) {
      await new Promise((resolve) => {
        const timeout = setTimeout(resolve, 1500);
        launchedProcess.once("exit", () => {
          clearTimeout(timeout);
          resolve();
        });
      });
    }
    if (profileDir) {
      try {
        await rm(profileDir, { force: true, recursive: true, maxRetries: 3, retryDelay: 250 });
      } catch {
        // Chrome can keep profile sqlite files briefly locked on Windows; perf output is already written.
      }
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
