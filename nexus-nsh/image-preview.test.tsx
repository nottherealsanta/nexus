import { describe, expect, open, test } from "nsh/test";

describe("image preview", () => {
  test("has a visible close action on a narrow screen", async () => {
    const app = await open("./ImagePreviewFixture.tsx", { width: 390, height: 844, platform: "web" });
    await app.waitFor("id:image-preview");
    await app.press("id:image-preview-close");
    await app.waitUntil(() => !app.shows("id:image-preview"));
    expect(app.shows("id:image-preview")).toBe(false);
  });
});
