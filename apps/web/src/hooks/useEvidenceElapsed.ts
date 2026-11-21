import { useEffect, useMemo, useState } from "react";

export function useEvidenceElapsed(receivedAt: number) {
  const anchor = useMemo(() => ({
    clock: performance.now(),
    elapsed: Math.max(0, Date.now() - receivedAt),
  }), [receivedAt]);
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick(value => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return anchor.elapsed + Math.max(0, performance.now() - anchor.clock);
}
