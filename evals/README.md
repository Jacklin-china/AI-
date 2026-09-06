# evals · 评测集与报告

- 评测集：`*.jsonl`，每行一个对象：`{"id", "input", "expect", "rubric"}`
- 运行：M1 起提供 `uv run python scripts/run_eval.py --suite evals/storyboard.jsonl`
- 报告归档：`docs/evals/Mx-日期.md`；**里程碑打 tag 前必须跑一次**（docs/01 §6）
