"""Add a parcel to an order in the CRM. Data-changing: runs only after approval (action type add_parcel). Labels and paths come from
pack.yaml helpers.crm_add_parcel; until they are confirmed on the real UI the helper stays disabled."""
from agent_tester.helpers import run_form

HELPER = {"name": "crm_add_parcel", "app": "crm", "changes_data": True, "action_type": "add_parcel",
          "description": "Add a parcel to an order in the CRM.", "params": {'order': 'order number', 'weight': 'parcel weight', 'shipping': 'shipping charge'},
          "needs_config": ["path", "submit_label"]}


async def run(browser, ctx, **params):
    missing = [p for p in HELPER["params"] if p not in params]
    if missing:
        return {"error": f"missing parameter(s): {', '.join(missing)}"}
    ctx.config.setdefault("name", HELPER["name"])
    return await run_form(browser, ctx, params)
