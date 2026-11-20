"""Run the gflow CLI with the Flow region-block bypass built in.

Flow (flow.google.com) shows "Flow isn't available in your country" in RU/BY.
The bezmezhau-flow extension fixes that by patching Flow's main JS bundle
(www.gstatic.com/...AiSandboxAngularFrontend.../m=_b): the region flag call
`_.X(a, 31)` becomes `true` and the /unsupported-country route guard is removed.
The extension does it via chrome.debugger and re-navigates the tab, which
fights with gflow's own browser control. So here the SAME patch is applied with
Playwright request routing inside gflow's browser context instead; browser
extensions stay disabled in gflow (Playwright default).

Also: Flow may label image models "Gemini 3.1 Flash" instead of "Nano Banana 2",
so those labels are added as fallback selectors.

Usage (same arguments as `gflow`):
    python gflow_ext.py auth login --browser chrome
    python gflow_ext.py image t2i --model nano2 --aspect 1:1 --out out -- "prompt"
"""

import functools
from pathlib import Path
import re
import sys

TARGET_HOST = "www.gstatic.com"
TARGET_BUNDLE_MARKER = "AiSandboxAngularFrontend"
TARGET_BUNDLE_SUFFIX = "/m=_b"

_REGION_FLAG = re.compile(r"_\.([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+),\s*31\)")
_AGE_FLAG = re.compile(r"_\.([A-Za-z0-9_$]+)\(([A-Za-z0-9_$]+),\s*32\)")
_GUARD_REDIRECT = re.compile(
    r"if\s*\(![A-Za-z0-9_$]+\.Ma\(\)\)\s*return\s+new\s+[A-Za-z0-9_$]+"
    r"\([^;]*/unsupported-country[^;]*\);?"
)
_DROP_RESPONSE_HEADERS = {
    "age",
    "cache-control",
    "content-encoding",
    "content-length",
    "content-md5",
    "etag",
    "transfer-encoding",
}


def _log(message: str) -> None:
    print(f"[gflow_ext] {message}", file=sys.stderr, flush=True)


def patch_flow_bundle(source: str):
    """Port of bezmezhau-flow patch.js. Returns (patched_source, error)."""
    region = list(_REGION_FLAG.finditer(source))
    age = list(_AGE_FLAG.finditer(source))
    if len(region) != 1 or len(age) != 1:
        return None, f"signature mismatch: region(31)={len(region)}, age(32)={len(age)}"
    func, arg = region[0].group(1), region[0].group(2)
    if (func, arg) != (age[0].group(1), age[0].group(2)):
        return None, "flag accessor mismatch between 31 and 32"
    target = re.compile(rf"_\.{re.escape(func)}\({re.escape(arg)},\s*31\)")
    patched = target.sub("true", source)
    patched, guards = _GUARD_REDIRECT.subn("/* bypass unsupported-country */", patched, count=1)
    if not guards:
        _log("route guard pattern not matched, relying on flag override")
    return patched, None


def is_target_bundle(url: str) -> bool:
    # Same as the extension's pattern https://www.gstatic.com/*AiSandboxAngularFrontend*/m=_b
    base = str(url or "").split("?", 1)[0].split("#", 1)[0]
    return (
        base.startswith(f"https://{TARGET_HOST}/")
        and TARGET_BUNDLE_MARKER in base
        and base.endswith(TARGET_BUNDLE_SUFFIX)
    )


async def _handle_bundle(route):
    try:
        response = await route.fetch()
        if not 200 <= response.status < 300:
            await route.fulfill(response=response)
            return
        source = await response.text()
        patched, error = patch_flow_bundle(source)
        if error:
            _log(f"bundle not patched ({error}); passing original through")
            await route.fulfill(response=response)
            return
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() not in _DROP_RESPONSE_HEADERS
        }
        headers["cache-control"] = "no-store"
        await route.fulfill(status=response.status, headers=headers, body=patched)
        _log("Flow region check patched")
    except Exception as exc:  # never break page loading because of the patch
        _log(f"bundle patch failed: {exc}")
        try:
            await route.continue_()
        except Exception:
            pass


