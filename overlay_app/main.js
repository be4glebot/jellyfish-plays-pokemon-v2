// Launches three windows:
//
//   1. A normal, regular Chrome window/tab (tabs, address bar, the works)
//      on the real youtube.com watch page -- playing at full native fps;
//      none of the CV pipeline's frame pacing (config.PLAYBACK_INTERVAL_SEC)
//      applies to it. Launched with a dedicated profile (isolated from your
//      everyday Chrome profile) and --remote-debugging-port so this process
//      can query it over the Chrome DevTools Protocol (CDP).
//   2. A transparent, frameless, click-through, always-on-top Electron
//      window showing dashboard.py's /overlay page (grid + HUD + input
//      log, no video) -- kept glued to window 1, see below.
//   3. A normal (framed, resizable, independent) Electron window showing
//      dashboard.py's /status page -- the denser stats view (inputs by
//      button, connection health, uptime, error log). Doesn't need to
//      track anything; it's just a regular window you can move/resize/
//      leave open wherever.
//
// Because it's a normal tab now (not an --app= window where the video fills
// the whole client area), the video player only occupies part of the
// window -- YouTube's header/sidebar/chat move it around, and theater
// mode/window width change its size. So alignment isn't "match the window
// bounds"; it's "find the actual <video> element's on-screen rect and match
// that". That's done by connecting to the tab via CDP and periodically
// asking the page itself for getBoundingClientRect() on its <video>, then
// combining that with the browser window's outer screen position (from
// watch_window.ps1) to get an absolute screen rect for the overlay.
//
// Keep VIDEO_ID in sync with config.py's STREAM_URL.
// Keep DASHBOARD_URL in sync with config.py's DASHBOARD_HOST/DASHBOARD_PORT
// (main.py must already be running for /overlay to load).

const { app, BrowserWindow, screen } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const WebSocket = require("ws");

const VIDEO_ID = "eQ_foBERmzA";
const VIDEO_URL = `https://www.youtube.com/watch?v=${VIDEO_ID}`;
const DASHBOARD_URL = "http://localhost:8000/overlay";
const STATS_URL = "http://localhost:8000/status";
const PROFILE_DIR = path.join(process.env.LOCALAPPDATA, "JellyfishVideoProfile");
const INIT_WIDTH = 1280;
const INIT_HEIGHT = 720;
const CDP_PORT = 9333;

function findBrowserPath() {
  const candidates = [
    [process.env["ProgramFiles"], "Google\\Chrome\\Application\\chrome.exe"],
    [process.env["ProgramFiles(x86)"], "Google\\Chrome\\Application\\chrome.exe"],
    [process.env["LOCALAPPDATA"], "Google\\Chrome\\Application\\chrome.exe"],
    [process.env["ProgramFiles"], "Microsoft\\Edge\\Application\\msedge.exe"],
    [process.env["ProgramFiles(x86)"], "Microsoft\\Edge\\Application\\msedge.exe"],
  ].map(([base, rel]) => (base ? path.join(base, rel) : null));
  return candidates.find((p) => p && fs.existsSync(p));
}

function launchVideoWindow() {
  const browser = findBrowserPath();
  if (!browser) {
    console.error("Could not find Chrome or Edge in the usual install locations.");
    return;
  }
  spawn(
    browser,
    [
      VIDEO_URL,
      `--user-data-dir=${PROFILE_DIR}`,
      `--remote-debugging-port=${CDP_PORT}`,
      `--window-size=${INIT_WIDTH},${INIT_HEIGHT}`,
      "--new-window",
      "--no-first-run",
      "--no-default-browser-check",
    ],
    { detached: true, stdio: "ignore" }
  ).unref();
}

let overlayWin;

function createOverlayWindow() {
  overlayWin = new BrowserWindow({
    width: INIT_WIDTH,
    height: INIT_HEIGHT,
    frame: false,
    transparent: true,
    hasShadow: false,
    resizable: false,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    show: false, // stays hidden until the video window is confirmed visible
    webPreferences: { contextIsolation: true },
  });
  overlayWin.setIgnoreMouseEvents(true, { forward: true });
  overlayWin.setAlwaysOnTop(true, "pop-up-menu");
  overlayWin.loadURL(DASHBOARD_URL);
}

