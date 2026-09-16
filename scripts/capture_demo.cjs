const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("playwright");

const root = path.resolve(__dirname, "..");
const outputDir = path.join(root, "artifacts", "demo");
const videoDir = path.join(outputDir, ".recordings");
fs.rmSync(videoDir, { recursive: true, force: true });
fs.mkdirSync(videoDir, { recursive: true });

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function main() {
  const browser = await chromium.launch({
    headless: true,
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  const browserErrors = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(message.text());
  });
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await page.goto("http://localhost:8000/dashboard", { waitUntil: "networkidle" });
  await page.getByText("API connected").waitFor();
  await page.getByText("Recent jobs").waitFor();
  await sleep(1800);

  await page.screenshot({ path: path.join(outputDir, "faultlab-dashboard.png"), fullPage: true });

  const existingFlaky = page.locator('button[aria-label^="flaky job"]').first();
  await existingFlaky.click();
  await page.getByRole("heading", { name: "Attempt history" }).waitFor();
  await sleep(1600);
  await page.screenshot({ path: path.join(outputDir, "faultlab-retry-detail.png"), fullPage: true });

  await page.locator("#preset").selectOption("flaky");
  await sleep(900);
  await page.getByRole("button", { name: /Create job/ }).click();
  await page.getByText(/flaky job created/i).waitFor();
  await sleep(1200);
  await page.locator("#job-detail").scrollIntoViewIfNeeded();
  await sleep(2200);
  await page.getByRole("button", { name: "Refresh" }).click();
  await sleep(1200);
  await page.locator("#job-detail .detail-top > .status-succeeded").waitFor();
  await sleep(1800);

  await page.locator("#status-filter").selectOption("cancelled");
  await sleep(1400);
  const cancelled = page.locator('button[aria-label*="cancelled"]').first();
  if (await cancelled.count()) {
    await cancelled.click();
    await sleep(1800);
  }

  await page.locator("#page-title").scrollIntoViewIfNeeded();
  await sleep(1600);
  const pageCheck = await page.evaluate(() => ({
    hasContent: document.body.innerText.trim().length > 0,
    hasErrorOverlay: Boolean(
      document.querySelector(
        "[data-nextjs-dialog], .vite-error-overlay, #webpack-dev-server-client-overlay",
      ),
    ),
  }));
  if (!pageCheck.hasContent || pageCheck.hasErrorOverlay || browserErrors.length) {
    throw new Error(`browser verification failed: ${JSON.stringify({ pageCheck, browserErrors })}`);
  }
  const video = page.video();
  await context.close();
  const recordedPath = await video.path();
  const webmPath = path.join(outputDir, "faultlab-demo.webm");
  const mp4Path = path.join(outputDir, "faultlab-demo.mp4");
  fs.renameSync(recordedPath, webmPath);
  execFileSync("ffmpeg", [
    "-y", "-loglevel", "error", "-i", webmPath,
    "-c:v", "libx264", "-preset", "medium", "-crf", "22",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4Path,
  ]);
  fs.rmSync(videoDir, { recursive: true, force: true });
  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
