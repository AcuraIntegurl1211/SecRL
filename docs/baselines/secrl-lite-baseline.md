# SecRL Lite Research Baseline

Status: **FROZEN — canonical source baseline approved**

This record contains the evidence and approved decision for Task 1, “Freeze
The Research Baseline.” No Ubuntu file was changed, no remote dirty change
was copied into this worktree, and this record adds provenance only.

## Canonical decision checkpoint

- Canonical commit: `9c36eae065978ac2b52ee772814b345b66702253`.
- Decision: Candidate A approved manually on 2026-08-11. Freeze the clean
  current local source commit and exclude every Ubuntu dirty change.
- Deferred alternative: a separate future change may split, regression-test,
  and commit the conditional truncation candidates identified below. That
  alternative is not part of this baseline.
- Local comparison commit required by the plan:
  `93daa706d5c093343837381444e1bf31d45bc9cf`.
- Ubuntu comparison commit required by the plan:
  `d0f07a8b327f96b41807de5e95d710ca3462300f`.

The deferred alternative is not performed in Task 1 because it would modify
existing production experiment semantics. The approved canonical commit is a
literal revision obtained from `git rev-parse HEAD` while the source
worktree was clean.

## 1. Repository revisions and dirty state

### Local linked worktree

Commands:

```console
$ pwd
/Users/acuraintegurl/.codex/worktrees/4783/secrl-sql
$ git rev-parse --git-dir
/Users/acuraintegurl/Documents/111/secrl-sql/.git/worktrees/secrl-sql
$ git rev-parse --git-common-dir
/Users/acuraintegurl/Documents/111/secrl-sql/.git
$ git branch --show-current

$ git rev-parse HEAD
9c36eae065978ac2b52ee772814b345b66702253
$ git status --short

```

The empty branch and status output means the Codex-managed linked worktree is
detached and clean. The current commit is one documentation-only child of the
planned local comparison commit:

```console
$ git show -s --format='%H%n%P%n%ad%n%s' --date=iso-strict HEAD
9c36eae065978ac2b52ee772814b345b66702253
93daa706d5c093343837381444e1bf31d45bc9cf
2026-08-11T12:41:41+08:00
docs: approve SecRL Lite platform design and plan
$ git show -s --format='%H%n%P%n%ad%n%s' --date=iso-strict 93daa706d5c093343837381444e1bf31d45bc9cf
93daa706d5c093343837381444e1bf31d45bc9cf
e20495f64b700e1e9d2d178fa76cd9390f5ad993
2026-08-02T12:41:57+08:00
Validate large staged CSV fields safely
```

### Ubuntu source checkout

Commands were executed read-only over SSH:

```console
$ git -C /home/acuraintegurl/Desktop/SecRL-git rev-parse HEAD
d0f07a8b327f96b41807de5e95d710ca3462300f
$ git -C /home/acuraintegurl/Desktop/SecRL-git status --short
 M experiments/run_exp.py
 M secgym/agents/agent_utils.py
 M secgym/agents/baseline_agent.py
 M secgym/evaluator.py
 M secgym/excytin_env.py
 M secgym/myconfig.py
?? experiments/failure_analysis_outputs_no_trunc/
?? experiments/full_incident_134_official_flash.supervisor.pid
?? experiments/full_incident_166_official_flash.supervisor.pid
?? experiments/full_incident_166_official_flash.supervisor_run2.pid
?? experiments/full_incident_322_official_flash.supervisor.pid
?? experiments/full_incident_34_Qwen3-30B.pid
?? experiments/full_incident_34_Qwen3-30B.supervisor_run2.pid
?? experiments/full_incident_34_Qwen3-30B.supervisor_run3.pid
?? experiments/full_incident_34_official_flash.supervisor.pid
?? experiments/full_incident_34_yuk_flash.supervisor.pid
?? experiments/full_incident_34_yuk_flash.supervisor_run2.pid
?? experiments/full_incident_38_official_flash.supervisor.pid
?? experiments/full_incident_38_official_flash.supervisor_run2.pid
?? experiments/full_incident_39_official_flash.supervisor.pid
?? experiments/full_incident_55_official_flash.supervisor.pid
?? experiments/run_exp_no_trunc.py
?? experiments/run_official_flash_incident134_supervisor.sh
?? experiments/run_official_flash_incident166_supervisor.sh
?? experiments/run_official_flash_incident166_supervisor_run2.sh
?? experiments/run_official_flash_incident322_supervisor.sh
?? experiments/run_official_flash_incident34_supervisor.sh
?? experiments/run_official_flash_incident38_supervisor.sh
?? experiments/run_official_flash_incident38_supervisor_run2.sh
?? experiments/run_official_flash_incident39_supervisor.sh
?? experiments/run_official_flash_incident55_supervisor.sh
?? experiments/run_qwen_incident34_supervisor.sh
?? experiments/run_qwen_incident34_supervisor_run2.sh
?? experiments/run_qwen_incident34_supervisor_run3.sh
?? experiments/run_yuk_flash_incident34_supervisor.sh
?? experiments/run_yuk_flash_incident34_supervisor_run2.sh
?? final_results_no_trunc/
?? full_incident_34_Qwen3-30B.pid
?? tests/test_assistant_response_extraction.py
?? tests/test_qwen_output_and_retry.py
?? tests/test_r1_response_boundary.py
?? tests/test_response_model_fallback.py
?? tests/test_run_exp_resume_count.py
?? tests/test_yuk_flash_config.py
```

