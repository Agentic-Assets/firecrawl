import { Semaphore } from "../browser_resources";
import { ScrapeDeadlineError } from "../scrape_lifecycle";

export const tick = (ms = 0) =>
  new Promise((resolve) => setTimeout(resolve, ms));
export const isPhase = (phase: "admission" | "work") => (error: unknown) =>
  error instanceof ScrapeDeadlineError && error.phase === phase;

export function fixture(semaphore = new Semaphore(1)) {
  const calls: string[] = [];
  const page = { content: async () => "<html>property</html>" };
  const context = { newPage: async () => page };
  return {
    calls,
    page,
    context,
    options: {
      deadlineAt: Date.now() + 100,
      semaphore,
      prepare: async () => {},
      createContext: async () => {
        calls.push("context");
        return context;
      },
      createPage: (value: typeof context) => value.newPage(),
      work: (value: typeof page) => value.content(),
      closeContext: async () => {
        calls.push("close-context");
      },
      closePage: async () => {
        calls.push("close-page");
      },
      cleanupTimeoutMs: 5,
    },
  };
}
