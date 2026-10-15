"""Solve the reCAPTCHA picture puzzle with the vision model (staging/pre-prod only). The browser reads the
challenge frame and clicks tiles; the model only says which tile numbers match. Any failure returns False and
the caller falls back to manual login."""
from __future__ import annotations

import asyncio
import json
import os

import re

_BFRAME = r"page.frames().find(f => /recaptcha\/(api2|enterprise)\/bframe/.test(f.url()))"

READ_JS = r"""async (page) => {
  const b = %s;
  if (!b || !(await (await b.frameElement()).isVisible())) return JSON.stringify({open: false});
  const desc = (await b.locator('.rc-imageselect-desc-wrapper').innerText().catch(() => '')).replace(/\s+/g, ' ').trim();
  const tiles = await b.locator('table[class*="rc-imageselect-table"] td').count();
  const shot = await b.locator('.rc-imageselect-challenge').screenshot({timeout: 8000});
  return JSON.stringify({open: true, desc, tiles, image: shot.toString('base64')});
}""" % _BFRAME

CLICK_JS = r"""async (page) => {
  const b = %s; const picks = __PICKS__;
  const td = b.locator('table[class*="rc-imageselect-table"] td');
  for (const i of picks) { await td.nth(i - 1).click({timeout: 4000}); await page.waitForTimeout(250 + Math.random() * 350); }
  return JSON.stringify({clicked: picks.length});
}""" % _BFRAME

VERIFY_JS = r"""async (page) => {
  const b = %s;
  await b.locator('#recaptcha-verify-button').click({timeout: 5000});
  await page.waitForTimeout(2500);
  const anchor = page.frames().find(f => /recaptcha\/(api2|enterprise)\/anchor/.test(f.url()));
  const passed = anchor ? (await anchor.locator('#recaptcha-anchor').getAttribute('aria-checked')) === 'true' : false;
  return JSON.stringify({passed});
}""" % _BFRAME


def question(desc: str, tiles: int) -> str:
    side = 4 if tiles == 16 else 3
    return (f"This is a reCAPTCHA image grid of {side}x{side} tiles, numbered 1 to {tiles} left to right, top to "
            f"bottom. The instruction is: \"{desc}\". Which tile numbers contain the requested object (any part "
            "of it)? Answer only with a JSON list of numbers, e.g. [2, 5, 9], or [] if none.")


def parse_result(out: str) -> dict:
    """Playwright MCP replies '### Result' + a JSON-encoded string."""
    m = re.search(r"### Result\s*\n(.+)", out or "")
    try:
        v = json.loads((m.group(1) if m else out or "").strip())
        v = json.loads(v) if isinstance(v, str) else v
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


async def _js(browser, code: str) -> dict:
    return parse_result(await browser._do("browser_run_code_unsafe", {"code": code}))


async def solve(browser, env: str = "staging", ask=None, rounds: int | None = None) -> bool:
    if env == "prod" or os.environ.get("CAPTCHA_SOLVE_IMAGES", "1") == "0":
        return False
    if ask is None:
        from agent_tester import vision
        ask = lambda img, q: asyncio.to_thread(vision.ask, img, q)  # noqa: E731
    from agent_tester.vision import tile_numbers
    from agent_tester import tile_model
    rounds = rounds or int(os.environ.get("CAPTCHA_ROUNDS", "3"))
    for _ in range(rounds):
        try:
            ch = await _js(browser, READ_JS)
            if not ch.get("open"):
                return False
            # "click until there are none left": tiles are replaced, so look again after each pass (max 4 passes)
            for _pass in range(4):
                picks = await asyncio.to_thread(tile_model.classify, ch["image"], ch["tiles"], ch["desc"])
                if picks is None:                     # local model can't answer: ask the vision LLM
                    picks = tile_numbers(await ask(ch["image"], question(ch["desc"], ch["tiles"])), ch["tiles"])
                if picks:
                    await _js(browser, CLICK_JS.replace("__PICKS__", json.dumps(picks)))
                if not picks or "none left" not in ch["desc"].lower():
                    break
                await asyncio.sleep(4)          # replacement tiles fade in
                ch = await _js(browser, READ_JS)
                if not ch.get("open"):
                    break
            if (await _js(browser, VERIFY_JS)).get("passed"):
                return True
        except Exception:  # noqa: BLE001  (model down, frame changed, ...): give up and let a person do it
            return False
    return False
