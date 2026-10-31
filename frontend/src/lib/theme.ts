import { useCallback, useEffect, useState } from "react";

/** Light or dark: the system's, until the toggle picks one (kept per browser). */
export type Theme = "light" | "dark";

const KEY = "console.theme";
const dark = () => window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;

function stored(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

export function useTheme(): [Theme, () => void] {
  const [chosen, setChosen] = useState<Theme | null>(stored);
  const [system, setSystem] = useState<Theme>(dark() ? "dark" : "light");

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const on = () => setSystem(mq?.matches ? "dark" : "light");
    mq?.addEventListener("change", on);
    return () => mq?.removeEventListener("change", on);
  }, []);

  useEffect(() => {
    if (chosen) document.documentElement.dataset.theme = chosen;
    else delete document.documentElement.dataset.theme;
  }, [chosen]);

  const theme = chosen ?? system;
  const toggle = useCallback(() => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setChosen(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // private window: the choice lasts this page only
    }
  }, [theme]);
  return [theme, toggle];
}