After all remote inspection and tests, the status text remained identical.
Its post-test SHA-256 and the tracked binary patch SHA-256 were:

```console
$ git status --short | shasum -a 256
a9f5ef3e31a249829a23ddbbbd68217fe7729d1ad39cb9da8ec093afea8c8ded  -
$ git diff --binary | shasum -a 256
06e11e542dca34566f10f48c0f94b04c43cfdf21c4548155769c8a3c40b54b4a  -
```

### Committed local/Ubuntu relationship

`d0f07a8...` is the merge base and an ancestor of the local commit. Local is
33 commits ahead. There are no Ubuntu-only committed files.

```console
$ git merge-base HEAD d0f07a8b327f96b41807de5e95d710ca3462300f
d0f07a8b327f96b41807de5e95d710ca3462300f
$ git rev-list --count d0f07a8b327f96b41807de5e95d710ca3462300f..HEAD
33
$ git diff --name-status d0f07a8b327f96b41807de5e95d710ca3462300f..HEAD
A docs/superpowers/plans/2026-08-11-secrl-lite-platform.md
A docs/superpowers/specs/2026-08-10-secrl-web-benchmark-platform-design.md
A docs/superpowers/specs/2026-08-11-secrl-web-benchmark-platform-lite-design.md
A experiments/failure_analysis/analyze_sql_retrieval.py
A experiments/failure_analysis/retrieval_extract.py
A experiments/failure_analysis/retrieval_models.py
A experiments/failure_analysis/retrieval_reporting.py
A experiments/failure_analysis/retrieval_review.py
A experiments/failure_analysis/retrieval_rules.py
A experiments/failure_analysis/sql_retrieval_taxonomy_v1.json
A tests/failure_analysis/test_retrieval_extract.py
A tests/failure_analysis/test_retrieval_models.py
A tests/failure_analysis/test_retrieval_reporting.py
A tests/failure_analysis/test_retrieval_review.py
A tests/failure_analysis/test_retrieval_rules.py
A tests/failure_analysis/test_sql_retrieval_cli.py
```

The committed comparison therefore adds failure-analysis SQL-retrieval code,
its tests, and design/plan documentation; it does not change committed
`secgym` Agent, Evaluator, Environment, or experiment-runner code.

## 2. Ubuntu dirty semantic differences

The tracked dirty files have the following line deltas and working-tree
SHA-256 values:

