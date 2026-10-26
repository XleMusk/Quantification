# F01 decision-cutoff contract v1

- Status: VERIFIED_LOCAL_SEMANTICS_BLOCKED_EXTERNAL_DATA
- vent_window remains the disclosure window; prior profit and event-pre assets must finish before its start.
- decision_cutoff is a separate exact decision clock; current profit may be disclosed during the event window when available by the cutoff.
- A current fact after the cutoff and a same-day asset without pre-event proof both remain PRE_EVENT_UNPROVEN.
- The change is pure calculation/serialization. It does not fetch data, create targets, route orders or enable execution.

Receipt: outputs/pipeline_receipts/20261026_f01_decision_cutoff_contract_v1.json.
