"""Change an order's status in the CRM. Data-changing: runs only after approval (action type move_order_status). Labels and paths come from
pack.yaml helpers.crm_move_order_status; until they are confirmed on the real UI the helper stays disabled."""
from agent_tester.helpers import run_form

HELPER = {"name": "crm_move_order_status", "app": "crm", "changes_data": True, "action_type": "move_order_status",
          "description": "Change an order's status in the CRM.", "params": {'order': 'order number', 'status': 'new status'},
          "needs_config": ["path", "submit_label"]}


async def run(browser, ctx, **params):
    missing = [p for p in HELPER["params"] if p not in params]
    if missing:
        return {"error": f"missing parameter(s): {', '.join(missing)}"}
    ctx.config.setdefault("name", HELPER["name"])
    return await run_form(browser, ctx, params)
