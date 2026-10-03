import { afterEach, expect, test } from "bun:test";
import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { act } from "react";
import { HomeVideoCardMetadata } from "../src/components/pv/HomeVideoCardMetadata";
if (!GlobalRegistrator.isRegistered) GlobalRegistrator.register();
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
const { createRoot } = await import("react-dom/client");
let root: ReturnType<typeof createRoot>;
let container: HTMLDivElement;
afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});
test("cards show title and exact location, then clear both across videos", async () => {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <HomeVideoCardMetadata
        name="IMG_2363.mp4"
        display_title="Synthetic Pool Challenge"
        location="Tulum, Mexico"
      />,
    ),
  );
  expect(container.textContent).toBe("Synthetic Pool ChallengeTulum, Mexico");
  await act(async () =>
    root.render(<HomeVideoCardMetadata name="IMG_2.mp4" display_title={null} location={null} />),
  );
  expect(container.textContent).toBe("IMG 2");
  expect(container.querySelectorAll("span").length).toBe(2);
});
