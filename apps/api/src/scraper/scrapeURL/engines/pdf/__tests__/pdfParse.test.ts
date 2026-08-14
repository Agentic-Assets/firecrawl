import { resolve } from "node:path";
import type { Meta } from "../../..";
import { scrapePDFWithParsePDF } from "../pdfParse";

const fixturePath = resolve(
  process.cwd(),
  "../test-site/public/example-long.pdf",
);

const meta = {
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    error: vi.fn(),
  },
} as unknown as Meta;

describe("scrapePDFWithParsePDF", () => {
  it("honors maxPages when the legacy pdf-parse fallback is used", async () => {
    const result = await scrapePDFWithParsePDF(meta, fixturePath, 1);

    expect(result.markdown).toContain("ECMA-262");
    // This heading starts after the cover page in the 816-page fixture. Its
    // absence proves the fallback did not silently return the full document.
    expect(result.markdown).not.toContain("4.4.14 Undefined type");
  });
});
