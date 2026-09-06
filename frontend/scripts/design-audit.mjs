import fs from "node:fs";
import postcss from "postcss";
const file = new URL(
  "../src/components/workspace/workspace.css",
  import.meta.url,
);
const root = postcss.parse(fs.readFileSync(file, "utf8"));
const issues = [];
const report = (node, message) =>
  issues.push(`${node.source.start.line}: ${message}`);
root.walkRules((rule) => {
  if (/span\.rounded-full|\[class[^\]]*rounded/.test(rule.selector))
    report(
      rule,
      "Use a component role for geometry, not a generic rounded utility selector",
    );
});
root.walkDecls((decl) => {
  if (
    decl.prop === "border-radius" &&
    decl.value !== "0" &&
    !decl.value.startsWith("var(--ws-radius-")
  )
    report(decl, "Border radius must use a design token");
  if (
    decl.prop === "box-shadow" &&
    decl.value !== "none" &&
    !decl.value.startsWith("var(--ws-shadow-") &&
    !/ws-model-switch|ws-switch-control/.test(decl.parent.selector ?? "")
  )
    report(decl, "Shadow must use a design token");
  if (
    /^(gap|column-gap|row-gap|padding|margin)(-(top|bottom|left|right))?$/.test(
      decl.prop,
    )
  ) {
    for (const match of decl.value.matchAll(/\b(\d+)px\b/g)) {
      if (+match[1] > 4 && +match[1] % 2)
        report(decl, "Content spacing must use the even-pixel scale");
    }
  }
});
if (issues.length) {
  console.error(issues.join("\n"));
  process.exitCode = 1;
} else
  console.log(
    "Design audit passed: role-scoped geometry, radius/shadow tokens and content spacing. Browser state checks remain required.",
  );
