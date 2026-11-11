# Golden-trace fixtures (redacted only)

Every file under this tree must use **placeholders only**:

- `{client_email}`, `{client_name}`, `{login}`
- `ORDER-1`, `INVOICE-1`, `RECORD-1`
- `https://crm.example`, `https://portal.example`

Never commit real emails, names, logins, order/invoice/record numbers, or environment hosts/URLs.
`tests/test_protected_behaviours.py` and the env-leak-checker scan this folder and fail on real patterns.

## Requested laptop sources (attach redacted copies)

| Run | Need |
|---|---|
| `183498-20261105-142928` (6/6 pass) | `state.json` (results + action_log), optional `trace.jsonl` excerpts of passed discovery/tester steps |
| `165177-20261111-103004` (loops) | `trace.jsonl` / action_log for the repeated “Request a purchase quote” clicks + stuck ask / guidance / go sequence; discovery submit if present |

Place redacted JSON under the matching `pass_183498/` or `loop_165177/` directories before expanding assertions.
