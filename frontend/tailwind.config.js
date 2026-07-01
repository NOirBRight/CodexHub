/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#1f2933",
        panel: "#f7f8fa",
        line: "#d8dee6",
        action: "#2563eb",
        ok: "#15803d",
        warn: "#b45309",
        danger: "#b91c1c",
      },
      boxShadow: {
        subtle: "0 1px 2px rgba(15, 23, 42, 0.08)",
      },
    },
  },
  plugins: [],
};
