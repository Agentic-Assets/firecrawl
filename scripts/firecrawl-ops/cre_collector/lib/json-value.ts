/** Refuse values that JSON.stringify would silently transform or omit. */
export function preservesJsonValue(value: unknown): boolean {
  const active = new Set<object>();
  let nodes = 0;
  const visit = (item: unknown, depth: number): boolean => {
    if (++nodes > 100000 || depth > 128) return false;
    if (item === null || typeof item === "string" || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item) && !Object.is(item, -0);
    if (typeof item !== "object" || active.has(item)) return false;
    const array = Array.isArray(item);
    const prototype = Object.getPrototypeOf(item);
    if (array && prototype !== Array.prototype) return false;
    if (!array && prototype !== Object.prototype && prototype !== null) return false;
    active.add(item);
    try {
      const keys = Reflect.ownKeys(item);
      if (keys.some((key) => typeof key !== "string")) return false;
      if (array) {
        const values = item as unknown[];
        if (keys.some((key) => key !== "length" && !/^(?:0|[1-9]\d*)$/.test(key as string)) ||
            keys.length !== values.length + 1) return false;
        for (let index = 0; index < values.length; index++) {
          const descriptor = Object.getOwnPropertyDescriptor(item, String(index));
          if (!descriptor || !("value" in descriptor) || !descriptor.enumerable || !visit(descriptor.value, depth + 1)) return false;
        }
      } else {
        for (const key of keys as string[]) {
          const descriptor = Object.getOwnPropertyDescriptor(item, key);
          if (!descriptor || !("value" in descriptor) || !descriptor.enumerable || !visit(descriptor.value, depth + 1)) return false;
        }
      }
      return true;
    } finally { active.delete(item); }
  };
  return visit(value, 0);
}

