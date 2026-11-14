import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeName = "light" | "dark";

/** Chart palette per theme (validated reference palette: fixed categorical order, one-hue ramps). */
export interface ChartPalette {
  surface: string;
  ink: string;
  ink2: string;
  muted: string;
  grid: string;
  axis: string;
  series: string[]; // categorical slots, fixed order - never cycled
  seq: string[]; // sequential blue ramp, light -> dark (100..700)
  status: { good: string; warning: string; serious: string; critical: string };
}

/** Sequential teal ramp (one hue, light -> dark) for heatmaps and pivot shading; same in both themes. */
const SEQ = ["#e3f3f1", "#cdeae6", "#b4dfd9", "#97d2cb", "#78c3bb", "#58b2a9", "#38a098",
  "#1a8d86", "#007d77", "#006c67", "#005a56", "#004845", "#003633"];

/** Same values as the CSS tokens in index.css (SVG needs literal colours). */
const PALETTES: Record<ThemeName, ChartPalette> = {
  light: {
    surface: "#ffffff", ink: "#15232b", ink2: "#52646c", muted: "#52646c",
    grid: "#dde5e4", axis: "#c9d5d4",
    // accent first (primary series), then the extra series colours, then amber; never cycled.
    series: ["#00756f", "#1f6fb2", "#6d4bb8", "#b7791f", "#f2b705", "#52646c"],
    seq: SEQ,
    status: { good: "#1e7a3a", warning: "#f2b705", serious: "#b7791f", critical: "#b3261e" },
  },
  dark: {
    surface: "#172227", ink: "#e4ecec", ink2: "#9ab0b4", muted: "#9ab0b4",
    grid: "#22323a", axis: "#2a3a40",
    series: ["#3cc4b6", "#6fb2f0", "#b39cff", "#e9b45a", "#f2c14e", "#9ab0b4"],
    seq: SEQ,
    status: { good: "#7fd69b", warning: "#f2c14e", serious: "#e9b45a", critical: "#ff8a80" },
  },
};

interface ThemeCtx {
  theme: ThemeName;
  palette: ChartPalette;
  toggle: () => void;
}

const Ctx = createContext<ThemeCtx | null>(null);

function initialTheme(): ThemeName {
  const t = document.documentElement.dataset.theme;
  return t === "dark" ? "dark" : "light";
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<ThemeName>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("hcdw-theme", theme);
    } catch {
      /* storage may be unavailable (private window); the theme still works for this visit */
    }
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  const value = useMemo(() => ({ theme, palette: PALETTES[theme], toggle }), [theme, toggle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useTheme outside ThemeProvider");
  return ctx;
}

/** Map t in [0,1] onto the sequential ramp; returns fill + readable text colour. */
export function seqColor(palette: ChartPalette, t: number): { fill: string; text: string } {
  const ramp = palette.seq;
  const i = Math.max(0, Math.min(ramp.length - 1, Math.round(t * (ramp.length - 1))));
  return { fill: ramp[i], text: i >= 6 ? "#ffffff" : "#0b0b0b" };
}
