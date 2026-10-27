# Paper preview view use case (2026-10-27)

The `/paper-preview-latest` route now delegates to `trading/research/paper_preview_view.py`. The use case receives an explicit data root, reads the newest saved preview, and preserves missing/invalid/saved states and all read-only flags. It does not advance an account, access a broker, or read process environment while projecting the result.

Focused direct-view plus API/operator/architecture tests passed (`58 passed`); `py_compile` passed. This is a structural refactor with unchanged paper semantics.
