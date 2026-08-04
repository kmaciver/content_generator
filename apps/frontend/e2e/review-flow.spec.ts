import { expect, test } from "@playwright/test";

/**
 * M1's exit criterion (SADD §27):
 *
 *   "a Playwright test drives topic → generate → reject → regenerate → edit →
 *    approve, and the audit trail explains every one of those steps."
 *
 * Both halves matter. Driving the UI proves the pipeline runs; checking the
 * audit trail afterwards proves it can *account for itself*, which is the
 * property the whole immutable-versioning design exists to buy.
 *
 * **[M2-13]** The flow now starts one stage earlier. M2-09 gave `script` an
 * upstream, so a project goes research → approve → script, and this test drives
 * both through the UI rather than reaching for the API. That is deliberate: a
 * test that set research up over HTTP would go green while a real user faced a
 * dead end, which is exactly the failure it exists to catch.
 *
 * Runs through nginx against the prod-local stack, on the mock provider — no
 * API key, no network, no cost.
 */

const TOPIC = `e2e photosynthesis ${Date.now()}`;

// Buttons render from the server's `capabilities` payload (§11) and the DAG's
// `unmet` list (M2-13), so waiting for one to become enabled is a genuine
// assertion about backend state rather than a UI timing hack.
const enabled = (name: string) => ({ name, exact: true }) as const;

test.describe.configure({ mode: "serial" });

test("topic → research → script → reject → regenerate → edit → approve", async ({
  page,
}) => {
  // --- topic ---------------------------------------------------------------
  await page.goto("/projects");
  await page.getByLabel("Topic").fill(TOPIC);
  await page.getByRole("button", enabled("Create")).click();

  const projectLink = page.getByRole("link", { name: TOPIC });
  await expect(projectLink).toBeVisible();
  await projectLink.click();

  await expect(page.getByRole("heading", { name: TOPIC })).toBeVisible();
  // A project with no artifacts is a DRAFT, and the phase is derived (§12.4) —
  // this is the first assertion that the DAG reached the browser.
  await expect(page.getByTestId("project-phase")).toHaveText("Draft");

  // --- script is blocked until research is approved -------------------------
  // The pipeline's shape, visible. Before M2-13 the UI offered "Generate
  // script" unconditionally and the job failed in a worker where nobody saw it.
  await expect(
    page.getByRole("button", enabled("Generate Script")),
  ).toHaveCount(0);
  await expect(page.getByText("Waiting on: Research")).toBeVisible();

  // --- research ------------------------------------------------------------
  await page.getByRole("button", enabled("Generate Research")).click();

  const approve = page.getByRole("button", enabled("Approve"));
  // This single wait covers: job dispatched, consumed by a real Celery worker
  // over a real broker, provider called, version written, artifact transitioned.
  await expect(approve).toBeEnabled({ timeout: 60_000 });
  await expect(page.getByTestId("project-phase")).toHaveText("Research Review");

  await approve.click();
  await expect(page.getByTestId("stage-state-research")).toHaveText("Approved");

  // --- script --------------------------------------------------------------
  const generateScript = page.getByRole("button", enabled("Generate Script"));
  await expect(generateScript).toBeEnabled();
  await generateScript.click();
  await expect(approve).toBeEnabled({ timeout: 60_000 });

  const scriptBody = page.locator("article");
  await expect(scriptBody).not.toBeEmpty();

  // --- reject --------------------------------------------------------------
  await page
    .getByLabel("Review comment")
    .fill("Too abstract — open with something concrete.");
  await page.getByRole("button", enabled("Reject")).click();

  // A rejected artifact can be regenerated but not approved. Asserting the
  // *disabled* side too is what makes this a check on the FSM rather than on
  // whether a click happened to work.
  await expect(page.getByRole("button", enabled("Regenerate"))).toBeEnabled();
  await expect(approve).toBeDisabled();

  // --- regenerate ----------------------------------------------------------
  await page.getByRole("button", enabled("Regenerate")).click();
  await expect(approve).toBeEnabled({ timeout: 60_000 });

  // Two versions now exist, so the switcher appears — and v1 must still be
  // there, showing REJECTED. Rejected versions stay queryable forever
  // (§10.3 rule 2); a UI that hid them would make the lineage unexplainable.
  const switcher = page.getByRole("navigation", { name: "Versions" });
  await expect(switcher.getByRole("button", { name: /^v1/ })).toContainText(
    "Rejected",
  );
  await expect(switcher.getByRole("button", { name: /^v2/ })).toBeVisible();

  // --- edit ----------------------------------------------------------------
  await page.getByRole("button", enabled("Edit")).click();
  const editor = page.getByLabel("Script");
  await editor.fill("A human wrote this version by hand.");
  await page.getByRole("button", enabled("Save as new version")).click();

  // An edit is a new version, not an in-place change — and it lands awaiting
  // approval, because writing something is not the same as signing off on it.
  await expect(switcher.getByRole("button", { name: /^v3/ })).toContainText(
    "Awaiting Approval",
  );
  await expect(switcher.getByRole("button", { name: /^v3/ })).toContainText(
    "edited",
  );

  // --- approve -------------------------------------------------------------
  await page.getByRole("button", enabled("Approve")).click();
  // Scoped to the artifact badge: "Approved" is legitimately both an artifact
  // state and a version status, so an unscoped text match resolves to two
  // elements and fails strict mode.
  await expect(page.getByTestId("artifact-state")).toContainText("Approved");
  await expect(page.getByRole("button", enabled("Approve"))).toBeDisabled();

  // Approving v3 supersedes v2; v1 keeps its explicit rejection. An approval
  // must never relabel a human "no" as merely outdated.
  await expect(switcher.getByRole("button", { name: /^v2/ })).toContainText(
    "Superseded",
  );
  await expect(switcher.getByRole("button", { name: /^v1/ })).toContainText(
    "Rejected",
  );

  // Approving the script unblocks the next stage. This is the DAG driving the
  // UI end to end — M2-02's graph, M2-03's phase, and M2-13's rail agreeing.
  await expect(
    page.getByRole("button", enabled("Generate Scene Set")),
  ).toBeEnabled();
});

