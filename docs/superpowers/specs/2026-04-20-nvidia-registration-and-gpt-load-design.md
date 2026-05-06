# NVIDIA Registration And GPT-Load Integration Design

## Background

The current repository has no `nvidia` platform plugin, but the requested target flow is now concrete:

1. Register a new NVIDIA account from the email-based entry at:
   - `https://build.nvidia.com/?modal=signin`
2. Reuse the repository's existing mailbox creation and OTP retrieval abstractions where the NVIDIA flow requires email verification.
3. After the account is usable, generate a provider-native NVIDIA API key.
4. Import that API key into an existing `gpt-load` group named `nvidia`.

This design is intentionally investigation-first. It captures what was proven from the live page and current repository, and it separates proven facts from implementation-time follow-ups.

## Goals

- Add a new platform plugin named `nvidia`.
- Build the registration flow around the real live page behavior, not guesses.
- Reuse the repository's mailbox abstraction and task-log conventions.
- Persist the generated NVIDIA API key as the account's primary token.
- Auto-import the generated API key into a configured `gpt-load` group.
- Produce a bounded implementation plan that can be executed without reopening basic page-contract questions.

## Non-Goals

- No speculative support for OAuth-style NVIDIA tokens.
- No automatic creation of `gpt-load` groups in the first version.
- No secret values committed into the repository.
- No assumption that NVIDIA registration can be completed purely by protocol requests.

## Live Findings

All findings below were re-checked on April 20, 2026 against the live pages.

### 1. `build.nvidia.com` starts with an in-page email-first modal

Initial entry:

- `https://build.nvidia.com/?modal=signin`

Observed facts:

- The page opens a dark modal titled:
  - `Sign In to Get Started with NVIDIA AI`
- The first step contains:
  - email input `name="email"`
  - `Next` button
- A cookie banner can block interaction and must be dismissed first.
- After the cookie banner is cleared, the script may still need to force-open the login modal again via the page `Login` button.

Implication:

- The plugin needs a deterministic "cookie banner handling + reopen modal if needed" prelude before any email submission.

### 2. Email submission redirects to the NVIDIA account system

After entering an unused email and clicking `Next`, the browser was redirected through these observed endpoints:

- `https://api.ngc.nvidia.com/login?...`
- `https://login.nvidia.com/authorize?...`
- `https://accounts.nvgs.nvidia.cn/api/1/oauth/authorize?...`
- `https://login.nvgs.nvidia.cn/v1/login?...`
- final page for a new email:
  - `https://login.nvgs.nvidia.cn/v1/create-account?...`

Observed browser-side checks during this transition included:

- `GET /api/1/frontend/oauth/email/check`
- `GET /api/1/validator/register`
- `GET /api/1/password/validation/policy`

Implication:

- The repo should model NVIDIA as a browser-first registration platform.
- A protocol-only implementation is not the safe first choice.

### 3. New-email flow currently lands on `Create Your Account`, not OTP-first

For an unused email, the live page did **not** immediately ask for an email OTP.

It landed on a create-account page with:

- `#emailAddress`
- `#registration_password`
- `#registration_passwordConfirm`
- `#data_general_agreement-input`
- `#terms_and_conditions-input`
- `#stay_signin_checkbox-input`
- `Create Account`

Observed page title:

- `Sign in with NVIDIA`

Observed visible heading:

- `Create Your Account`

Implication:

- The first registration milestone is account creation, not mailbox OTP entry.
- Mailbox handling is still required later if NVIDIA sends verification mail after submission, but the initial branch is password-first.

### 4. hCaptcha is part of the create-account page

On the create-account page, the live browser rendered an `I am human` widget.

Additional supporting evidence from the live login bundle:

- `https://login.nvgs.nvidia.cn/assets/js/environment.js`
- contains:
  - `hCaptchaSiteKey`
  - `passiveHCaptchaSiteKey`
  - `hcaptchaScriptURL`

Implication:

- Current repo captcha abstractions are insufficient as-is.
- The repository currently supports:
  - Turnstile via `YesCaptcha.solve_turnstile`
  - local Turnstile solver
  - manual token entry
- It does **not** currently expose a first-class `solve_hcaptcha(...)` API.

This is the main implementation gap before NVIDIA account creation can be fully automated.