| Path | `git diff --numstat` | Clean/local SHA-256 | Ubuntu working SHA-256 | Classification |
| --- | ---: | --- | --- | --- |
| `experiments/run_exp.py` | `7 2` | `6ab981d14d9b0c9a7b6caefc66fb02cded45ebda72fb8dd8c77e08fe1192c351` | `65bbf72ebcc1f4d79d9f7bb51ba1ed23719b3d6b2d84b7d2ee085a77329e12de` | Excluded from canonical A; truncation hunk remains a conditional future candidate and resume hunk remains separate |
| `secgym/excytin_env.py` | `41 7` | `eb098a4666a81660133066daaac34f916eae36d0c311b139634846ce1a9a293a` | `737c379744bb19005c040d2ac39224a2ee9915cd2a21db9553ec7097b61c132f` | Excluded from canonical A; conditional future candidate only after focused regression tests |
| `secgym/agents/agent_utils.py` | `183 37` | `bc948ae7bdc4172826cd611882d1060bcd3dad42255f7e7df266f5d520e5e37f` | `9c39f9dde6b2b38958443f2c8993ddf7ed5330b8e252f03e94abde5079adf7ae` | Exclude from no-trunc baseline; timeout/retry/model behavior |
| `secgym/agents/baseline_agent.py` | `131 16` | `539ce0d0175b848c6c09683be0ff1bdeeb6380f44354e8439c51a6e01fae1717` | `6a7ebaf4a7d3ce6d225585403e857668f2e9a22d664bd93bd7c9017d4b06bd01` | Exclude from no-trunc baseline; response parsing/prompt/usage behavior |
| `secgym/evaluator.py` | `107 10` | `b146af231c0b63d7252c5b7852c62f0ba59ab40980b65be5d003cbc2f08d05e2` | `e66d994ea2cf896351555401fea8b436f3cc91e28d6d1fa77902a6f5c483d623` | Exclude from no-trunc baseline; retry/timeout/JSON parsing behavior |
| `secgym/myconfig.py` | `38 5` | `d98a007a8d4504ff4b813e574e6f062c619e1178eebc632052de02dd6f814db3` | `fbffb5024de57b84aed0a3f43e5fa136fbfdfb096886eda6f9f9dc9341b5433a` | Exclude; local model/configuration and API-key risk; content intentionally not printed |

The two conditional candidates are not safe to accept as whole files:

1. `run_exp.py` adds `--max_entry_return` and `--max_str_len` and passes them
   to `ExcytinEnv`, but also contains an unrelated resume-count change.
2. `excytin_env.py` makes row limiting unconditional when the row count is
   above `max_entry_return`, then independently clips characters at
   `max_str_len`. This changes the previous default behavior for small textual
   results with more than 15 rows.
3. A test-tree search found no dedicated coverage of these two runtime
   parameters. The only truncation match was failure-analysis evidence-excerpt
   truncation, which is unrelated.

The pre-decision recommendation was **conditional inclusion after a split
commit and focused environment tests**. Under approved Candidate A, both
hunks are excluded from the canonical baseline and remain future candidates.

## 3. Untracked no-trunc and runtime files

### Duplicate runner and result directories

```console
$ diff -u experiments/run_exp.py experiments/run_exp_no_trunc.py
@@ -282,7 +282,7 @@
-    base_dir = "final_results"
+    base_dir = "final_results_no_trunc"
```

`experiments/run_exp_no_trunc.py` is excluded because it duplicates the dirty
runner and differs only by output directory. Its SHA-256 is
`d58bb70ddcd54771d009f1c02f40f5da7ac73a0b45e3fa5e2cb9cc3a32865501`.

The output directories are excluded and must never be committed:

| Path | Files | Size | Deterministic manifest SHA-256 | Reason |
| --- | ---: | ---: | --- | --- |
| `experiments/failure_analysis_outputs_no_trunc/` | 93 | 4.8 MiB | `abaf492b7b89aa4eb8c5c6e7d32e7ac34a02ddf992a965b3e28c0d01af0e6600` | Generated failure-analysis output |
| `final_results_no_trunc/` | 23 | 52 MiB | `59ea9c1883b4cefaafcd219d6a5136e7049f08cf5016f4c30de2e7ca191ed54d` | Generated experiment result |

### Supervisor scripts

All 14 scripts are excluded. Every script passes
`--max_entry_return 10 --max_str_len 6000`, which is truncating rather than
no-truncating, and every script matched a filename-only scan for at least one
of `API_KEY`, `TOKEN`, `SECRET`, or `PASSWORD`. No matching value was printed.