let statsWin;

function createStatsWindow() {
  const workArea = screen.getPrimaryDisplay().workArea;
  const width = 420;
  const height = 640;
  statsWin = new BrowserWindow({
    width,
    height,
    x: workArea.x + workArea.width - width - 20,
    y: workArea.y + 20,
    backgroundColor: "#0b1020", // matches dashboard.py's STATUS_TEMPLATE body
    title: "Jellyfish Stats",
    webPreferences: { contextIsolation: true },
  });
  statsWin.loadURL(STATS_URL);
}

// -- outer browser window position (physical screen pixels) ----------------
// Reported continuously by watch_window.ps1, which finds the chrome/edge
// process using our dedicated profile and calls GetWindowRect on it (with
// DPI awareness set, so the numbers are real physical pixels, not
// DPI-virtualized garbage).

let latestWindowRectPhysical = null; // {x, y, width, height}
let videoWindowVisible = false; // false while minimized or not found at all

function startWindowWatcher() {
  const scriptPath = path.join(__dirname, "watch_window.ps1");
  const proc = spawn("powershell", [
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    scriptPath,
    "-ProfileMarker",
    PROFILE_DIR,
  ]);

  let buffered = "";
  proc.stdout.on("data", (chunk) => {
    buffered += chunk.toString();
    const lines = buffered.split(/\r?\n/);
    buffered = lines.pop(); // keep the last, possibly-incomplete line for next time
    for (const line of lines) {
      if (!line.trim()) continue;
      if (line === "HIDDEN") {
        videoWindowVisible = false;
        if (overlayWin && !overlayWin.isDestroyed()) overlayWin.hide();
        continue;
      }
      const parts = line.split(",").map(Number);
      if (parts.length === 4 && parts.every(Number.isFinite) && parts[2] > 0 && parts[3] > 0) {
        const [x, y, width, height] = parts;
        latestWindowRectPhysical = { x, y, width, height };
        if (!videoWindowVisible && overlayWin && !overlayWin.isDestroyed()) {
          overlayWin.showInactive();
        }
        videoWindowVisible = true;
      }
    }
  });
  proc.stderr.on("data", (chunk) => console.error("watch_window.ps1:", chunk.toString()));
  proc.on("exit", (code) => console.error("watch_window.ps1 exited with code", code));

  app.on("before-quit", () => proc.kill());
}

// -- <video> element position within the page (via CDP) --------------------

function fetchJson(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            resolve(JSON.parse(data));
          } catch (e) {
            reject(e);
          }
        });
      })
      .on("error", reject);
  });
}

async function findYoutubeTargetWsUrl() {
  const targets = await fetchJson(`http://127.0.0.1:${CDP_PORT}/json`);
  const target = targets.find((t) => t.type === "page" && t.url && t.url.includes("youtube.com/watch"));
  return target ? target.webSocketDebuggerUrl : null;
}

class CdpClient {
  constructor(wsUrl) {
    this.ws = new WebSocket(wsUrl);
    this.nextId = 1;
    this.pending = new Map();
    this.ws.on("message", (raw) => {
      let msg;
      try {
        msg = JSON.parse(raw.toString());
      } catch (e) {
        return;
      }
      const p = this.pending.get(msg.id);
      if (!p) return;
      this.pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message));
      else p.resolve(msg.result);
    });
  }

  ready() {
    return new Promise((resolve, reject) => {
      this.ws.once("open", resolve);
      this.ws.once("error", reject);
    });
  }

  send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