### 5. Create-account submit contract was captured without creating an account

The real submit request was captured by filling the form, then intercepting and aborting only the final submit request.

Observed submit endpoint:

- `POST https://accounts.nvgs.nvidia.cn/api/1/frontend/oauth/user/register`

Observed request headers of interest:

- `Authorization: Bearer <page key token>`
- `Content-Type: application/json`
- `Origin: https://login.nvgs.nvidia.cn`
- `Referer: https://login.nvgs.nvidia.cn/`
- cookie carrying at least:
  - `MainAuth_1`

Observed JSON payload shape:

```json
{
  "email": "probe_nvidia_flow@example.com",
  "password": "Nv!juH0z032fcyD",
  "rememberLogin": true,
  "autoLogin": false,
  "regionalPolicy": {
    "chinaPipl": [
      {
        "type": "dataGeneralAgreement",
        "level": "Full"
      }
    ]
  },
  "deviceId": "WNFGcZ0pVL66VWx4"
}
```

Observed page behavior after intercept abort:

- the page reported service-unavailable style errors because the final request was intentionally blocked
- this confirmed the request contract without creating an external account

Implication:

- A protocol fallback could theoretically target this endpoint, but only if the browser-prepared bearer token, cookies, device ID, hCaptcha state, and surrounding anti-abuse expectations are reproduced correctly.
- For the first implementation, browser automation remains the correct owner.

### 6. Current repo patterns to reuse

Best matching existing patterns:

- `platforms/grok/plugin.py`
  - browser-first plugin structure
  - mailbox callback pattern
  - task-log usage
  - explicit captcha dependency wiring
- `platforms/tavily/core.py`
  - staged "step1/step2/..." flow structure
  - provider-native API key as final output
- `core/base_mailbox.py`
  - mailbox acquisition and OTP polling abstractions
- `services/external_sync.py`
  - post-registration automatic external import hook

## API Key Generation Findings

### Proven

The target NVIDIA inference base URL is already known and matches official docs:

- `https://integrate.api.nvidia.com`

This also matches current official NVIDIA API docs for hosted LLM APIs.

Additional facts proven from the live `build.nvidia.com` frontend bundles:

- the active organization is sourced from:
  - `GET /user-context`
- the active organization can be changed inside the app via:
  - `POST /user-context`
- the key-generation mutation in the public bundle targets:
  - `POST /v3/orgs/{orgName}/keys/type/AI_PLAYGROUNDS_KEY`
- the frontend default payload is:
  - `name: ""`
  - `type: "AI_PLAYGROUNDS_KEY"`
  - `expiryDate: now + 6 months`
  - `policies: [{product: "nv-cloud-functions", scopes: ["invoke_function"], resources: [{id: "*", type: "account-functions"}]}]`
- the frontend expects the response shape to include:
  - `apiKey.value`
  - `apiKey.keyId`
  - `apiKey.name`
  - `apiKey.expiryDate`
- the frontend UI explicitly treats the key as one-time visible output:
  - `This is the only time your key will be displayed.`

Implications:

- `orgName` does not need to be guessed or scraped from arbitrary DOM text; it should be read from the authenticated user context after login.
- if the plugin wants a 100-year key instead of the frontend default, it must override `expiryDate` explicitly.
- the repository should persist the returned API key value, while preserving `keyId` and `expiryDate` in `extra`.

### User-provided post-login key-creation contract

The requested API key generation method is:

```javascript
fetch("https://api.ngc.nvidia.com/v3/orgs/{orgName}/keys/type/AI_PLAYGROUNDS_KEY", {
  "headers": {
    "accept": "*/*",
    "content-type": "application/json"
  },
  "referrer": "https://build.nvidia.com/",
  "body": "{\"expiryDate\":\"2126-04-08T07:00:00Z\",\"name\":\"dev\",\"type\":\"AI_PLAYGROUNDS_KEY\",\"policies\":[{\"product\":\"nv-cloud-functions\",\"scopes\":[\"invoke_function\"],\"resources\":[{\"id\":\"*\",\"type\":\"account-functions\"}]}]}",
  "method": "POST",
  "mode": "cors",
  "credentials": "include"
})
```

Response contract from the user:

- response JSON field `value` is the API key

This user-provided fetch pattern is consistent with the public frontend bundle:

- both use the same `orgName`
- both use `AI_PLAYGROUNDS_KEY`
- both use the same `nv-cloud-functions / invoke_function / account-functions:*` policy family

Recommendation for first implementation:

- keep key generation inside the authenticated browser context
- prefer a Playwright `page.evaluate(fetch(..., { credentials: \"include\" }))` call against the user-provided absolute NGC URL
- avoid rebuilding the session out-of-browser until the browser-first path is proven end to end

### Not yet live-verified in this investigation

The following post-login details were **not** proven live in this pass because no external account was created:

- whether there is any mandatory email verification before key creation
- whether key creation works immediately after account creation or only after another approval / onboarding step
- whether the direct absolute NGC fetch needs anything beyond browser session cookies in the real post-login state

These remain implementation-time checkpoints, not open-ended unknowns.

### Additional gating evidence from the build frontend

The live `Generate API Key` component also consumes:

- `GET /v2/users/me`

and checks current-user state including:

- `user.blocked`
- `user.verified`

before deciding whether to proceed directly or open an additional modal.

Implication:

- account creation success alone is not enough to assume immediate key generation
- the implementation should verify the authenticated session has reached a state that the NVIDIA frontend considers eligible for key creation

## Current GPT-Load Boundary

The target `gpt-load` instance already has an existing group named:

- `nvidia`

The instance is an OpenAI-compatible group targeting:

- upstream: `https://integrate.api.nvidia.com`

Implications:

- first version should import into the existing `nvidia` group
- first version does **not** need group creation logic
- no repository file should store the actual management key

## Proposed Repository Design

### 1. New platform package

Add:

- `platforms/nvidia/plugin.py`
- `platforms/nvidia/core.py`

Recommended plugin shape:

- `name = "nvidia"`
- `display_name = "NVIDIA"`
- `version = "1.0.0"`
- `supported_executors = ["headless", "headed"]`

Rationale:

- the live flow is browser-first
- hCaptcha and anti-abuse surfaces make `protocol` a poor first implementation target

### 2. Account persistence contract

On success, store:

- `Account.platform = "nvidia"`
- `Account.email`
- `Account.password`
- `Account.token = <nvidia_api_key>`
- `Account.extra["api_key"] = <nvidia_api_key>`
- `Account.extra["base_url"] = "https://integrate.api.nvidia.com"`

Optional metadata to preserve:

- `Account.extra["org_name"]`
- `Account.extra["login_domain"]`
- `Account.extra["key_expiry"]`
- `Account.extra["key_id"]`
- `Account.extra["device_id"]`

This mirrors the repo convention that platform-specific state belongs in `extra`.

### 3. Browser flow skeleton

Recommended state machine in `platforms/nvidia/core.py`:

1. Open `https://build.nvidia.com/?modal=signin`
2. Dismiss cookie banner if present
3. Reopen login modal if the modal disappeared after cookie interaction
4. Fill email and click visible `Next`
5. Wait for redirect into `login.nvgs.nvidia.cn`
6. Branch:
   - `v1/create-account` -> new account branch
   - login / identifier / challenge pages -> existing-account or verification branch
7. In create-account branch:
   - fill password
   - fill password confirm
   - check required agreements
   - solve hCaptcha
   - submit `Create Account`
8. After submission:
   - detect whether NVIDIA requests mailbox verification / OTP
   - if yes, use repo mailbox abstraction to fetch code or verification link
   - continue until a usable logged-in session is reached
9. Once authenticated:
   - read `orgName` from authenticated user context
   - create key with explicit `name` and explicit `expiryDate`
   - extract response `apiKey.value` or user-observed `value`
   - return provider-native API key

### 4. Mailbox reuse

Even though the first visible branch is password-first, the plugin should still accept the standard mailbox dependency used by other platforms.

Reuse exactly:

- `mailbox.get_email()`
- `mailbox.get_current_ids(...)`
- `mailbox.wait_for_code(...)`

Expected NVIDIA follow-up mail patterns still to classify during implementation:

- numeric OTP
- verification link
- security confirmation mail

The NVIDIA core module should therefore support both:

- `wait_for_code(...)`
- link extraction from email body if the site sends verify-link mail instead of a bare code

### 5. Captcha abstraction change

Current repo limitation:

- `core/base_captcha.py` only models Turnstile and image captcha

