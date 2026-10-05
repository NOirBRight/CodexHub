// Theme colors also apply to portals outside .workspace-root. Keep alpha
// modifiers working without duplicating the palette as RGB channel variables.
const themeColor = (token) => ({ opacityValue }) => opacityValue === undefined
  ? `var(--ws-${token})`
  : `color-mix(in srgb, var(--ws-${token}) calc(${opacityValue} * 100%), transparent)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        "ink": themeColor("ink"),
        "canvas": themeColor("bg"),
        "surface": themeColor("surface"),
        "panel": themeColor("inset"),
        "panel-soft": themeColor("inset"),
        "line": themeColor("line"),
        "line-soft": themeColor("line"),
        "muted": themeColor("muted"),
        "action": themeColor("accent"),
        "ok": themeColor("green"),
        "warn": themeColor("warning"),
        "danger": themeColor("danger"),
        "on-action": themeColor("on-accent"),
        "on-status": themeColor("on-status"),
        "action-hover": themeColor("accent-hover"),
        "ok-hover": themeColor("green-hover"),
        "danger-hover": themeColor("danger-hover"),
        "action-soft": themeColor("soft"),
        "ok-soft": themeColor("green-soft"),
        "danger-soft": themeColor("danger-soft"),
        "warn-soft": themeColor("warning-soft"),
        "ok-line": themeColor("green-line"),
        "danger-line": themeColor("danger-line"),
        "warn-line": themeColor("warning-line"),
        "scrim": themeColor("scrim"),
        "brand-surface": themeColor("brand-surface"),
        "switch-thumb": themeColor("switch-thumb"),
      },
      borderRadius: {
        DEFAULT: "8px",
        sm: "8px",
        md: "12px",
        lg: "16px",
        xl: "18px",
        "2xl": "20px",
        control: "10px",
        inner: "12px",
        panel: "16px",
        overlay: "20px",
      },
      boxShadow: {
        hairline: "0 0 0 1px var(--ws-line)",
        control: "var(--ws-shadow-control)",
        field: "var(--ws-shadow-control)",
        subtle: "var(--ws-shadow-surface)",
        card: "var(--ws-shadow-surface)",
        raised: "var(--ws-shadow-surface)",
        floating: "var(--ws-shadow-floating)",
        overlay: "var(--ws-shadow-overlay)",
        divider: "inset 0 1px 0 var(--ws-line)",
        connected: "0 0 0 4px color-mix(in srgb, var(--ws-green) 16%, transparent)",
      },
    },
  },
  plugins: [],
};
