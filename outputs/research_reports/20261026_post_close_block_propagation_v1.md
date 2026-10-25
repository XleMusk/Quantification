# Post-close D0/D1 block propagation v1

The post-close coordinator now propagates a non-completed D0/D1 result. A blocked D0/D1 run becomes `BLOCKED_D0_D1_INPUT`, retains the reason and `snapshot_coverage`, and is never written as `COMPLETED_RESEARCH_ONLY_POST_CLOSE_D0_D1`. Successful runs expose the same coverage at the coordinator level.

Validation: post-close and next-close focused tests — `24 passed`.
