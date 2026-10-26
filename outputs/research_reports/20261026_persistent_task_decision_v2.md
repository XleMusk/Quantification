# Persistent task decision receipt v1

- status: `VERIFIED_LOCAL`
- created runs: `4`; active runs: `4`
- rejection codes: `CONCURRENCY_LIMIT_REACHED, INVALID_RUN_REQUEST, REQUEST_KEY_CONFLICT`
- concurrency rejection persisted: `True`
- idempotency conflict persisted: `True`
- invalid request persisted: `True`
- no fifth run created: `True`

This is an isolated local paper-store exercise. It records rejection reasons and does not schedule a worker or contact a broker/THS.
