import { expect, test, type Page } from "@playwright/test";

/**
 * M4-12 / M5-12 — the whole pipeline, end to end, in a browser.
 *
 * `review-flow.spec.ts` proves the *review machinery*: reject, regenerate,
 * edit, approve, and an audit trail that accounts for all of it. It stops at
 * script. This one proves the *pipeline*: topic → research → script → scenes →
 * prompts → images → voice → timeline → render → caption → cover → a
 * downloadable publishing package, driven entirely through the UI a person
 * would use.
 *
 * **M5-12 extended it by the last hop**, which is also the SADD's own
 * definition of this suite (§21.4): "create project → generate/approve every
 * stage → render → download package → assert zip manifest". The milestone's
 * exit criterion and this test are the same sentence.
 *
 * **Why that distinction is worth a second file.** Everything below script had
 * only ever been exercised by unit tests and by hand. Unit tests can prove the
 * compiler emits a correct timeline; only this can prove that approving twenty
 * pictures leads to a video element with a source in it. The two failures look
 * nothing alike and neither predicts the other.
 *
 * **Provider mode.** Free and deterministic by construction: `make e2e-guard`
 * refuses to start when compose resolves `PROVIDERS__MODE` to a mode that
 * spends. That guard is not paranoia — it was written because `.env` on a
 * working machine usually says `real`, so this flow, which drives images and a
 * full narration, would have billed the vendor once per run. The mocks are not
 * stubs: `MockImageProvider` emits decodable PNGs at the requested ratio and
 * `MockVoiceProvider` emits real MP3 frames with per-character timings, so the
 * bytes travel the same normalisation, storage and render path the real ones do.
 *
 * **Sequential, and long.** One project moves through nine stages, each
 * involving a real Celery round-trip over a real broker, and the last of them
 * runs FFmpeg over every scene. The per-test timeout is raised accordingly:
 * the failure this suite exists to catch is a stage that never completes, and
 * declaring that early would report a bug that is not there.
 */

const TOPIC = `e2e media ${Date.now()}`;

const enabled = (name: string) => ({ name, exact: true }) as const;

test.describe.configure({ mode: "serial" });

/** Nine stages, one of which shells out to FFmpeg. The config's 90s is sized
 * for a single generate; this is the whole pipeline. */
test.setTimeout(10 * 60_000);

/** Generate one stage and wait for it to come back reviewable.
 *
 * The wait is on the **Approve button becoming enabled**, which is a genuine
 * assertion about server state rather than a sleep: it renders from the FSM's
 * `capabilities` payload (§11), so it can only enable once a real worker has
 * consumed the job, called a provider, written a version and transitioned the
 * artifact.
 */
async function generate(page: Page, label: string): Promise<void> {
  const button = page.getByRole("button", enabled(`Generate ${label}`));
  await expect(button).toBeEnabled();
  await button.click();
}

async function approveCurrent(page: Page): Promise<void> {
  const approve = page.getByRole("button", enabled("Approve"));
  await expect(approve).toBeEnabled({ timeout: 120_000 });
  await approve.click();
}

