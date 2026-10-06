"""Read the customer's rewards wallet figures on the portal (read-only)."""
from agent_tester.helpers import HelperError, read_labelled_values

HELPER = {"name": "read_wallet", "app": "portal", "changes_data": False, "action_type": None,
          "description": "Read Current / Total / Used rewards on the portal rewards page.",
          "params": {}, "needs_config": ["path", "labels"]}


async def run(browser, ctx):
    await browser.navigate(ctx.url(ctx.config["path"]))
    page = await browser.snapshot()
    values = read_labelled_values(page, ctx.config["labels"])
    if not values:
        raise HelperError(f"none of {ctx.config['labels']} found on {ctx.config['path']}")
    shot = await browser.screenshot(f"{ctx.shot_prefix}{ctx.case_id}-wallet.png")
    return {"values": values, "screenshots": [shot]}
