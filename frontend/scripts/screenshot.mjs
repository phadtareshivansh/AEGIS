import { mkdirSync } from "node:fs";
import { chromium } from "playwright-core";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = "/var/folders/52/mxrcqkxn54l83d33ycm3gglr0000gq/T/opencode/aegis-shots";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--no-sandbox"],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });
page.on("console", (m) => console.log("[console]", m.type(), m.text().slice(0, 180)));
page.on("pageerror", (e) => console.log("[pageerror]", e.message));

console.log("loading :3000 …");
await page.goto("http://localhost:3000", { waitUntil: "networkidle" });
await page.getByRole("button", { name: "Run a scenario" }).click();
console.log("clicked Run a scenario …");

try {
  await page.waitForSelector('[data-phase="debate"]', { state: "visible", timeout: 240_000 });
  console.log("debate visible — waiting for streamed text …");
} catch {
  console.log("TIMEOUT waiting for debate; continuing");
}
await page.waitForTimeout(5000);
await page.screenshot({ path: `${OUT}/1-debate.png` });
console.log(`saved ${OUT}/1-debate.png`);

try {
  await page.waitForSelector('#briefing[data-phase="briefing"]', { state: "visible", timeout: 240_000 });
  console.log("briefing visible");
} catch {
  console.log("TIMEOUT waiting for briefing; continuing");
}
await page.waitForTimeout(1500);
await page.locator("#briefing").screenshot({ path: `${OUT}/2-briefing.png` });
await page.screenshot({ path: `${OUT}/2-fullpage.png`, fullPage: true });
console.log(`saved ${OUT}/2-briefing.png`);
console.log(`saved ${OUT}/2-fullpage.png`);

await browser.close();
console.log("done");