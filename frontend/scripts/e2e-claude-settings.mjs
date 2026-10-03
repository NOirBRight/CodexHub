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
  const beforeCardSubagent = settings();
  let cardSaveCount = 0;
  await page.route("**/api/invoke", async (route) => {
    if (route.request().postDataJSON()?.command === "save_claude_subagent") {
      cardSaveCount++;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    await route.continue();
  });
  const cardPicker = card.getByRole("button", { name: "Default subagent", exact: true });
  await expect(cardPicker).toBeEnabled();
  await cardPicker.click();
  let cardMenu = page.getByRole("dialog", { name: "Default subagent", exact: true });
  await cardMenu.getByRole("button", { name: /^Model/ }).click();
  await cardMenu.getByRole("option", { name: /^Native ·/ }).first().click();
  await expect(cardMenu).toBeHidden();
  await page.getByRole("heading", { name: "Claude Code", exact: true }).click();
  await expect.poll(() => settings().env?.CLAUDE_CODE_SUBAGENT_MODEL).toMatch(/^claude-/);
  await expect(cardPicker).toBeEnabled();
  assert.equal(cardSaveCount, 1, "closing the card menu during save must not submit twice");
  await cardPicker.click();
  cardMenu = page.getByRole("dialog", { name: "Default subagent", exact: true });
  await cardMenu.getByRole("button", { name: /^Model/ }).click();
  await cardMenu.getByRole("option", { name: "CLI default", exact: true }).click();
  await expect.poll(() => settings().env?.CLAUDE_CODE_SUBAGENT_MODEL).toBeUndefined();
  await expect(cardPicker).toBeEnabled();
  assert.equal(cardSaveCount, 2);
  assert.deepEqual(settings(), beforeCardSubagent, "card subagent edits must preserve main model, authentication and route settings");
  await page.unroute("**/api/invoke");
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  let dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  const picker = (name) => dialog.locator("label.ws-claude-field")
    .filter({ has: page.locator("span").filter({ hasText: new RegExp(`^${name}$`) }) }).locator("select");
  await expect(dialog).toBeVisible();
  await picker("Default model").selectOption("e2e/alpha");
  await picker("Haiku / fast").selectOption("e2e/beta");
  const beforeSubagent = settings();
  const subagentPicker = picker("Subagent");
  await expect.poll(() => subagentPicker.locator('option[value^="claude-"]').count()).toBeGreaterThan(0);
  const nativeSubagent = await subagentPicker.locator('option[value^="claude-"]').first().getAttribute("value");
  await subagentPicker.selectOption(nativeSubagent);
  await dialog.getByRole("button", { name: "Save Default subagent", exact: true }).click();
  await expect.poll(() => settings().env?.CLAUDE_CODE_SUBAGENT_MODEL).toBe(nativeSubagent);
  const afterSubagent = settings();
  delete afterSubagent.env.CLAUDE_CODE_SUBAGENT_MODEL;
  if (!Object.keys(afterSubagent.env).length && !beforeSubagent.env) delete afterSubagent.env;
  assert.deepEqual(afterSubagent, beforeSubagent, "saving subagent must leave unsaved main/family edits and connection untouched");
  await expect(dialog.getByRole("button", { name: "Save Default subagent", exact: true })).toBeDisabled();
  await dialog.getByText("Advanced and diagnostics").click();
  await dialog.getByRole("button", { name: "Preview connection configuration" }).click();
  await expect(dialog.locator("pre")).toContainText("claude-codexhub-e2e-alpha");
  await expect(dialog.locator("pre")).toContainText("claude-codexhub-e2e-beta");
  await expect(dialog.locator("pre")).not.toContainText("synthetic-local-gateway-key");
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeDisabled();
  await dialog.getByRole("checkbox").check();
  await dialog.getByRole("button", { name: "Connect", exact: true }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBe("claude-codexhub-e2e-alpha");
  await expect.poll(() => settings().env?.ANTHROPIC_DEFAULT_HAIKU_MODEL).toBe("claude-codexhub-role/haiku/e2e/beta");
  assert.equal(settings().env.CLAUDE_CODE_SUBAGENT_MODEL, nativeSubagent);
  await expect(dialog.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  await expect(picker("Default model")).toHaveValue("__codexhub_preserve_claude_default__");
  await expect(picker("Default model")).toContainText("Keep current default (e2e/alpha)");
  await expect(picker("Haiku / fast")).toHaveValue("e2e/beta");
  await picker("Default model").selectOption("e2e/beta");
  await picker("Haiku / fast").selectOption("");
  await picker("Sonnet").selectOption("e2e/alpha");
  await dialog.getByRole("button", { name: "Apply changes" }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBe("claude-codexhub-e2e-beta");
  await expect.poll(() => settings().env?.ANTHROPIC_DEFAULT_SONNET_MODEL).toBe("claude-codexhub-role/sonnet/e2e/alpha");
  assert(!("ANTHROPIC_DEFAULT_HAIKU_MODEL" in settings().env));
  await expect(dialog.getByRole("button", { name: "Disconnect" })).toBeEnabled();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(dialog).toBeHidden();
  await card.getByRole("button", { name: "Claude Code connection details" }).click();
  dialog = page.getByRole("dialog", { name: "Claude Code settings" });
  await expect(picker("Default model")).toHaveValue("__codexhub_preserve_claude_default__");
  await expect(picker("Default model")).toContainText("Keep current default (e2e/beta)");
  await expect(picker("Sonnet")).toHaveValue("e2e/alpha");
  await dialog.getByRole("button", { name: "Disconnect" }).click();
  await expect.poll(() => settings().env?.ANTHROPIC_MODEL).toBeUndefined();
  assert.equal(settings().theme, "dark");
  assert.equal(settings().env.EDITOR, "vim");
  assert.equal(settings().env.CLAUDE_CODE_SUBAGENT_MODEL, nativeSubagent);
  console.log("PASS: Claude browser UI preview, connect, edit, readback, disconnect");
} finally {
  await browser.close();
}
