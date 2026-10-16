# Savostore project pack: facts

Rewritten 2026-10-06 after the original file was lost. Sources: QA runs on #180966 (25 Sep), #165177 and #165348 (28-30 Sep), the user's confirmations, and the 28 Sep report email.

## Tracker
- Redmine: https://redmine.rolustech.com, project 1567.
- Test cases live in a child task named "Test Cases - ...".
- Staging addresses are on the wiki page "Staging_Cardentials". The wiki has no production app logins. The wiki holds plaintext secrets; never copy them into reports or memory.

## Environments
| | Staging | Production |
|---|---|---|
| Customer portal | https://staging5.rolustech.com:4225/ | https://app.savostore.com/ |
| CRM (SuiteCRM) | https://staging5.rolustech.com:4220/ | https://savo.store/ |

## Accounts (passwords never stored here)
- All Redmine posts (notes, time entries, bugs) go through Hammad Iqbal's account, never Sidney's.
- Staging CRM: `maria.tariq`. Was not an admin on 25 Sep, became admin by 28 Sep. Check the role each run.
- Staging portal: `sidney@savostore.com`. Check the account currency (USD or KES) each run.
- Production: no accounts known yet. Ask.

## Payments
- Stripe test card on staging: 4242 4242 4242 4242, expiry 12/34, CVC 123.
- Buy & Ship (B&S) orders on staging offer only M-Pesa, so card payment is blocked for them.
- Classic Buy & Ship is paid by bank transfer. Stripe (card) tests for non-Express orders use Shipping Only orders instead: pay an EXISTING unpaid Shipping Only order with the 4242 card; don't create new orders for this (confirmed by Hammad 2026-10-15).
- M-Pesa: skip unless the user says otherwise.