def _patch_browser_type(module_name: str) -> None:
    try:
        module = __import__(module_name, fromlist=["BrowserType"])
        browser_type = module.BrowserType
    except Exception:
        return
    original = browser_type.launch_persistent_context
    if getattr(original, "_gflow_ext_patched", False):
        return

    @functools.wraps(original)
    async def launch_persistent_context(self, *args, **kwargs):
        # Playwright request routing does not see requests served by a page
        # service worker; block service workers so the bundle always hits the route.
        kwargs.setdefault("service_workers", "block")
        context = await original(self, *args, **kwargs)
        try:
            await context.route(is_target_bundle, _handle_bundle)
        except Exception as exc:
            _log(f"could not install Flow bundle route: {exc}")
        return context

    launch_persistent_context._gflow_ext_patched = True
    browser_type.launch_persistent_context = launch_persistent_context


_patch_browser_type("playwright.async_api")
_patch_browser_type("patchright.async_api")


def _add_model_label_fallbacks() -> None:
    """Flow may label image models by their Gemini names instead of
    "Nano Banana ...". Add those labels as fallbacks (tried after the
    original selectors, so nothing changes when the old labels still exist)."""
    try:
        from gflow_cli.api.image import Model
        from gflow_cli.api.transports import ui_automation
    except Exception:
        return
    table = getattr(ui_automation, "IMAGE_MODEL_OPTION_SELECTORS", None)
    if not isinstance(table, dict):
        return
    extra = {
        "NARWHAL": (
            "[role='menuitem']:has-text('Gemini 3.1 Flash'):not(:has-text('Lite'))",
        ),
        "HARBOR_SEAL": ("[role='menuitem']:has-text('Gemini 3.1 Flash Lite')",),
        "GEM_PIX_2": ("[role='menuitem']:has-text('Gemini 3 Pro')",),
    }
    for name, selectors in extra.items():
        model = getattr(Model, name, None)
        if model is None:
            continue
        current = tuple(table.get(model, ()))
        table[model] = current + tuple(s for s in selectors if s not in current)


_add_model_label_fallbacks()


