import { useEffect, useState } from "react";

const anchors = new WeakMap<object, { updatedAt: number; clock: number; elapsed: number }>();

export function useEvidenceElapsed(data: object | undefined, updatedAt: number) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick(value => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (!data) return 0;
  let anchor = anchors.get(data);
  if (!anchor || anchor.updatedAt !== updatedAt) {
    anchor = { updatedAt, clock: performance.now(), elapsed: Math.max(0, Date.now() - updatedAt) };
    anchors.set(data, anchor);
  }
  return anchor.elapsed + Math.max(0, performance.now() - anchor.clock);
}
