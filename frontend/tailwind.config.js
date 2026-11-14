/** Colors are CSS variables (src/index.css) so light/dark swap in one place. */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        page: v("page"),
        surface: v("surface"),
        raised: v("raised"),
        line: v("line"),
        ink: { DEFAULT: v("ink"), 2: v("ink-2"), 3: v("ink-3") },
        accent: { DEFAULT: v("accent"), soft: v("accent-soft"), ink: v("accent-ink"), on: v("accent-on") },
        highlight: { DEFAULT: v("highlight"), soft: v("highlight-soft") },
        good: { DEFAULT: v("good"), soft: v("good-soft") },
        warn: { DEFAULT: v("warn"), soft: v("warn-soft"), ink: v("warn-ink") },
        bad: { DEFAULT: v("bad"), soft: v("bad-soft") },
        band: { DEFAULT: v("band"), ink: v("band-ink"), muted: v("band-muted") },
      },
      fontFamily: {
        sans: ['"Be Vietnam Pro"', "system-ui", "-apple-system", '"Segoe UI"', "Roboto", "sans-serif"],
        mono: ["ui-monospace", "Menlo", "Consolas", "monospace"],
      },
      boxShadow: {
        card: "none",
        lift: "0 10px 30px rgba(12, 59, 57, 0.14)",
        pop: "0 12px 32px -8px rgb(0 0 0 / 0.18), 0 2px 6px rgb(0 0 0 / 0.06)",
      },
    },
  },
  plugins: [],
};
