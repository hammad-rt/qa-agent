"""Apply rewards to an order on the portal. Data-changing: runs only after approval (action type apply_rewards). Labels and paths come from
pack.yaml helpers.portal_apply_rewards; until they are confirmed on the real UI the helper stays disabled."""
from agent_tester.helpers import run_form

HELPER = {"name": "portal_apply_rewards", "app": "portal", "changes_data": True, "action_type": "apply_rewards",
          "description": "Apply rewards to an order on the portal.", "params": {'order': 'order number', 'amount': 'rewards amount'},
          "needs_config": ["path", "submit_label"]}


async def run(browser, ctx, **params):
    missing = [p for p in HELPER["params"] if p not in params]
    if missing:
        return {"error": f"missing parameter(s): {', '.join(missing)}"}
    ctx.config.setdefault("name", HELPER["name"])
    return await run_form(browser, ctx, params)
