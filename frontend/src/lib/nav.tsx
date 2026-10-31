import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { formatRoute, parseRoute, type Route } from "./route";

export type Navigate = (to: Route, opts?: { replace?: boolean }) => void;

/** The current Route, kept in step with the URL hash. `replace` rewrites the
 *  current entry instead of adding one — for filling in a default (the newest
 *  session, the newest run), which is not a step the user took. */
export function useHashRoute(): [Route, Navigate] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const navigate = useCallback<Navigate>((to, opts = {}) => {
    const hash = formatRoute(to);
    if (hash === window.location.hash) return;
    if (opts.replace) {
      window.history.replaceState(null, "", hash);
      setRoute(parseRoute(hash));
    } else {
      window.location.hash = hash; // fires hashchange
    }
  }, []);
  return [route, navigate];
}

export const NavContext = createContext<Navigate>(() => {});

/** Go somewhere: every link between objects goes through this. */
export const useNavigate = () => useContext(NavContext);
