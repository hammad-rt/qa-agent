"""Create a test order on the portal. Data-changing: runs only after approval (action type create_test_order). Labels and paths come from
pack.yaml helpers.portal_create_order; until they are confirmed on the real UI the helper stays disabled."""
from agent_tester.helpers import run_form

HELPER = {"name": "portal_create_order", "app": "portal", "changes_data": True, "action_type": "create_test_order",
          "description": "Create a test order on the portal.", "params": {'item': 'item name or link', 'quantity': 'quantity'},
          "needs_config": ["path", "submit_label"]}


async def run(browser, ctx, **params):
    missing = [p for p in HELPER["params"] if p not in params]
    if missing:
        return {"error": f"missing parameter(s): {', '.join(missing)}"}
    ctx.config.setdefault("name", HELPER["name"])
    return await run_form(browser, ctx, params)