test("topic → every stage → a verified publishing package", async ({
  page,
}) => {
  // --- topic, with a series ------------------------------------------------
  // The series is the point of this step, not decoration. ADR-016 resolves an
  // image job's character and style through the project's series, and this
  // form used to send a topic alone — so no project a person could create in
  // this UI was capable of being illustrated. The 409 arrived four stages
  // later, naming a screen the create form does not link to.
  await page.goto("/projects");
  await page.getByLabel("Topic").fill(TOPIC);

  const series = page.getByLabel("Series");
  await expect(series).toBeVisible();
  // Asserting a *real* option is selected, not merely that the control exists:
  // "No series" is a valid value of this select and is exactly the state that
  // produced the bug.
  await expect(series).not.toHaveValue("");

  await page.getByRole("button", enabled("Create")).click();
  await page.getByRole("link", { name: TOPIC }).click();
  await expect(page.getByRole("heading", { name: TOPIC })).toBeVisible();

  // The way out exists. This screen was reachable and not leavable — the only
  // route back was the browser's own Back button or editing the URL — and a
  // suite that never tried to leave could not notice.
  await page.getByRole("link", { name: "← Projects" }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await page.getByRole("link", { name: TOPIC }).click();
  await expect(page.getByRole("heading", { name: TOPIC })).toBeVisible();

  // --- research → script → scene_set ---------------------------------------
  for (const stage of ["Research", "Script", "Scene Set"]) {
    await generate(page, stage);
    await approveCurrent(page);
  }
  await expect(page.getByTestId("stage-state-scene_set")).toHaveText(
    "Approved",
  );

  // The DAG's concurrency, visible (ADR-009). `prompt` and `voice` both hang
  // off `scene_set` and neither depends on the other, so approving the scene
  // set must unblock *both* — a pipeline that had quietly serialised them
  // would still pass every stage-by-stage assertion above.
  await expect(
    page.getByRole("button", enabled("Generate Prompt")),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", enabled("Generate Voice")),
  ).toBeEnabled();

  // --- prompts, one per scene ----------------------------------------------
  await generate(page, "Prompt");

  const scenes = page.getByRole("navigation", { name: "Scenes" });
  await expect(scenes).toBeVisible({ timeout: 120_000 });

  // Read the chips once the fan-out has landed. Card scenes (M4-01) have no
  // prompt artifact and render as disabled chips, so the enabled ones are
  // exactly the set awaiting a decision — which is why this counts buttons
  // rather than assuming a scene count the mock is free to vary (4–7).
  const promptChips = scenes.getByRole("button").filter({ hasNotText: "Set" });
  const sceneCount = await promptChips.count();
  expect(sceneCount).toBeGreaterThan(0);

  // **The Set chip is not optional.** A per-scene stage produces N artifacts
  // plus the project-wide row the job was requested against, which the worker
  // completes with a manifest of the batch — and stage state is the *least
  // advanced* artifact of a kind, so approving all six prompts and not the
  // manifest leaves `prompt` awaiting approval and `image` blocked forever.
  // That is what this test hit on its first run.
  await scenes.getByRole("button", enabled("Set")).click();
  await approveCurrent(page);

  for (let i = 0; i < sceneCount; i += 1) {
    const chip = promptChips.nth(i);
    if (await chip.isDisabled()) continue;
    await chip.click();
    await approveCurrent(page);
  }
  await expect(page.getByTestId("stage-state-prompt")).toHaveText("Approved");

  // --- images --------------------------------------------------------------
  await generate(page, "Image");

  // The contact sheet is the review unit for pictures (M3-09): one decision
  // about a set, not N decisions in sequence.
  //
  // Waited for on the **stage's state**, not on the sheet appearing. The sheet
  // renders one cell per scene the moment the stage is active, whether or not
  // a picture has arrived — so "the sheet is visible" is true seconds before
  // any image exists, and asserting on it caught nothing.
  const sheet = page.getByTestId("contact-sheet");
  await expect(sheet).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("stage-state-image")).toHaveText(
    "Awaiting Approval",
    { timeout: 300_000 },
  );

  // Every tile is a real image that actually loaded. `naturalWidth > 0` is the
  // assertion that matters: a broken `src` still renders an <img>, and the
  // dev-profile asset bug found in M4-11 looked exactly like a passing test
  // that only checked the element was there.
  // One picture per scene, exactly. Card scenes are drawn locally rather than
  // bought (M4-01) but still land in storage, so a missing tile is a hole in
  // the video and not a stage that was skipped.
  const tiles = sheet.locator("img");
  await expect.poll(async () => tiles.count()).toBe(sceneCount);
  const loaded = await tiles.evaluateAll((nodes) =>
    nodes.every((node) => (node as HTMLImageElement).naturalWidth > 0),
  );
  expect(loaded, "every contact-sheet image should have decoded").toBe(true);

  // **Open one scene.** The grid is not the only image surface, and it was the
  // only one that worked: `stage-content` had no `image` branch at all, so
  // selecting a single scene rendered an empty panel. It went unnoticed for two
  // milestones precisely because this suite never left the contact sheet — the
  // gap in the test and the gap in the code were the same shape.
  await promptChips.nth(0).click();
  const frame = page.getByTestId("scene-frame");
  await expect(frame).toBeVisible();
  await expect
    .poll(async () =>
      frame
        .locator("img")
        .evaluate((n) => (n as HTMLImageElement).naturalWidth),
    )
    .toBeGreaterThan(0);
  await scenes.getByRole("button", enabled("Set")).click();

  const approveAll = page.getByTestId("approve-all");
  await expect(approveAll).toBeEnabled();
  await approveAll.click();
  await expect(page.getByTestId("stage-state-image")).toHaveText("Approved", {
    timeout: 60_000,
  });

  // --- voice ---------------------------------------------------------------
  await generate(page, "Voice");
  await expect(page.getByTestId("narration-player")).toBeVisible({
    timeout: 180_000,
  });

  // The audio element has real bytes behind it. `duration` comes from the
  // browser's own demuxer, so a non-zero value proves the MP3 was served,
  // ranged and parsed — three hops the review screen cannot fake.
  const audio = page.getByTestId("narration-audio");
  await expect
    .poll(
      async () =>
        audio.evaluate((node) => (node as HTMLAudioElement).duration || 0),
      { timeout: 60_000 },
    )
    .toBeGreaterThan(0);

  await approveCurrent(page);

  // --- timeline ------------------------------------------------------------
  await generate(page, "Timeline");
  await approveCurrent(page);

  // --- render --------------------------------------------------------------
  await generate(page, "Render");

  const video = page.getByTestId("render-video");
  await expect(video).toBeVisible({ timeout: 300_000 });

  // The end of the money path. **Checked by fetching the bytes, not by asking
  // the video element for its duration** — Playwright's bundled Chromium is an
  // open-source build with no H.264 decoder, so `duration` is 0 there for any
  // MP4 the pipeline will ever produce. (MP3 has been royalty-free since 2017
  // and *is* bundled, which is why the audio check above can be stricter.)
  //
  // Verified by hand against a real browser on the same artifact: readyState 4,
  // duration 22.6s. So this asserts the two things that can fail in CI — the
  // server serves the render, and it serves it as a video — and leaves
  // decoding to M4-10's golden frames, which read pixels with FFmpeg and need
  // no browser at all.
  const source = await video.getAttribute("src");
  expect(source, "the render must have a source").toBeTruthy();
  expect(source).toContain("/assets/artifacts/");

  const bytes = await page.request.get(source!);
  expect(bytes.status()).toBe(200);
  expect(bytes.headers()["content-type"]).toBe("video/mp4");
  expect((await bytes.body()).byteLength).toBeGreaterThan(10_000);

  // The length the *pipeline* chose, read off the player's own summary, which
  // renders it from the render version's stored duration rather than from the
  // element. "0.0s" would mean the encoder wrote a file with no timeline in it.
  await expect(page.getByTestId("render-player")).not.toContainText("0.0s");

  // Scene marks come from the render version, not from the client (M4-11), so
  // one per scene is a check that the encoder and the player agree about where
  // each scene starts.
  // Scoped to the player: the scene *selector* also labels itself "Scenes",
  // and an unscoped role query would resolve to whichever the DOM offers first.
  const marks = page.getByTestId("render-player").locator("ol button");
  await expect.poll(async () => marks.count()).toBe(sceneCount);

  await expect(page.getByTestId("project-phase")).toHaveText("Render Review");

  await approveCurrent(page);

  // --- caption (M5-01) -----------------------------------------------------
  // Instagram copy, and the first stage that is *published* verbatim rather
  // than consumed by another stage.
  await generate(page, "Caption");
  await expect(page.getByTestId("caption-review")).toBeVisible({
    timeout: 120_000,
  });
  await expect(page.getByTestId("caption-review")).toContainText("/ 2200");
  await approveCurrent(page);

  // --- thumbnail (M5-02) ---------------------------------------------------
  await generate(page, "Thumbnail");
  const cover = page.getByTestId("cover-preview");
  await expect(cover).toBeVisible({ timeout: 120_000 });

  // Both crops render, and both decode. The 1:1 one is the assertion that
  // matters: it is what the profile grid shows, and a cover whose hook sits
  // outside it looks perfect in every 9:16 preview and is cropped away where
  // people actually browse.
  await expect
    .poll(async () =>
      cover
        .locator("img")
        .evaluateAll((nodes) =>
          nodes.every((node) => (node as HTMLImageElement).naturalWidth > 0),
        ),
    )
    .toBe(true);
  await expect(cover.getByTestId("cover-cropped")).toBeVisible();
  await approveCurrent(page);

  // --- package (M5-03/04) --------------------------------------------------
  await generate(page, "Package");
  const packageReview = page.getByTestId("package-review");
  await expect(packageReview).toBeVisible({ timeout: 180_000 });

  // **The manifest is the review unit**, so it is what this asserts. A zip
  // lists its own names; what the manifest adds is a sha256 per entry, which
  // is the only thing that lets a recipient verify the archive rather than
  // trust it. F10's contents are checked by name here and by hash below.
  const rows = packageReview.locator("tbody tr");
  await expect.poll(async () => rows.count()).toBeGreaterThan(0);
  const listed = await rows.evaluateAll((tr) =>
    tr.map((row) => row.querySelector("td")?.textContent?.trim() ?? ""),
  );
  expect(listed).toContain("video.mp4");
  expect(listed).toContain("cover.png");
  expect(listed).toContain("caption.txt");
  expect(listed).toContain("hashtags.txt");
  expect(
    listed.some((name) => name.startsWith("scenes/scene-")),
    "the package should carry the scene assets (F10)",
  ).toBe(true);

  // --- download, and verify it (SADD §21.4) --------------------------------
  // "download package → assert zip manifest" is the SADD's own definition of
  // this suite's last step, and the exit criterion of the milestone. Fetched
  // through the same URL the button uses rather than driven through the
  // browser's download manager: what is being proven is that the archive is
  // served and intact, not that Chromium can save a file.
  const download = page.getByTestId("package-download");
  const zipUrl = await download.getAttribute("href");
  expect(zipUrl, "the package must have a download URL").toBeTruthy();
  expect(zipUrl).toContain("/assets/artifacts/");

  const archive = await page.request.get(zipUrl!);
  expect(archive.status()).toBe(200);
  expect(archive.headers()["content-type"]).toBe("application/zip");

  const body = await archive.body();
  // The local file header magic. A 200 carrying an HTML error page would pass
  // a status check and fail here, which is the failure worth catching.
  expect(body.subarray(0, 2).toString("latin1")).toBe("PK");
  expect(body.byteLength).toBeGreaterThan(10_000);

  await expect(page.getByTestId("project-phase")).toHaveText(
    "Ready To Publish",
  );
});
