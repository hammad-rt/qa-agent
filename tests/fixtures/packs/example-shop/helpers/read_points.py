"""Example data: read loyalty points on the fictional shop."""
from agent_tester.helpers import read_labelled_values

HELPER = {"name": "read_points", "app": "shop", "changes_data": False, "action_type": None,
          "description": "Read the points balance.", "params": {}, "needs_config": ["path", "labels"]}


async def run(browser, ctx):
    await browser.navigate(ctx.url(ctx.config["path"]))
    return {"values": read_labelled_values(await browser.snapshot(), ctx.config["labels"])}