// The whole point of one Runtime.evaluate call bundling all three of these
// together is to avoid multiple CDP round-trips per poll tick (viewport
// size and devicePixelRatio are needed alongside the video rect to convert
// its page-relative coordinates into absolute screen coordinates below).
const VIDEO_RECT_EXPRESSION = `JSON.stringify((() => {
  const v = document.querySelector('video');
  const r = v ? v.getBoundingClientRect() : null;
  return {
    video: r && { left: r.left, top: r.top, width: r.width, height: r.height },
    viewportWidth: document.documentElement.clientWidth,
    viewportHeight: document.documentElement.clientHeight,
    dpr: window.devicePixelRatio,
  };
})())`;

// Converts a physical-pixel screen rect (Windows/GetWindowRect units) to
// the logical-pixel (DIP) units Electron's BrowserWindow.setBounds()
// expects, using whichever display the rect actually falls on -- so this
// still works correctly across monitors with different scale factors.
function physicalToDip(rect) {
  const displays = screen.getAllDisplays();
  for (const d of displays) {
    const physX = d.bounds.x * d.scaleFactor;
    const physY = d.bounds.y * d.scaleFactor;
    const physW = d.bounds.width * d.scaleFactor;
    const physH = d.bounds.height * d.scaleFactor;
    if (rect.x >= physX - 1 && rect.x < physX + physW && rect.y >= physY - 1 && rect.y < physY + physH) {
      return {
        x: Math.round((rect.x - physX) / d.scaleFactor + d.bounds.x),
        y: Math.round((rect.y - physY) / d.scaleFactor + d.bounds.y),
        width: Math.round(rect.width / d.scaleFactor),
        height: Math.round(rect.height / d.scaleFactor),
      };
    }
  }
  const primary = screen.getPrimaryDisplay();
  return {
    x: Math.round(rect.x / primary.scaleFactor),
    y: Math.round(rect.y / primary.scaleFactor),
    width: Math.round(rect.width / primary.scaleFactor),
    height: Math.round(rect.height / primary.scaleFactor),
  };
}

async function connectCdpWithRetry(retries = 60, delayMs = 500) {
  for (let i = 0; i < retries; i++) {
    try {
      const wsUrl = await findYoutubeTargetWsUrl();
      if (wsUrl) {
        const client = new CdpClient(wsUrl);
        await client.ready();
        await client.send("Runtime.enable");
        return client;
      }
    } catch (e) {
      // Chrome/the tab isn't up yet -- keep retrying.
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error("Could not attach to the YouTube tab via CDP after retrying");
}

async function startVideoTracking() {
  const cdp = await connectCdpWithRetry();

  setInterval(async () => {
    if (!latestWindowRectPhysical || !videoWindowVisible) return;
    try {
      const result = await cdp.send("Runtime.evaluate", {
        expression: VIDEO_RECT_EXPRESSION,
        returnByValue: true,
      });
      const data = JSON.parse(result.result.value);
      if (!data.video) return;

      const dpr = data.dpr || 1;
      const win = latestWindowRectPhysical;
      // The page's viewport sits inset within the outer window rect (tab
      // strip + address bar above it); assume it's bottom-anchored and
      // horizontally centered within the window (true for Chrome's normal
      // layout -- no side chrome, only a top toolbar).
      const contentWidthPhysical = data.viewportWidth * dpr;
      const contentHeightPhysical = data.viewportHeight * dpr;
      const topInset = win.height - contentHeightPhysical;
      const leftInset = (win.width - contentWidthPhysical) / 2;

      const videoPhysical = {
        x: win.x + leftInset + data.video.left * dpr,
        y: win.y + topInset + data.video.top * dpr,
        width: data.video.width * dpr,
        height: data.video.height * dpr,
      };
      if (videoPhysical.width <= 0 || videoPhysical.height <= 0) return;

      const dip = physicalToDip(videoPhysical);
      if (overlayWin && !overlayWin.isDestroyed()) {
        overlayWin.setBounds(dip);
      }
    } catch (e) {
      console.error("CDP poll error:", e.message);
    }
  }, 200);
}

app.whenReady().then(() => {
  createOverlayWindow();
  createStatsWindow();
  launchVideoWindow();
  startWindowWatcher();
  startVideoTracking().catch((e) => console.error(e.message));
});

app.on("window-all-closed", () => app.quit());
