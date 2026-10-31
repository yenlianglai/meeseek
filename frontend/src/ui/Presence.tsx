import { useEffect, useState } from "react";
import { IconAlert, IconCheck } from "./Icons";

/** The running turn's state, quietly: a pulse while it works, a check when it
 *  is done, an alert when it failed. It says only what the screen needs. */
export function TurnPresence({ phase }: { phase: "work" | "done" | "failed" }) {
  // under reduced motion the pulse holds still (CSS), the state still reads
  const [shown, setShown] = useState(false);
  useEffect(() => {
    const id = window.setTimeout(() => setShown(true), 80); // no flash for an instant turn
    return () => window.clearTimeout(id);
  }, []);
  return (
    <span className={`presence presence-${phase}${shown ? " shown" : ""}`} aria-hidden>
      {phase === "work" ? <span className="presence-pulse" /> : phase === "done" ? <IconCheck /> : <IconAlert />}
    </span>
  );
}