def _patch_agent_image_settings() -> None:
    """Support the Agent panel's separate Image defaults screen."""
    from gflow_cli.api.transports import _mc_editor_gate, agent_only_composer as agent

    header_settings = "flow-agent-panel button:visible:has(mat-icon:text-is('settings_2'))"
    settings = f"{_mc_editor_gate.AGENT_ONLY_ANCHOR}, {header_settings}"
    _mc_editor_gate.AGENT_ONLY_ANCHOR = settings
    agent.SETTINGS_BUTTON = settings
    image_defaults = "flow-agent-panel flow-generation-defaults-view"
    agent.TOGGLE_GROUP = f"{agent.TOGGLE_GROUP}, {image_defaults} mat-button-toggle-group"
    gate = _mc_editor_gate.EditorGateMixin
    detect_agent = gate._is_agent_only_composer

    async def detect_agent_panel(page, trigger):
        if await trigger.count() and not await trigger.is_visible():
            chip = page.locator(_mc_editor_gate.AGENT_MODE_CHIP_ANY)
            open_panel = page.locator("button:visible:has(mat-icon:text-is('right_panel_open'))")
            if not await chip.count() and await open_panel.count() == 1:
                await agent._click(open_panel, "the Agent panel button")
                await page.locator(header_settings).wait_for(state="visible", timeout=8000)
        return await detect_agent(page, trigger)

    gate._is_agent_only_composer = staticmethod(detect_agent_panel)

    class AgentImageSettings(agent.AgentOnlyComposer):
        async def _open_image_defaults(self, page):
            pane = page.locator(image_defaults)
            if await pane.is_visible():
                return pane
            entry = page.locator("flow-agent-panel").get_by_text("Image defaults", exact=True)
            if not await entry.is_visible():
                await agent._click(page.locator(header_settings), "Agent settings")
            await agent._click(entry, "Image defaults")
            await pane.wait_for(state="visible", timeout=8000)
            return pane

        async def _close_image_defaults(self, page):
            back = page.locator(agent.PANE_BACK)
            await agent._click(back, "Back from Image defaults")
            await agent._click(back, "Back from Agent settings")
            await page.locator(header_settings).wait_for(state="visible", timeout=8000)

        async def _select_image_model(self, pane, wants):
            labels = pane.locator("flow-radio-group.model-radio-group label:has(input[type=radio])")
            names = [label.strip().splitlines()[-1] for label in await labels.all_inner_texts()]
            matches = [i for i, name in enumerate(names) if wants(name)]
            if len(matches) != 1:
                raise agent.ConfigurationError(detail=f"Image defaults offers: {', '.join(names)}")
            choice = labels.nth(matches[0])
            if not await choice.locator("input:checked").count():
                await agent._click(choice, f"the {names[matches[0]]} image model")
                await choice.locator("input:checked").wait_for(state="attached", timeout=5000)

        async def apply_defaults(self, page, *, section, aspect_ligature, count, model, confirm):
            self._uses_image_defaults = (
                section == "image" and await page.locator(header_settings).is_visible()
            )
            if not self._uses_image_defaults:
                return await super().apply_defaults(
                    page, section=section, aspect_ligature=aspect_ligature,
                    count=count, model=model, confirm=confirm,
                )

            with agent.nothing_submitted():
                pane = await self._open_image_defaults(page)
                selected_model = pane.locator("flow-radio-group.model-radio-group label:has(input:checked)")
                snapshot = agent.DefaultsSnapshot(
                    section=section,
                    aspect_index=await self._checked_index(page, 0),
                    count_index=await self._checked_index(page, 1),
                    model_text=(await selected_model.inner_text(timeout=5000)).strip().splitlines()[-1],
                )
                if snapshot.aspect_index < 0 or snapshot.count_index < 0:
                    raise agent.UiSelectorDriftError(detail="Image defaults has no selected aspect or count")
                try:
                    if model is not None:
                        await self._select_image_model(pane, model.matches)
                    await self._check_radio(page, 0, aspect_ligature, "aspect")
                    await self._check_radio(page, 1, count - 1, "count")
                    await self._close_image_defaults(page)
                except Exception:
                    await agent._restore_quietly(self, page, snapshot)
                    raise
                return snapshot

        async def restore_defaults(self, page, snapshot):
            if not self._uses_image_defaults:
                return await super().restore_defaults(page, snapshot)
            pane = await self._open_image_defaults(page)
            try:
                await self._select_image_model(pane, lambda name: name == snapshot.model_text)
                await self._check_radio(page, 0, snapshot.aspect_index, "aspect")
                await self._check_radio(page, 1, snapshot.count_index, "count")
            finally:
                await self._close_image_defaults(page)

    agent.AgentOnlyComposer = AgentImageSettings


_patch_agent_image_settings()