| Path | SHA-256 |
| --- | --- |
| `experiments/run_official_flash_incident134_supervisor.sh` | `83fe6b5d1d5bacd2476f7b7ac36aff62a6f012563cf595b41fdc62ee29f1cbf6` |
| `experiments/run_official_flash_incident166_supervisor.sh` | `c417546ea2c70d9553c288406e465ff0fe7fe16c1398a944507bb52f11464f4c` |
| `experiments/run_official_flash_incident166_supervisor_run2.sh` | `8504fe65c880f9fd89afdaf799417eb1a804abc4ba6ae41e2955bfbb029ca352` |
| `experiments/run_official_flash_incident322_supervisor.sh` | `021f30e250dfea42dfd6fdf697117ed23fd67078de0ac8f1c9bbc5c655470427` |
| `experiments/run_official_flash_incident34_supervisor.sh` | `f0f2d78c593651cf1d3a3267ae219c188c3233774cf699c18697801f1eacec30` |
| `experiments/run_official_flash_incident38_supervisor.sh` | `3af0d64175cd337d224daf652b69571d58c6340f67c6b49962d205221eea9fd5` |
| `experiments/run_official_flash_incident38_supervisor_run2.sh` | `87e20a4e1a14552a140f00c1c28e408c596898f64005f68179b77389ed0be24b` |
| `experiments/run_official_flash_incident39_supervisor.sh` | `ef6bb880e24e4b3a3471aba647af8714c6d8ade419c7fc4e56fcbbff6ac5bd5a` |
| `experiments/run_official_flash_incident55_supervisor.sh` | `50ded95fe139db06bc966a3c6ab9165f25d304e97e6d9d3b9c7f9038f9c44d9a` |
| `experiments/run_qwen_incident34_supervisor.sh` | `ecb87d35631b6ebb169374796cd9ffb37a2b666ec2f78e66898a2e8b43c54181` |
| `experiments/run_qwen_incident34_supervisor_run2.sh` | `0b72d33261a5eee1f6b3039bc4ebc9016e3e5feefbcd956dfa586c886128cb7c` |
| `experiments/run_qwen_incident34_supervisor_run3.sh` | `a646b3e20facf9603374c58cf4dbcdb56f84ef757361f7e1bc69b556bf312ed3` |
| `experiments/run_yuk_flash_incident34_supervisor.sh` | `1a1a3537311d69e1242ff00d784fd1ef2e2494b1e62080ee38ffe4cba94770ad` |
| `experiments/run_yuk_flash_incident34_supervisor_run2.sh` | `cc6f2f281bbc2836eadba82f2e75384e7a6ad075a3b550a487ca524c331a09fd` |

### PID files

All PID files are excluded as ephemeral process state:

| Path | SHA-256 |
| --- | --- |
| `experiments/full_incident_134_official_flash.supervisor.pid` | `3279618b9d8fbeb89b594cb3a6fb563f2867700271cffa27aa7cfbf8b23b2e38` |
| `experiments/full_incident_166_official_flash.supervisor.pid` | `e3c03ec6a39d37263bb67b96e36c5c2c2630234320489343071d0f0ffd9282fc` |
| `experiments/full_incident_166_official_flash.supervisor_run2.pid` | `0c64f3d25712222defe678c8549ab20e913f2d955b7e66d047822a1ae747ae4c` |
| `experiments/full_incident_322_official_flash.supervisor.pid` | `a430dec142301ab320a84684b8fdb74346cee06890e2f8d1a8e32257bda04e0a` |
| `experiments/full_incident_34_Qwen3-30B.pid` | `749529233d21fb7e416c1231f7959145b578f0d7d3d11c651f8b7fba88e27951` |
| `experiments/full_incident_34_Qwen3-30B.supervisor_run2.pid` | `e9fde8037a232c4b89b9e80cccef84a7e6cda947f807d77dc51159f13da23274` |
| `experiments/full_incident_34_Qwen3-30B.supervisor_run3.pid` | `5ddff6a1ef1c6afff7f804019c21caa29f1bb82091391e3ce04d3a65b20043b1` |
| `experiments/full_incident_34_official_flash.supervisor.pid` | `040f08e42fb8c6010d0c3ddc036635ac6fa40c2d4bf67d98f2eb91722abcfb15` |
| `experiments/full_incident_34_yuk_flash.supervisor.pid` | `2f88055b68692422f82af1201a9b16da5eccd8a8aab990567c7d97669c4d47e1` |
| `experiments/full_incident_34_yuk_flash.supervisor_run2.pid` | `9d76ac4f43d1832c7fa1b4024f185d2b032cf1d8c53caf7b3c121a767d04c209` |
| `experiments/full_incident_38_official_flash.supervisor.pid` | `04e27496fdc89796f0a493ce026b919132b062e7cefcdc2536cbdb007cc4dd2d` |
| `experiments/full_incident_38_official_flash.supervisor_run2.pid` | `7f02c0bfe0871cef4acb86c01a398ac828efda1b759a8204074356629bcf6b01` |
| `experiments/full_incident_39_official_flash.supervisor.pid` | `371912e5ffb415ad4ddce8a9290d617099491d10466e3951ec5cc5d40868c62c` |
| `experiments/full_incident_55_official_flash.supervisor.pid` | `bc458c6710a9ed48b4b8937b068455a5741742b1f810a70c5ae94ad03e24a569` |
| `full_incident_34_Qwen3-30B.pid` | `e92bb2bbaebcc7045ee7e45d4682a7c50d096868fdd62c6f38d700827a726277` |