## Modules (portal)
- Shipping Only is its own module with its own menu in the portal, separate from Buy & Ship (confirmed 2026-10-15). In the CRM, Shipping Only orders are `AOS_Invoices`; Buy & Ship are `AOS_Quotes`.
- Savo Express: the order is created only after payment. Other order types (Shipping Only, Buy & Ship) still get an order ID up front.
- Adding to the cart opens an "Added to cart" prompt with Checkout and Continue shopping.
- Savo Express prices are shown as they are, with no "est." or "estimated" label (#182698 won't do). Don't flag a missing "estimated" label.
- Intercom on the portal stays as it is. Don't flag it or suggest changing it.

## Routes (staging, verified 2026-10-16; order numbers and record ids below are placeholders, look them up each run)
Portal base https://staging5.rolustech.com:4225
- Home `/home`. Account: `/rewards`, `/profile`, `/change-password`, `/warehouse-addresses`, `/delivery-addresses`.
- Shipping Only: list `/shipping-only`, create `/shipping-only/create`, open an order `/shipping-only?editedOrder=<A-number>`, invoice/payment `/shipping-only/invoice/<crm record id>`.
- Buy & Ship: list `/buy-and-ship`, purchase-quote form `/buy-and-ship/create`, open an order `/buy-and-ship?editedOrder=<B-number>`, edit `/buy-and-ship/edit/<record id>?orderNumber=<B-number>`, invoice/payment `/buy-and-ship/invoice/<record id>`.
- Savo Express: `/savo-express`, cart `/savo-express/cart`. Express orders appear in the Buy & Ship list as BX numbers.
- Orders have no detail page: `editedOrder` opens the order's card expanded in the list.

CRM base https://staging5.rolustech.com:4220/index.php
- Shipping Only orders `module=AOS_Invoices`; Buy & Ship (incl. BX) `module=AOS_Quotes`; Payments `module=sav_payments`; Clients `module=Contacts`; Rewards Redeemed `module=sav_rewards_redeemed`; Loyalty Rewards `module=sav_rewards`.
- Record page `?module=<M>&action=DetailView&record=<id>`. The CRM record id is the same id the portal invoice/edit URLs use.
- Admin settings (open from Administration `action=index`; a direct URL gives an XSRF error): `saveRewardsPanel` (Savo Rewards), `saveShipmentProtection`, `saveExpressCategoryWeightDefaults`, `saveExpressPricingSettings`.

## Business rules
- Rewards earn rate: 5% of each order. Earned when the order is paid and moved to "Order Delivered" in the CRM.
- Redemption cap: rewards cover at most 10% of the shipping price, not the order total.
- Adding a parcel to an already-paid order: the rewards limit is based only on the new unpaid shipping (confirmed intended by the user; for example a $400 parcel gives a $40 limit on order A1158).
- A rewards balance of at least $100 or 50,000 KES is needed for the redemption cases.
- Mandatory Shipment Protection (#180966) applies above a threshold set in the CRM. Production threshold: $150 (as of 30 Sep). Test at the threshold and just above it (150 and 151).

## Savo Rewards: CRM facts (verified 2026-10-12)
- Earn/redeem config: CRM `index.php?module=Administration&action=saveRewardsPanel`; fields "Rewards Earned (Percentage)" (normal 5) and "Rewards Redeemed (Percentage)" (normal 10). Changing them is a settings change: needs approval, record the original, restore after.
- Shipping-only orders are `AOS_Invoices` records; Buy & Ship orders are `AOS_Quotes`.
- Admin redemption: order DetailView > "Rewards Redeemed" subpanel > Create (name "Redeemed rewards by admin against <order>"; Redeemed Rewards editable, Applicable Rewards read-only).
- Client: portal user "Hammad" = CRM client Hammad Iqbal (not "Sidney Test"). Match the client by the orders it owns, not by name. Contact fields: Total Rewards, Current Rewards, Rewards Used.
- Earn happens only on the status change to Order Delivered (re-triggering overwrites). Earn = 5% of shipping after rewards (pending product confirmation).
- Redeem cap = 10% of unpaid shipping; 0 once paid >= shipping.
- Buy & Ship rewards box shows only at Awaiting Payment / Shipping Fee Invoiced; USD shows only Pay to Bank Account. Shipping ~$10/cu ft, $35 minimum.
- Order fit check before any rewards case: invoice exists, balance > 0, unpaid shipping > 0, client matches. A paid order (e.g. A1148) is unusable; prepare a fresh shipping-only order with an invoice and unpaid shipping (needs approval).
- Timing: apply/remove takes 3-5s; the rewards box shows a false "no rewards"/"up to $15.00" for 1-2s on load; CRM subpanels update ~8s late. Wait and recheck before any verdict.
- Stripe path: Pay By Card > /shipping-only/stripe/<id>?appliedRewards=N > second "Pay by Card" > Stripe; choose USD; test card from the Payments section.

## CRM login (SuiteCRM)
- Login fields are `user_name` and `username_password`; the button is `#bigbutton` (Enter also submits).
- Data setup on staging needs the Administrator account (`admin`); `maria.tariq` is not an admin.
- If login_form fails, do not retry by clicking Log In by hand; finish with notes "BLOCKED: CRM login failed".

## Verified flows (from manual retest, 2026-10-12; follow these, check each step against the page)
**Open a CRM order directly:** browser_navigate to `<crm URL>/index.php?module=AOS_Invoices&action=DetailView&record=<record id>` (same id as the portal invoice, read in this run). Don't click the order-number link in lists; it often doesn't navigate.

**Paying a shipping-only invoice:** use the **Pay By Card** button on the invoice page (opens `/shipping-only/stripe/<id>?appliedRewards=N`, then a second "Pay by Card" goes to Stripe; recipe R4). Do not click "Make a Payment" on these invoices; it does nothing useful.

**R1 Prepare a shipping-only order (A1142):** CRM: open the client's shipping-only order (AOS_Invoices) -> open its parcel, set Received, Save -> move status one step at a time, saving each: Shipped to Warehouse, On Warehouse floor, Ready to Ship, Shipped, Invoiced & Shipped. Portal: open `/shipping-only/invoice/<CRM record id>` directly (read the id from the CRM URL `record=` in the same run; never reuse an old id; "View Invoice" is not needed), wait ~4 s, check the balance, the Pay By Card button and rewards "up to" (= 10% of unpaid shipping; A1142: $44.00, up to $3.50).
**R2 Buy & Ship (B1465):** AOS_Quotes record; the rewards box shows only at Awaiting Payment or Shipping Fee Invoiced. In USD the only option is Pay to Bank Account; there is no currency switch, so KES/M-Pesa cases are BLOCKED.
**R3 Apply/remove rewards (portal):** type the amount, click Apply, wait 3-5 s before reading. Remove with the X, then wait again. An amount over the cap is rejected with a message that disappears after a few seconds (read it right away).
**R4 Stripe (staging only):** Pay By Card -> `/shipping-only/stripe/<id>?appliedRewards=N` -> click the second "Pay by Card" -> on Stripe choose USD -> card 4242 4242 4242 4242, expiry 12/34, CVC 123, cardholder name (required). For B6, leave Stripe without paying and check the wallet did not drop.
**R5 Wallet check:** portal wallet balance; CRM Contacts > <client> -> Total Rewards, Current Rewards, Rewards Used (wait ~10 s, reload if empty). Redemptions are in the order's Rewards Redeemed subpanel.
**R6 Admin redemption:** CRM order -> Rewards Redeemed subpanel -> Create -> Redeemed Rewards <= Applicable Rewards -> Save. On A1142 this returned HTTP 500: after two failures take a screenshot and record FAIL.
**R7 Earn rewards (staging only):** pay an order (R4) -> CRM: set it to Order Delivered (earn = 5% of shipping after rewards). To earn faster raise the earn % under Administration > Rewards panel, then restore 5% earn / 10% redeem.
- Not verified: creating a brand-new order of either type. Explore it from the page and propose a recipe.

## UI recipes
- Build a rewards balance: create a test order, pay it with the 4242 card, move it to Order Delivered in the CRM, then check the wallet.
- After applying rewards on the portal, reload twice before reading values (first reload can be stale).

## Known open bugs (drafts from the 28 Sep report, not posted; report as "still present: bug N")
1. [Critical] Portal rewards are committed (Used, wallet deducted) when Stripe checkout opens, not after payment. Abandoning checkout burns rewards.
2. [High] "You can use up to" and the replacement limit are computed on the rewards-reduced amount: $60 applied on $1,000 shows $34 (expected $40); $15 on $400 unpaid shows $23.50 (expected $25). Confirmed real by the user.
3. [High] CRM admin Rewards Redeemed saves as Applied; wallet not deducted on save; Used never incremented; status never becomes Used.
4. [High] Pre-existing wallet drift: Current balance below Total minus Used (20.50, grew to 120.50).
5. [Medium] Portal apply does not persist when an admin redemption already exists on the order.
6. [Medium] Stale values on the first reload after apply.
7. [Medium] Buy & Ship: only M-Pesa offered, the exact decimal maximum is rejected, the total label does not update.
8. [Low] Wallet line still shown after removing rewards with the X button.
9. [Low] Invalid discount code gives no feedback.

## Not reproducible from the UI
- Webhook retry and orphan pending redemptions (#165348 A13, C11; #165177 B17).

## Test history
- #165177 / #165348 on staging: #165348 13 of 17 pass; #165177 5 pass, 9 fail, 3 partial, 1 not verified, 1 skipped. Report PDF: `qa/rewards/report/savo-rewards-qa-report-2026-09-28.pdf` (original; the later 97-page version with notes is in the user's 1:1 chat).
- #180966 on staging (25 Sep): portal cases run against the existing threshold; admin threshold cases blocked because the CRM account was not admin then. Production testing waits on accounts and approvals.

## Shipment Protection behaviour (confirmed on staging 2026-10-14)
- CRM Shipment Protection threshold rejects 12.5, 200.5, 0, -5, abc and blank silently: the form shows the typed value again with no message, and reopening the page shows the old value. Whole numbers like 200 save. Check the result by reloading, not by looking for a message.
- The rate accepts decimals (e.g. 2.5); the minimum accepts 0.
- Rounding is intended: the CRM protection cost shows whole dollars (4.02 shows as 4.00) while the USD field keeps cents.
- A client exemption always overrides the threshold. Declining protection means no charge anywhere.
- Portal Parcel Value drops decimals (200.50 becomes 200, pasting 25.50 gives 25) and 1,000 becomes 1000. 0 shows "Enter a value greater than zero"; typing "-" shows the "Whole dollars only" hint.
- Buy & Ship orders have no protection step.
- Fixing old orders (TC7 of #182941) is a repair script run on production; it can't be tested on staging.
