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
- Staging CRM: `maria.tariq`. Was not an admin on 25 Sep, became admin by 28 Sep. Check the role each run.
- Staging portal: `sidney@savostore.com`. Check the account currency (USD or KES) each run.
- Production: no accounts known yet. Ask.

## Payments
- Stripe test card on staging: 4242 4242 4242 4242, expiry 12/34, CVC 123.
- Buy & Ship (B&S) orders on staging offer only M-Pesa, so card payment is blocked for them.
- M-Pesa: skip unless the user says otherwise.

## Business rules
- Rewards earn rate: 5% of each order. Earned when the order is paid and moved to "Order Delivered" in the CRM.
- Redemption cap: rewards cover at most 10% of the shipping price, not the order total.
- Adding a parcel to an already-paid order: the rewards limit is based only on the new unpaid shipping (confirmed intended by the user; for example a $400 parcel gives a $40 limit on order A1158).
- A rewards balance of at least $100 or 50,000 KES is needed for the redemption cases.
- Mandatory Shipment Protection (#180966) applies above a threshold set in the CRM. Production threshold: $150 (as of 30 Sep). Test at the threshold and just above it (150 and 151).

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