def _patch_reference_mentions() -> None:
    """Fix multi-reference attach on flow.google.com's classic composer.

    gflow types the next '@name' right after the previous mention chip; Flow's editor
    only opens the mention picker after whitespace, so the 2nd+ reference ended up as
    plain text. Here every mention is typed at the END of the prompt editor and is
    preceded by a space."""
    try:
        from gflow_cli.api.transports import migrated_composer as mc
    except Exception:
        return
    cls = getattr(mc, "MigratedComposer", None)
    if cls is None or getattr(cls._mention_by_name, "_gflow_ext_patched", False):
        return

    async def _editor(page):
        main = page.locator("flow-rich-text-editor.prompt-input [contenteditable='true']")
        return main.first if await main.count() else page.locator(mc.COMPOSER).first

    async def _wait_until(page, check, timeout_ms, step_ms=150):
        waited = 0
        while True:
            if await check():
                return True
            if waited >= timeout_ms:
                return False
            await page.wait_for_timeout(step_ms)
            waited += step_ms

    async def _mention_by_name(self, page, name, *, expect_chips):
        # Fast path: type quickly, then wait for the picker to actually offer the
        # file (instead of fixed multi-second sleeps), commit, and poll for the chip.
        offered = []
        chips = await self.read_chips(page)
        attempts = getattr(mc, "FRAME_SEARCH_ATTEMPTS", 3)
        options = page.locator(mc.PICKER_OPTION)
        tag = re.search(r"-([0-9a-f]{8})(?:\.[a-z0-9]+)?$", name)
        key = (tag.group(1) if tag else name).lower()  # unique upload tag, robust to truncation

        async def name_offered():
            nonlocal offered
            try:
                offered = [t.strip() for t in await options.all_text_contents()]
            except Exception:
                offered = []
            return any(key in t.lower() for t in offered)

        for attempt in range(1, attempts + 1):
            before = len(chips)
            slow = attempt > 1  # retry with the original, slower timing
            await (await _editor(page)).click(timeout=5000)
            await page.keyboard.press("Meta+ArrowDown" if sys.platform == "darwin" else "Control+End")
            await page.keyboard.type(" ", delay=30)
            await page.keyboard.type("@", delay=60)
            await page.wait_for_timeout(2200 if slow else 500)
            await page.keyboard.type(name, delay=100 if slow else 25)
            found = await _wait_until(page, name_offered, 8000 if slow else 6000)
            if found and not slow:
                await page.wait_for_timeout(250)  # let the highlighted row settle
            await page.keyboard.press("Enter")
            got = await _wait_until(
                page, lambda: _chip_count_is(self, page, expect_chips), 4000)
            chips = await self.read_chips(page)
            if got or len(chips) == expect_chips:
                await page.keyboard.type(" ", delay=30)
                _log(f"reference {len(chips)}/{expect_chips} attached")
                return
            _log(f"reference mention miss: attempt={attempt} chips={len(chips)} offered={len(offered)}")
            if attempt == attempts:
                break
            for _ in range(len(name) + 1):  # the '@' and the query it opened
                await page.keyboard.press("Backspace")
            chips = await self.read_chips(page)
            if len(chips) < before:
                raise mc.ReferenceNotFoundError(
                    detail=f"clearing the failed {name!r} query removed an attached reference"
                )
            await page.wait_for_timeout(int(getattr(mc, "FRAME_SEARCH_RETRY_PAUSE_S", 2.0) * 1000))
        raise mc.ReferenceNotFoundError(
            detail=(
                f"{name!r} did not attach as a reference in {attempts} attempts "
                f"({len(chips)} chip(s), expected {expect_chips}); offered: {', '.join(offered[:6]) or '<nothing>'}"
            )
        )

    async def _chip_count_is(composer, page, n):
        try:
            return len(await composer.read_chips(page)) == n
        except Exception:
            return False

    _mention_by_name._gflow_ext_patched = True
    cls._mention_by_name = _mention_by_name


_patch_reference_mentions()


def _patch_inline_upscale() -> None:
    """GFLOW_EXT_UPSCALE=2k: after t2i/i2i has downloaded its images, upscale each one
    to 2K in the SAME browser session (no second Chrome launch). Saved next to the
    original as <name>_2k.<ext>. A failed upscale never fails the generation; the
    worker then falls back to a separate upscale run or the 1K original."""
    import os

    try:
        from gflow_cli import cli_image
        from gflow_cli.api.image_upscale import TargetResolution
    except Exception:
        return
    original = getattr(cli_image, "_generate_verify_download", None)
    if original is None or getattr(original, "_gflow_ext_patched", False):
        return

    @functools.wraps(original)
    async def _generate_verify_download(client, **kwargs):
        images, saved_paths = await original(client, **kwargs)
        if os.environ.get("GFLOW_EXT_UPSCALE", "").lower() != "2k":
            return images, saved_paths
        project_id = kwargs.get("project_id")
        total = len(images)
        for number, (img, path) in enumerate(zip(images, saved_paths), start=1):
            try:
                path = Path(str(path))
                target = await client.upsample_image(
                    media_id=img.media_name,
                    project_id=project_id,
                    target_resolution=TargetResolution.RES_2K,
                    out_path=path.with_name(f"{path.stem}_2k.png"),
                )
                _log(f"2k {number}/{total} saved: {target}")
            except Exception as exc:  # keep the 1K result
                _log(f"2k {number}/{total} failed: {type(exc).__name__}: {str(exc)[:200]}")
        return images, saved_paths

    _generate_verify_download._gflow_ext_patched = True
    cli_image._generate_verify_download = _generate_verify_download


_patch_inline_upscale()

if __name__ == "__main__":
    from gflow_cli.cli import main

    sys.argv[0] = "gflow"
    sys.exit(main())
