import { expect, test } from "@playwright/test";

test("Library DOI import blocks duplicate clicks until the initial request settles on desktop and phone", async ({
  page,
  request,
}) => {
  const projectResponse = await request.post("/api/projects", {
    data: { name: "Concurrent DOI browser control", mode: "manual" },
  });
  expect(projectResponse.ok()).toBeTruthy();
  const project = await projectResponse.json();
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 850 });
      let submitted = 0;
      let release: (() => void) | undefined;
      const pending = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route("**/api/library/import", async (route) => {
        submitted++;
        await pending;
        await route.fulfill({
          status: 200,
          json: {
            id: "single-record-browser-control",
            project_id: project.id,
            title: "The imported paper",
            status: "available",
            revision: 0,
            data: { doi: "10.9999/one-source" },
          },
        });
      });
      try {
        await page.goto(`/projects/${project.id}/library`);
        const input = page.getByRole("textbox", {
          name: "DOI or arXiv identifier",
        });
        await expect(input).toBeVisible();
        await input.fill("10.9999/one-source");
        const button = page.getByRole("button", {
          name: "Import",
          exact: true,
        });
        await expect(button).toBeEnabled();
        await button.click();
        await expect(button).toBeDisabled();
        await expect.poll(() => submitted).toBe(1);
        // A second attempted click while the network is held cannot dispatch.
        await button.evaluate((element: HTMLButtonElement) => element.click());
        expect(submitted).toBe(1);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= window.innerWidth,
          ),
        ).toBeTruthy();
        release?.();
        await expect(input).toHaveValue("");
        await expect(button).toBeDisabled();
        expect(submitted).toBe(1);
      } finally {
        release?.();
        await page.unroute("**/api/library/import");
      }
    }
    expect(errors).toEqual([]);
  } finally {
    await request.delete(`/api/projects/${project.id}`);
  }
});