Required extension:

- add `solve_hcaptcha(page_url: str, site_key: str, **kwargs) -> str`

Recommended first implementation:

- `YesCaptcha.solve_hcaptcha(...)`
  - use official `HCaptchaTaskProxyless`
- `ManualCaptcha.solve_hcaptcha(...)`
  - user pastes token
- `LocalSolverCaptcha.solve_hcaptcha(...)`
  - explicitly unsupported in first version unless a separate hCaptcha solver is added

This avoids forcing NVIDIA into the wrong Turnstile abstraction.

### 6. GPT-Load integration

Add a new generic external uploader service, not a `cliproxyapi` sync clone.

Recommended new module:

- `services/gpt_load_sync.py`

Suggested responsibilities:

- list groups via `GET /api/groups`
- resolve group by configured `gpt_load_group_name`
- import one or more keys via `POST /api/keys/add-multiple`
- normalize success / duplicate / failure messages

Config keys to add:

- `gpt_load_enabled`
- `gpt_load_url`
- `gpt_load_admin_key`
- `gpt_load_group_name`

Then extend:

- `services/external_sync.py`

with a platform whitelist:

- first version: `platform == "nvidia"`

Upload source:

- `account.extra["api_key"]` if present
- otherwise `account.token`

### 7. Frontend and API surfaces to update

Add `nvidia` to all platform lists that are currently hard-coded:

- `frontend/src/pages/RegisterTaskPage.tsx`
- `frontend/src/lib/platformExecutorOptions.ts`
- `frontend/src/pages/RunningTasks.tsx`
- `frontend/src/pages/TaskHistory.tsx`

Review whether any additional UI filters need updating:

- `frontend/src/App.tsx`
- `api/platforms.py`

No dedicated account action is required for the first pass.

## Testing Plan

### Unit / mocked tests

1. New NVIDIA plugin registration class is discoverable through `@register`.
2. NVIDIA core correctly handles:
   - cookie dismissal branch
   - email step redirect to create-account
   - create-account field fill
   - required agreement toggles
3. Captured key-creation response parsing returns `value`.
4. Key-generation helper handles both observed response shapes:
   - top-level `value`
   - nested `apiKey.value`
5. `gpt_load_sync`:
   - finds group by name
   - imports key
   - treats duplicate import as non-fatal success
6. `external_sync.sync_account(...)` uploads NVIDIA API keys when `gpt_load_enabled=1`.

### Focused integration probes

During implementation, run real probes for:

1. hCaptcha sitekey extraction
2. post-create branch:
   - direct success
   - mail verification
   - challenge / review / failure
3. `GET /user-context` and `GET /v2/users/me` after first successful login
4. key creation response shape and expiry handling
5. real import into the existing `gpt-load:nvidia` group

## Open Questions

These are the only material points still needing live confirmation in the implementation phase:

1. Does NVIDIA always require a verification mail after account creation, or only sometimes?
2. Is the verification artifact a numeric code, a link, or both?
3. Does the authenticated browser session need any extra post-login warmup before the direct NGC fetch succeeds?
4. Is `AI_PLAYGROUNDS_KEY` immediately usable against `https://integrate.api.nvidia.com` with no extra onboarding step?

## Recommended Execution Order

1. Extend captcha abstraction for hCaptcha.
2. Implement `platforms/nvidia/core.py` browser-first flow up to authenticated session.
3. Prove post-create verification branch with a real mailbox.
4. Implement NVIDIA API key creation helper.
5. Add `services/gpt_load_sync.py`.
6. Extend `services/external_sync.py` for NVIDIA.
7. Wire frontend platform lists.
8. Add focused tests.

## References

- NVIDIA build entry:
  - `https://build.nvidia.com/?modal=signin`
- NVIDIA hosted inference base URL:
  - `https://integrate.api.nvidia.com`
- NVIDIA API quickstart:
  - `https://docs.api.nvidia.com/nim/docs/api-quickstart`
- NVIDIA LLM API reference:
  - `https://docs.api.nvidia.com/nim/reference/llm-apis`
- YesCaptcha hCaptcha docs:
  - `https://yescaptcha.atlassian.net/wiki/spaces/YESCAPTCHA/pages/7929858/HCaptchaTaskProxyless%2BHCaptcha`
