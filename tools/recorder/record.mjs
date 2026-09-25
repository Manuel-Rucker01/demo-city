// Record the Jev City web replay (record mode) to an MP4.
//
// Usage (web dev server must be running on :5180):
//   node tools/recorder/record.mjs --run base-or3-s1 --compare metro-or3-s1 \
//     --color commute --speed 2 --title "A new metro line, simulated" --out media/metro.mp4
//
// Captures frames with the Chrome DevTools screencast (JPEG q=92, real GPU in a headed
// window so WebGL runs at full speed), keeps each frame's timestamp, and lets ffmpeg rebuild
// a constant-30fps H.264 MP4 from them. Stops when the page sets window.__jevcityDone.
import { chromium } from "playwright";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { dirname, join, resolve } from "node:path";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => {
    if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1]]);
    return acc;
  }, []),
);
const base = args.base ?? "http://localhost:5180";
const out = resolve(args.out ?? "media/jevcity.mp4");
const tail = Number(args.tail ?? 2.5); // seconds to keep after the last day
const params = new URLSearchParams({
  record: "1",
  run: args.run ?? "base-or3-s1",
  ...(args.compare ? { compare: args.compare } : {}),
  speed: args.speed ?? "2",
  ...(args.color ? { color: args.color } : {}),
  ...(args.title ? { title: args.title } : {}),
});
const url = `${base}/?${params}`;
const framesDir = join(dirname(out), "frames");
rmSync(framesDir, { recursive: true, force: true });
mkdirSync(framesDir, { recursive: true });

const browser = await chromium.launch({
  headless: false,
  args: ["--window-size=1920,1160", "--hide-scrollbars", "--force-device-scale-factor=1"],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
const client = await page.context().newCDPSession(page);

const frames = [];
client.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
  const file = join(framesDir, `f${String(frames.length).padStart(6, "0")}.jpg`);
  writeFileSync(file, Buffer.from(data, "base64"));
  frames.push({ file, t: metadata.timestamp });
  await client.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
});

console.log("opening", url);
await page.goto(url, { waitUntil: "networkidle" });
await client.send("Page.startScreencast", {
  format: "jpeg", quality: 92, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1,
});
await page.waitForFunction(() => window.__jevcityDone === true, null, { timeout: 15 * 60_000, polling: 250 });
await page.waitForTimeout(tail * 1000);
await client.send("Page.stopScreencast");
await browser.close();

if (frames.length < 10) throw new Error(`only ${frames.length} frames captured`);
// ffmpeg concat list with each frame's real on-screen duration -> constant 30 fps output.
const lines = ["ffconcat version 1.0"];
for (let i = 0; i < frames.length; i++) {
  const dur = i + 1 < frames.length ? Math.max(frames[i + 1].t - frames[i].t, 0.001) : 1 / 30;
  lines.push(`file '${frames[i].file}'`, `duration ${dur.toFixed(4)}`);
}
lines.push(`file '${frames[frames.length - 1].file}'`);
const list = join(framesDir, "list.ffconcat");
writeFileSync(list, lines.join("\n"));
const seconds = frames[frames.length - 1].t - frames[0].t;
console.log(`captured ${frames.length} frames over ${seconds.toFixed(1)} s (${(frames.length / seconds).toFixed(1)} fps avg)`);

execFileSync("ffmpeg", [
  "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", list,
  "-vf", "fps=30,scale=1920:1080:flags=lanczos,format=yuv420p",
  "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", out,
], { stdio: "inherit" });
rmSync(framesDir, { recursive: true, force: true });
console.log("wrote", out);