### Untracked regression tests

These tests are excluded from the no-trunc baseline because they cover the
excluded model-response/retry/resume/config changes, not the truncation
semantics. They should remain preserved in the Ubuntu working tree.

| Path | SHA-256 |
| --- | --- |
| `tests/test_assistant_response_extraction.py` | `dc7bdea3af973a4b40d030b269449a2244b16f8a7d4d26ea203e9041b709f42f` |
| `tests/test_qwen_output_and_retry.py` | `78471d0a45f593b5ef232b07e9fa14b63e2f8bac89527cd3cdfe63a84ea41924` |
| `tests/test_r1_response_boundary.py` | `2186aacd5604bf61fc65324fe27f4cfde6051ee57056d115b34d0f13c2fd4470` |
| `tests/test_response_model_fallback.py` | `7dfb5da37fee1756f71b4d3360561d1ad7a8a8fc61de1a585b350a33b81f15ef` |
| `tests/test_run_exp_resume_count.py` | `62a6f544056280383f484f156e2c65e9cf7d1dbc0d985b69c8815675a11cf16a` |
| `tests/test_yuk_flash_config.py` | `91900a2dbfcbb7097191b944b2dc9b137d0a3b725c9d845e16d3d2b341c8d684` |

## 4. Python and platform verification

Local does not provide a `python` executable, so it cannot satisfy the plan's
literal local command. The available Homebrew interpreter is recorded only as
additional context:

```console
$ python --version
zsh:1: command not found: python
$ python3 --version
Python 3.14.6
$ uname -m
arm64
```

The authoritative Ubuntu environment is exactly Python 3.11:

```console
$ /home/acuraintegurl/miniconda3/envs/excytin/bin/python --version
Python 3.11.15
$ uname -m
aarch64
```

## 5. Question split verification

The split is `secgym/questions/o1/test`. It contains eight JSON files and 589
question objects:

```console
$ for f in secgym/questions/o1/test/*.json; do jq -r '[input_filename, (length|tostring)] | @tsv' "$f"; done
secgym/questions/o1/test/incident_134_qa_incident_o1-ga_c42.json  57
secgym/questions/o1/test/incident_166_qa_incident_o1-ga_c42.json  87
secgym/questions/o1/test/incident_322_qa_incident_o1-ga_c42.json  56
secgym/questions/o1/test/incident_34_qa_incident_o1-ga_c42.json   82
secgym/questions/o1/test/incident_38_qa_incident_o1-ga_c42.json   11
secgym/questions/o1/test/incident_39_qa_incident_o1-ga_c42.json   98
secgym/questions/o1/test/incident_55_qa_incident_o1-ga_c42.json   100
secgym/questions/o1/test/incident_5_qa_incident_o1-ga_c42.json    98
$ jq -s 'map(length) | add' secgym/questions/o1/test/*.json
589
```

The local and Ubuntu question manifests are identical:

```console
$ find secgym/questions/o1/test -type f -print0 | sort -z | xargs -0 shasum -a 256 | shasum -a 256
53d71d4b7c411680a2e74c184633711e8e98f14b1fdd5b61b72aa7bfa83ebfcd  -
```

## 6. MySQL image verification

The installed Ubuntu image is the Linux arm64 build of MySQL 9.0:

```console
$ docker image inspect mysql:9.0 --format '{{json .RepoDigests}} {{.Id}} {{.Os}}/{{.Architecture}}'
["mysql@sha256:92dc869678019f65d761155dacac660a904f6245bfe1b7997da0a73b2bfc68c9"] sha256:92dc869678019f65d761155dacac660a904f6245bfe1b7997da0a73b2bfc68c9 linux/arm64
```

Per-platform digest:
`sha256:92dc869678019f65d761155dacac660a904f6245bfe1b7997da0a73b2bfc68c9`.

## 7. Source, Evaluator, and built-in Agent hashes

The plan command generated 45 deterministic Python source rows in
`docs/baselines/secrl-lite-files.sha256`. The manifest SHA-256 is
`d9a19f1dc4ab97dee16dc676157efbe527770de8cbebb0515193bf7ff84d7fe8`.

