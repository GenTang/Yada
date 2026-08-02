# pytest-dev__pytest-10051

This is a checked-in development recipe for one official SWE-bench Verified
instance. The public problem statement in `instance.json`, base commit, test
patch, FAIL_TO_PASS test, and PASS_TO_PASS tests are copied from the canonical
dataset row at revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`.
The case pins Python 3.9.10 and its Python packages independently of Yada's own
Python 3.11+ environment.

From the Yada repository root:

```bash
export DEEPSEEK_API_KEY="sk-..."
uv run yada eval --case benchmarks/swebench_verified/pytest-10051 --agent yada --yes
```

This local result is optimized for fast feedback. It is not a substitute for
the official Docker Harness result.
