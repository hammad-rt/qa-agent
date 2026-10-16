"""Drafted by the tester in TC1 on 2026-10-15 and approved by the user.
Edit or delete freely."""
import json

HELPER = {
 "name": "type_and_verify",
 "app": "any",
 "description": "To test if a specific key (like . or ,) is blocked when typing in a field, and verify the final value.",
 "changes_data": False,
 "action_type": "UI Interaction",
 "params": {
  "target": "<element ref from the current page read>",
  "text1": "12",
  "key": ".",
  "text2": "34"
 },
 "needs_config": []
}

JS = r'''async (page, args) => {
  const locator = page.locator(`aria-ref=${args.target}`);
  await locator.fill('');
  await page.keyboard.type(args.text1);
  await page.keyboard.press(args.key);
  await page.keyboard.type(args.text2);
  return await locator.inputValue();
}'''


async def run(browser, ctx, **params):
    code = "async (page) => { const args = " + json.dumps(params) + "; return await (" + JS + ")(page, args); }"
    out = await browser._do("browser_run_code_unsafe", {"code": code})
    return {"values": {}, "screenshots": [], "notes": str(out)[:1500]}