```console
$ wc -l docs/baselines/secrl-lite-files.sha256
45 docs/baselines/secrl-lite-files.sha256
$ LC_ALL=C sort -c -k2 docs/baselines/secrl-lite-files.sha256
$ shasum -a 256 docs/baselines/secrl-lite-files.sha256
d9a19f1dc4ab97dee16dc676157efbe527770de8cbebb0515193bf7ff84d7fe8  docs/baselines/secrl-lite-files.sha256
```

The local candidate Evaluator and built-in Agent rows are:

| Path | SHA-256 |
| --- | --- |
| `secgym/evaluator.py` | `b146af231c0b63d7252c5b7852c62f0ba59ab40980b65be5d003cbc2f08d05e2` |
| `secgym/agents/__init__.py` | `229771b91067a7bb719fee94bf5a0cea5d3f224b9aa0f963b7ea60364e565838` |
| `secgym/agents/agent.py` | `cba7b1f0f33d1bc86e2c8fe5403c37234c272df24823afcc08dccea804f3e874` |
| `secgym/agents/agent_utils.py` | `bc948ae7bdc4172826cd611882d1060bcd3dad42255f7e7df266f5d520e5e37f` |
| `secgym/agents/baseline_agent.py` | `539ce0d0175b848c6c09683be0ff1bdeeb6380f44354e8439c51a6e01fae1717` |
| `secgym/agents/expel_agent.py` | `380eece196c5531949cb8990c25654f26f1badde39517dbab257c17c979d8fe0` |
| `secgym/agents/maset_slave_agent.py` | `f04d63909647a103a211b2deb3afcdb9ae48aa9a65cf4d8f19578599113eac8c` |
| `secgym/agents/prompt_sauce_agent.py` | `af49413a76aba5fed56570df4e9c24104dccfef33ec2d7cb3e5118fca07a912a` |
| `secgym/agents/prompt_sauce_reflexion_agent.py` | `bd539f7e7fb7a9cdd8ee2cf2ad095accdd6ab4d3e28d68ce42b74fc36d68d84d` |
| `secgym/agents/react_agent.py` | `d2bff734e72efd36e4b1a4120a9f383ceff7606bb6904b62906b807f1fdc232d` |
| `secgym/agents/react_reflexion_agent.py` | `d1f62c2f0c4037512992b44988a3703e973a043c66414a100cf1d66f63f658f4` |

The Ubuntu working hashes differ only for `agent_utils.py`,
`baseline_agent.py`, and `evaluator.py`; all other rows above match Ubuntu.
Those three working hashes are listed in section 2 and are excluded from the
no-trunc recommendation.

## 8. Test evidence

### Authoritative Ubuntu failure-analysis suite

```console
$ /home/acuraintegurl/miniconda3/envs/excytin/bin/python -m unittest discover -s tests/failure_analysis -v
----------------------------------------------------------------------
Ran 73 tests in 0.023s

OK
```

### Ubuntu dirty-change regression tests

The six untracked modules contain seven tests. They all pass, but none covers
the conditional truncation candidates:

```console
$ /home/acuraintegurl/miniconda3/envs/excytin/bin/python -m unittest -v tests.test_assistant_response_extraction tests.test_qwen_output_and_retry tests.test_r1_response_boundary tests.test_response_model_fallback tests.test_run_exp_resume_count tests.test_yuk_flash_config
----------------------------------------------------------------------
Ran 7 tests in 0.003s

OK
```

### Additional local suite

The local commit contains newer SQL-retrieval failure-analysis modules not
present at the Ubuntu comparison commit. With the available, non-authoritative
Python 3.14.6 interpreter:

```console
$ python3 -m unittest discover -s tests/failure_analysis -v
----------------------------------------------------------------------
Ran 156 tests in 0.187s

OK
```

## 9. Commit gate

The commit gate was evaluated after manual approval:

- `secrl-lite-files.sha256` was regenerated against the approved tree and its
  hash rechecked;
- the authoritative Python 3.11 suite and the additional local suite were
  rerun with the results in section 8;
- the complete staged diff and exact staged file list were displayed before
  commit;
- only `docs/baselines/` and `tests/fixtures/platform/baseline/` were staged;
- `.superpowers`, `failure_analysis_outputs_no_trunc/`,
  `final_results_no_trunc/`, supervisor/PID files, configuration, and API keys
  were absent from the staged payload.
