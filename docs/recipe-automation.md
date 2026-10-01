# Automatic recipe integration

In the Cookbook, review a built-in, community, pasted, or local recipe and choose
**Import & prepare**. TwinSpark saves that exact reviewed snapshot, pins model
commits and container images, validates memory and prerequisites, pulls missing
images, and downloads/verifies/synchronizes weights on the required nodes. Split
profiles pin both models together and stage each model only where it runs. TP2
and PP2 prepare one model across both nodes.

The Jobs page shows progress and cancellation. **Switch to prepared revision**
is a separate action after completion. Preparation does not drain requests,
stop containers, change routes, reboot nodes, or mark a revision known-good.
Dry-run results are simulations; on-site hardware testing is still required.

**Import only** remains available for customization. A draft profile's
**Pin & prepare** button runs the same automatic pipeline with its saved settings.
The **Pin** action allows explicit model/image overrides. Container builds and
patch installation remain prerequisites: community build scripts are not run
automatically. Failed jobs identify missing dependencies and retain the profile.

After fixing a missing patch, connectivity, or disk-space issue, use **Retry
preparation**. A retry creates a new job linked to the original. Once pinning has
succeeded, retries reuse the exact revision and repeat checks against current
nodes/cache contents. They do not follow a newly changed model branch or image
tag. If pinning failed, it is attempted again. If the profile was edited, reload
it and use **Pin & prepare** instead; retries cannot overwrite those edits.
Interrupted jobs are recorded as failed on controller restart and can be retried.

## Management API

The authenticated API supports the same workflow without a browser:

```http
POST /api/v1/cookbook/integrate
Content-Type: application/json
x-api-key: <management key>

{
  "draft": { "...": "the complete ProfileDraft returned by recipe preview" },
  "existing": false,
  "request_id": "experiment-unique-request-001",
  "pins": {
    "model_ref": "main",
    "image": "ghcr.io/owner/image:tag",
    "secondary": { "model_ref": "release", "local_image": "coder-local" }
  }
}
```

`pins` is optional; recipe image hints and existing pins supply defaults.
`secondary` overrides are only valid for a split profile. Set `existing: true`
to prepare a saved profile: the supplied draft must exactly match its current
working draft. The response is HTTP 202 with a job ID, available at
`GET /api/v1/jobs/{job_id}`. Cancel with `POST /api/v1/jobs/{job_id}/cancel`.

Choose a unique `request_id` for each intended operation. Resending the same
request returns its original job, including after completion or restart.
Reusing an ID with different settings is rejected. A busy cluster rejects new
operations before creating a profile. The recipe URL is never re-fetched during
integration; the reviewed draft is the input.

Retry a failed job with `POST /api/v1/cookbook/integration/{job_id}/retry` and
`{"request_id":"experiment-retry-001"}`. Use the same retry ID for an uncertain
response. After success, activate `job.profile_revision` explicitly through the
profile activation endpoint.
