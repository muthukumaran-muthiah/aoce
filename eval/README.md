# Evaluation

| File | What it is |
|---|---|
| `failure_catalogue.yaml` | Ground truth for every flagd flag in the demo: root cause, mechanism, expected alerts and symptoms, decisive evidence, red herrings, grading facts (AOCE-22) |
| `benchmark.yaml` | The 10 scenarios the agent is measured on: which flag and variant to set, and how the agent gets paged (AOCE-42) |
| `run_benchmark.py` | Runs the scenarios against a live stack and records what the agent concluded next to the ground truth |
| `results/` | Run output, one folder per run (gitignored) |

## Running the benchmark

With `tilt up` running and `AOCE_API_KEY` and `AOCE_WEBHOOK_TOKEN` in `.env` (or exported):

```sh
pip install -e ".[dev]"
python eval/run_benchmark.py --list       # scenarios, how each pages, expected root cause
python eval/run_benchmark.py --dry-run    # checks flagd, Prometheus and the agent are reachable; changes nothing
python eval/run_benchmark.py              # all 10, one after another
python eval/run_benchmark.py --only paymentFailure,paymentUnreachable
```

For each scenario the runner:

1. waits until no Prometheus alert is pending or firing and no investigation is running (up to 15 min),
2. switches the flag to the scenario's variant through the flagd UI API (`/feature/api/write`), setting
   `targeting` too where the scenario gives one,
3. waits for the first incident at `/incidents` (up to `page_timeout_s`, 10 min), or for a synthetic
   scenario posts the alert itself after `after_s`,
4. keeps collecting incidents for `settle_s` (2 min) after the first page, then until each investigation
   has finished (up to 15 min),
5. restores the flags as it found them and waits for the stack to recover before the next scenario.

Flags are restored at the end of the run even if it is interrupted. A full run takes roughly 2 to 3 hours.

Each `results/<run id>/<flag>.json` holds the incidents opened (alert, status, time to page, steps,
tokens, the agent's report) next to the catalogue's `culprit`, `root_cause`, `grading`,
`red_herrings` and `giveaway_logs`, plus `expected_alerts_missing` and any `warnings` (stack not
quiet, no page, investigations still running). `summary.json` has one line per scenario. Scoring the
reports against `grading.must_identify` is AOCE-43.

## Decisions

- **Two scenarios use a synthetic page.** `emailMemoryLeak` and `kafkaQueueProblems` page nothing with the
  current rules (catalogue `gap`). The runner posts the alert the missing rule would send
  (`ContainerRestarts` on email, `KafkaConsumerLagHigh` on fraud-detection), worded as a real rule would
  be, without naming the cause. Switch each to `kind: alert` once a real rule exists.
- **Not being paged is a result, not an error.** `adHighCpu` and `loadGeneratorFloodHomepage` are only
  "likely" to page. If nothing arrives in time the scenario is recorded with `paged: false`.
- **Giveaway logs stay visible.** Six scenarios log a line that names the flag (`giveaway_logs` in the
  catalogue). Hiding them would need a filter in `search_logs`; for now each result carries the
  catalogue's `giveaway_logs` so scoring can report those scenarios separately.
- **Scenarios run one at a time**, with a quiet stack in between, so each incident is attributed to one fault.

The expected symptoms are still `verified: false` in the catalogue; the first live run will confirm or
correct the timings above (`after_s`, `page_timeout_s`).
