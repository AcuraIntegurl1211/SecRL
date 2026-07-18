# Reproducing SecRL with DeepSeek Pro and Flash

This note records the environment, implementation changes, validation results,
and limitations of the current SecRL reproduction.

## Repository state

- Upstream: `https://github.com/microsoft/SecRL`
- Fork: `https://github.com/AcuraIntegurl1211/SecRL`
- Branch: `repro/deepseek-pro-flash`
- Base commit: `cb0fc6aedd7a8565e897ab3a8d82fc28d022e8bb`
- Implementation commit: `b21081f`

## Tested environment

- Apple Silicon Mac with VMware Fusion
- Ubuntu 24.04.4 ARM64 (`aarch64`)
- Conda environment: `excytin`
- Python 3.11.15
- Docker 29.6.2
- Docker Compose 5.3.1
- `pyautogen==0.2.35`
- `openai==2.46.0`
- `httpx==0.28.1`
- `socksio==1.0.0`

Only the `incident_5` Docker environment is currently installed.

## DeepSeek configuration

The API key is read from the environment:

```bash
export DEEPSEEK_API_KEY="your-api-key"
```

Never commit or publish the real key.

Configured models:

- `deepseek-v4-pro`
- `deepseek-v4-flash`

API base URL:

```text
https://api.deepseek.com
```

Thinking mode is disabled, request timeouts are configured, and automatic API
retries are disabled.

In the tested proxy environment, experiments were launched with:

```bash
env -u ALL_PROXY -u all_proxy <experiment-command>
```

This removes the SOCKS-wide proxy variables while preserving HTTP proxy
variables.

## Source changes

### `secgym/myconfig.py`

- Reads `DEEPSEEK_API_KEY` from the environment.
- Adds DeepSeek Pro and Flash configuration.
- Uses the DeepSeek OpenAI-compatible endpoint.
- Disables thinking and automatic retries.

### `experiments/run_exp.py`

- Enables final-summary submission for the baseline agent.
- Flushes the final environment trajectory before returning.

### `secgym/agents/agent_utils.py`

- Extracts the last complete `execute[...]` or `submit[...]` action.
- Handles duplicate actions without greedy concatenation.
- Strips whitespace and truncates execution text after the first semicolon.

### `secgym/agents/baseline_agent.py`

- Detects the final allowed step correctly.
- Requests an exact `submit[...]` final answer.
- Allows one correction call.
- Never executes SQL after the step limit.
- Uses a safe fallback submission when correction fails.

### `secgym/excytin_env.py`

- Adds idempotent `flush_logging()`.
- Saves the current trajectory exactly once.
- Avoids duplicate logs on repeated flushes.
- Clears the trajectory only after a successful save.

## Validation

Offline checks passed:

- Python syntax checks
- `git diff --check`
- Sensitive-data scan
- Parser unit tests
- Baseline final-flow tests
- Environment logging tests
- Function-level experiment integration test

Historical DeepSeek output scan:

- Assistant messages checked: 55
- Successfully parsed: 55
- Messages with multiple actions: 46
- Unparsed messages: 0

## Real API smoke tests

Two paid single-question tests were run against Incident 5.

### c431

- Submitted answer: `198.43.121.209`
- Reward: `1`
- Approximate DeepSeek Pro tokens: `38,034`
- The model submitted early.

### c531

- Nodes: `139-66`
- Submitted answer: `curl http://vectorsandarrows.com`
- Reward: `1`
- Approximate DeepSeek Pro tokens: `76,325`
- Final prompt count: `1`
- Correction prompt count: `0`

The temporary single-question loop restriction was not retained in source.

## Full Incident 5 experiment

A complete Incident 5 experiment was run with:

- Agent model: `deepseek-v4-flash`
- Evaluator model: `deepseek-v4-pro`
- Agent: `BaselineAgent`
- Maximum steps per question: `15`
- Trials per question: `1`
- Layer: `alert`
- Temperature: `0`
- Total questions: `98`

Validated results:

| Metric | Result |
|---|---:|
| Fully correct questions | 46 |
| Partially rewarded questions | 6 |
| Zero-reward questions | 46 |
| Full-success rate | 46/98 = 46.94% |
| Total reward | 48.4 |
| Average reward | 0.493878 |

Flash Agent token usage:

| Token category | Count |
|---|---:|
| Prompt tokens | 14,110,160 |
| Completion tokens | 186,291 |
| Total tokens | 14,296,451 |
| Prompt cache-hit tokens | 11,143,552 |
| Prompt cache-miss tokens | 2,966,608 |

These totals cover the Flash Agent usage recorded in the Agent logs. They do
not include the complete token usage of the Pro evaluator.

### Result integrity repair

The initial full run used cache seed `732`. An early implementation of
`--num_test` placed its stopping condition inside the trial loop. As a result,
the Agent log recorded node `134-147` twice and omitted node `161-55`.

The audit found:

- 98 Agent records but only 97 unique nodes
- 97 environment records
- Duplicate node: `134-147`
- Missing node: `161-55`

The missing question was run separately with cache seed `733` and
`--question_index 10`. It submitted `mimikatz.exe` and received reward `0`.

A repaired result set was generated offline by keeping the first `134-147`
record from `c732`, removing its duplicate, inserting the `161-55` Agent and
environment records from `c733`, restoring the original 98-question order, and
recalculating rewards and token totals. All original `c732` and `c733` files
were preserved unchanged.

The repaired result directory is:

```text
experiments/final_results/BaselineAgent_deepseek-v4-flash_c732_alert_level_t0_s15_trial1_repaired_with_c733_q10
```

It contains:

- `agent_incident_5.json`: 98 unique, ordered Agent records
- `env_incident_5.json`: 98 unique, ordered environment records
- `results.txt`: corrected aggregate metrics
- `repair_manifest.json`: source hashes, repair operations, and validation data

Because the repaired set combines 97 unique records from `c732` with one
record from `c733`, it must be described as a repaired mixed-run result rather
than as an unmodified single-seed run.

The experiment-selection fix was committed as:

```text
3989b27 Add scoped experiment selection options
```

It adds:

- `--attack` to select one incident
- `--num_test` to limit the number of questions
- `--question_index` to run one zero-based question index
- `_nN` and `_qN` result-directory suffixes

## Important notes

- AutoGen may display zero model cost when its local pricing table does not
  contain the selected model. External API requests may still be charged.
- The two earlier single-question runs are smoke tests rather than a benchmark.
- A complete Incident 5 result has now been produced and integrity-checked.
- The complete benchmark across all incidents has not been run.
- Only Incident 5 is provisioned.
- `ExcytinEnv.close()` stops the associated container and should not be called
  merely for inspection.
- The `flaml.automl is not available` warning was irrelevant to the tested
  baseline workflow.

## Suggested next work

- Add automated tests for parsing, final submission, log flushing, and scoped
  experiment selection.
- Add a reusable command for result-integrity auditing and repair.
- Record evaluator token usage separately from Agent usage.
- Provision more incident containers only when broader testing is required.
- Estimate API cost before any larger experiment.
