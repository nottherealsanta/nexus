import { expect, open, test } from "nsh/test";

test("a growing row above the viewport preserves the visible marker", async () => {
  const app = await open("./ScrollAnchorFixture.tsx", { width: 600, height: 500 });
  await app.scroll("id:anchor-list", 150);
  await app.press("id:save-offset");
  expect(app.shows("text:saved 150")).toBe(true);
  await app.press("id:grow-row");
  await app.wait(500);
  await app.press("id:measure");
  expect(app.shows("text:delta 80")).toBe(true);
  expect(app.shows("text:position 0")).toBe(true);
});

test("prepending history preserves the visible marker", async () => {
  const app = await open("./ScrollAnchorFixture.tsx", { width: 600, height: 500 });
  await app.scroll("id:anchor-list", 150);
  await app.press("id:save-offset");
  expect(app.shows("text:saved 150")).toBe(true);
  await app.press("id:prepend-rows");
  await app.wait(500);
  await app.press("id:measure");
  expect(app.shows("text:delta 120")).toBe(true);
  expect(app.shows("text:position 0")).toBe(true);
});
