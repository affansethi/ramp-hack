import { useCallback, useEffect, useRef, useState } from "react";

export const POLL_MS = 2000;

// Re-run `load` every two seconds and on demand; keeps the last good value.
export function usePoll(load, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const refresh = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, []);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, refresh };
}
