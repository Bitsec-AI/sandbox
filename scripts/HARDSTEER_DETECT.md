# HardSteer Detect in Claude Code Opus 4.5

validator/curated-high.json is the full known solution list. reduce to only include the projects that are in the codebases list for the eval set, rename to eval-set-curated-high.json.

## Prompt

this is an agent competition, the solutions are known by contestants ahead of time. i want agents that do well in finding exploits in the public dataset, and also generalize well across out of sample codebases.

tmp/agent-{number}.py this is a submission into the contest
validator/eval-set-curated-high.json this is the known solution list

is there strong evidence this submission is hardcoded, using hard steering, or a legitimate entry?

phase 2. gather the evidence and give me a confidence percentage 0% (valid entry) to 100% (very obviously hardcoded or hard steering)
