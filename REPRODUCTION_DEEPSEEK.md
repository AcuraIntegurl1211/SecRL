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

## Important notes

- AutoGen may display zero model cost when its local pricing table does not
  contain the selected model. External API requests may still be charged.
- These two successful questions are smoke tests, not a complete benchmark.
- The full benchmark has not been run.
- Only Incident 5 is provisioned.
- `ExcytinEnv.close()` stops the associated container and should not be called
  merely for inspection.
- The `flaml.automl is not available` warning was irrelevant to the tested
  baseline workflow.

## Suggested next work

- Add a supported single-question command-line option.
- Add automated tests for parsing, final submission, and log flushing.
- Provision more incident containers only when broader testing is required.
- Estimate API cost before any larger experiment.