test("the audit trail explains every step", async ({ request }) => {
  // The second half of the exit criterion. Read through the same BFF the UI
  // uses, so this also proves the trail is reachable by a client rather than
  // only present in the database.
  const projects = await request.get("/api/bff/projects");
  expect(projects.ok()).toBeTruthy();
  const { items } = (await projects.json()) as {
    items: { id: string; topic: string }[];
  };
  const project = items.find((p) => p.topic === TOPIC);
  expect(project, "the e2e project should exist").toBeDefined();

  const detail = await request.get(`/api/bff/projects/${project!.id}`);
  const { artifacts, stages } = (await detail.json()) as {
    artifacts: { id: string; kind: string; state: string }[];
    stages: { kind: string; unmet: string[]; can_generate: boolean }[];
  };
  const script = artifacts.find((a) => a.kind === "script");
  expect(script?.state).toBe("APPROVED");

  // The graph, as the server computed it: research and script are done, so
  // scene_set is runnable and everything below it is still waiting.
  const sceneSet = stages.find((s) => s.kind === "scene_set");
  expect(sceneSet?.unmet).toEqual([]);
  const image = stages.find((s) => s.kind === "image");
  expect(image?.unmet).toContain("prompt");

  const artifact = await request.get(`/api/bff/artifacts/${script!.id}`);
  const { versions } = (await artifact.json()) as {
    versions: { version_no: number; status: string; origin: string }[];
  };

  // Three versions, each with the status the flow above should have produced,
  // and the third distinguishable as a human edit. This is the whole run,
  // read back from the state of record.
  expect(versions.map((v) => [v.version_no, v.status, v.origin])).toEqual([
    [3, "APPROVED", "human_edit"],
    [2, "SUPERSEDED", "generated"],
    [1, "REJECTED", "generated"],
  ]);
});
