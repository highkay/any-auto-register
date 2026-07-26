# Z.ai Chat API Latest Contract

Verified on `2026-05-12` against the live `https://chat.z.ai` web app with a freshly registered non-guest `user` token.

This document records only what was re-verified from live browser traffic and direct HTTP replay. It is intentionally narrow: the goal is to describe the current chat interface contract, what is actually required, and what earlier assumptions were disproven.

## Summary

- Canonical chat endpoint:
  - `POST https://chat.z.ai/api/v2/chat/completions`
- `GET` and `OPTIONS` on the same path still return `405 Method Not Allowed`.
- A valid non-guest bearer token is required.
- The current direct-call hard requirement is the frontend version header:
  - `X-FE-Version: prod-fe-1.1.29`
- The browser sends many extra query parameters, cookies, and headers, but current direct HTTP validation shows they are not all required for a successful basic chat call.
- The browser currently includes `captcha_verify_param` in chat requests, but direct replay showed a stale captured value causes:
  - `FRONTEND_CAPTCHA_REQUIRED`
  - `verify_code=F018` or `F019`
- For direct programmatic calls, omitting `captcha_verify_param` is currently safer than replaying a stale browser value.

## Proven Contract

### 1. Endpoint and method

Current working path:

```text
POST /api/v2/chat/completions
```

Negative checks that were re-verified:

- `GET /api/v2/chat/completions` -> `405`
- `OPTIONS /api/v2/chat/completions` -> `405`

So if a client is still seeing `405`, the first thing to check is whether it is using `POST`.

### 2. Auth precondition

Use a real `user` token, not a pre-login or `guest` token.

The repository now produces a real registered account token. Before chat calls, the safest sequence is:

1. Start from the stored bearer token.
2. Refresh once via:
   - `GET https://chat.z.ai/api/v1/auths/`
   - `Authorization: Bearer <stored token>`
3. Use the returned bearer token for the chat call.

Reason:

- `Z.ai` rolls the token on `/api/v1/auths/`.
- The token returned by `/api/v1/auths/` may differ from the one stored earlier.

### 3. Minimum headers

The smallest live-verified working header set is:

```http
Authorization: Bearer <fresh user token>
Content-Type: application/json
X-FE-Version: prod-fe-1.1.29
```

Additional observations:

- Header name casing is not important in HTTP, but examples in this doc use `X-FE-Version`.
- `X-Region: domestic` is currently optional for a basic successful call.
- `User-Agent`, `Origin`, `Cookie`, query `token=...`, and browser fingerprint query parameters were present in browser traffic, but they were not required in the minimal direct-call success case.
- `X-Signature` is not the current blocker. Removing it still reached business logic as long as `X-FE-Version` was present.

### 4. Minimum request body

A live-verified minimal working body is:

```json
{
  "stream": false,
  "model": "GLM-5.1",
  "messages": [
    {
      "role": "user",
      "content": "hello"
    }
  ]
}
```

Important notes:

- `model` must be a currently available model from `GET /api/models`.
- The server currently returns `text/event-stream` even when `stream` is `false`.
- That means callers should be prepared to parse SSE-style `data: ...` frames instead of assuming one plain JSON payload.

## Known-good direct example

### cURL

```bash
curl 'https://chat.z.ai/api/v2/chat/completions' \
  -X POST \
  -H 'Authorization: Bearer <FRESH_USER_TOKEN>' \
  -H 'Content-Type: application/json' \
  -H 'X-FE-Version: prod-fe-1.1.29' \
  --data-raw '{
    "stream": false,
    "model": "GLM-5.1",
    "messages": [
      {
        "role": "user",
        "content": "hello"
      }
    ]
  }'
```

### Python

```python
import requests

token = "<FRESH_USER_TOKEN>"
resp = requests.post(
    "https://chat.z.ai/api/v2/chat/completions",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-FE-Version": "prod-fe-1.1.29",
    },
    json={
        "stream": False,
        "model": "GLM-5.1",
        "messages": [
            {"role": "user", "content": "hello"}
        ],
    },
    timeout=90,
)

print(resp.status_code)
print(resp.headers.get("content-type"))
print(resp.text[:2000])
```

## What the browser currently sends

The browser request is much noisier than the true minimum. Current observed extras include:

- Query string:
  - `timestamp`
  - `requestId`
  - `user_id`
  - `version=0.0.1`
  - `platform=web`
  - `token`
  - many fingerprint fields such as screen size, timezone, current URL, browser name, OS name
- Headers:
  - `X-FE-Version: prod-fe-1.1.29`
  - `X-Region: domestic`
  - `X-Signature: ...`
  - browser cookies
- Body extras:
  - `signature_prompt`
  - `features`
  - `variables`
  - `chat_id`
  - `id`
  - `current_user_message_id`
  - `background_tasks`
  - `captcha_verify_param`

Direct replay validation showed:

- These extras are not all required for a basic answer.
- Blindly copying the browser request is not always better.

## Failure matrix

### 1. Wrong method

Symptom:

- `405 Method Not Allowed`

Owner:

- Request is not `POST`.

### 2. Missing frontend version header

Symptom:

- SSE error payload with business code `426`
- Message like:
  - `Your client version (unknown) is outdated`

Owner:

- Missing `X-FE-Version`

### 3. Replaying stale chat captcha

Symptom:

- SSE error payload:
  - `FRONTEND_CAPTCHA_REQUIRED`
  - `verify_code=F018` or `F019`

Owner:

- A stale browser-captured `captcha_verify_param` was replayed.

Current recommendation:

- Do not send `captcha_verify_param` in direct calls unless you can produce a fresh one for that request context.

## Integration guidance

### Recommended direct-call flow

1. Refresh the stored bearer token once through `/api/v1/auths/`.
2. Extract the returned token.
3. Call:
   - `POST /api/v2/chat/completions`
4. Send only:
   - `Authorization`
   - `Content-Type`
   - `X-FE-Version`
5. Start with the minimal JSON body.
6. Parse the response as SSE, even if `stream=false`.

### What not to do

- Do not use `GET` or `OPTIONS` for the chat endpoint.
- Do not assume browser query parameters are mandatory.
- Do not assume `X-Signature` is the current blocker.
- Do not blindly replay old `captcha_verify_param` values captured from the browser.
- Do not use `guest` tokens.

## Drift warning

The most drift-prone field currently is:

- `X-FE-Version: prod-fe-1.1.29`

If direct calls suddenly start returning the `426 outdated client` error again, the first refresh step should be:

1. Open the live web app.
2. Capture a fresh successful browser chat request.
3. Check whether `X-FE-Version` changed.

## Local verification artifacts

These local artifacts were produced during the 2026-05-12 re-verification pass:

- `.tmp/zai_chat_contract_probe.json`
- `.tmp/zai_chat_api_reverify.json`
- `.tmp/zai_chat_request_headers_probe.json`
- `.tmp/zai_chat_header_replay.json`
- `.tmp/zai_chat_query_cookie_matrix.json`
- `.tmp/zai_chat_minimal_matrix.json`
- `.tmp/zai_chat_header_minimalest.json`

They are debugging evidence, not stable API contracts by themselves. The contract in this document is the distilled result of those probes.
