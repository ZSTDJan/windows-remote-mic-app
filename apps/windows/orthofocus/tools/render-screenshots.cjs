const path = require("path");
const fs = require("fs");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright");

async function main() {
  const root = path.resolve(__dirname, "..");
  const source = path.join(__dirname, "screenshots.html");
  const output = path.join(root, "docs", "screenshots");
  fs.mkdirSync(output, { recursive: true });

  const browser = await chromium.launch({
    headless: true,
    executablePath:
      process.env.BROWSER_EXECUTABLE ||
      "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 1,
    });
    await page.goto(pathToFileURL(source).href);
    await page.locator("#directional-navigation").screenshot({
      path: path.join(output, "directional-navigation.png"),
    });
    await page.locator("#orthogonal-territory-grid").screenshot({
      path: path.join(output, "orthogonal-territory-grid.png"),
    });
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
