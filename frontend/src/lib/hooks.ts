import { useCallback, useLayoutEffect, useRef } from "react";

/** A callback whose identity never changes but which always runs the latest
 *  `fn` — for handing a handler to a memoized child without the child
 *  re-rendering every time the parent does. Call it from events, not render. */
export function useStableCallback<A extends unknown[], R>(fn: (...args: A) => R): (...args: A) => R {
  const ref = useRef(fn);
  useLayoutEffect(() => { ref.current = fn; });
  return useCallback((...args: A) => ref.current(...args), []);
}
