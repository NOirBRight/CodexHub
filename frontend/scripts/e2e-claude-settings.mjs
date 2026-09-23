import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { chromium, expect } from "playwright/test";

const [url, settingsPath] = process.argv.slice(2);
assert(url && settingsPath, "usage: node e2e-claude-settings.mjs URL SETTINGS_PATH");
const settings = () => JSON.parse(readFileSync(settingsPath, "utf8"));
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.CODEXHUB_E2E_CHROMIUM || "/usr/bin/chromium",
  args: ["--no-sandbox"],
});
try {
  const page = await browser.newPage({ locale: "en-US", viewport: { width: 1440, height: 900 } });
  await page.goto(url);
  await page.getByRole("button", { name: "Clients", exact: true }).click();
  const card = page.locator(".ws-client-card").filter({ has: page.getByRole("heading", { name: "Claude Code" }) });
  await expect(card).toBeVisible();
  await expect(card.locator(".ws-client-logo img")).toHaveAttribute("src", /claude-code-icon/);
  await expect.poll(() => card.locator(".ws-client-logo img")
    .evaluate((image) => image.complete && image.naturalWidth > 0)).toBe(true);
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  let dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  const picker = (name) => dialog.locator("label.ws-claude-field")
    .filter({ has: page.getByText(name, { exact: true }) }).locator("select");
  await expect(dialog).toBeVisible();
  await picker("Default model").selectOption("e2e/alpha");
  await picker("Haiku / fast").selectOption("e2e/beta");
  await dialog.getByText("Advanced and diagnostics").click();
  await dialog.getByRole("button", { name: "Preview connection configuration" }).click();
  await expect(dialog.locator("pre")).toContainText("claude-codexhub-e2e-alpha");
  await expect(dialog.locator("pre")).toContainText("claude-codexhub-e2e-beta");
  await expect(dialog.locator("pre")).not.toContainText("synthetic-local-gateway-key");
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Connect", exact: true }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBe("claude-codexhub-e2e-alpha");
  await expect.poll(() => settings().env?.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe("claude-codexhub-e2e-beta");
  await expect(dialog.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  await expect(picker("Default model")).toHaveValue("e2e/alpha");
  await expect(picker("Haiku / fast")).toHaveValue("e2e/beta");
  await picker("Default model").selectOption("e2e/beta");
  await picker("Haiku / fast").selectOption("");
  await picker("Sonnet").selectOption("e2e/alpha");
  await dialog.getByRole("button", { name: "Apply changes" }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBe("claude-codexhub-e2e-beta");
  await expect.poll(() => settings().env?.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe("claude-codexhub-e2e-alpha");
  assert(!("ANTHROPIC_DEFAULT_HAIKU_MODEL" in settings().env));
  await expect(dialog.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  await expect(picker("Default model")).toHaveValue("e2e/beta");
  await expect(picker("Sonnet")).toHaveValue("e2e/alpha");
  await dialog.getByRole("button", { name: "Disconnect" }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBeUndefined();
  assert.equal(settings().theme, "dark");
  assert.equal(settings().env.EDITOR, "vim");
  console.log("PASS: Claude browser UI preview, connect, edit, readback, disconnect");
} finally {
  await browser.close();
}
