# E06 finite campaign evidence

- [Frozen inputs](../../../services/backend/tests/fixtures/e06/cases.json) and [independent oracle](../../../services/backend/tests/fixtures/e06/oracle.json) precede live calls; model context does not receive oracle labels.
- [Offline controls](offline-controls.json): 8 PostgreSQL/loopback checks pass, including global budgets, partial-usage stop, post-send smoke failure, ownership and safe negatives. They establish controlled integration, not model quality.
- [Initial live attempt](live-initial.json): one HTTP model request to the exact authorized gateway/configuration failed with HTTP 400. The timestamp-matched gateway artifact reports that its Kiro integration version is no longer supported. Usage/cost are unavailable. The driver stopped before further primary cases or the secondary probe.

The failed initial cohort is retained. Ten-case quality thresholds, healthy/injection negatives, second-configuration compatibility and qualified operational smoke remain unmet. A successful model catalog read is not a chat/schema/quality pass. No qualification has been installed and E06 is not accepted.

The owned evaluation analysis is failed, its Job completed, copied private context discarded, and operational API result absent. The remaining allowance is 14 model requests from the original total of 15; further execution requires a working supported integration and a recorded remaining-budget cohort, without erasing this failure or changing the oracle.
