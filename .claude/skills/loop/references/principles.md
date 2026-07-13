# LOOPS.md — the nine rules (distilled)

*Field notes on agents that run for days. Adapted from Andrej Karpathy's
`loops.md` (v060726). The prompts in this skill set encode these rules; this
file is the theory, kept short so it's actually re-read.*

Most agent systems die not from a weak model but from a weak harness. The model
can write code, review code, and verify its own output against a rubric it
agreed to ten minutes ago. What it cannot do on its own is decide **when to
stop, when to restart, and where to write the result.** That is the work of the
loop. Treat the loop as a first-class object: separate the roles, keep state on
disk, negotiate contracts before the first line of code, and read the harness
like a stack trace when something goes wrong. Short loops, simple state, clean
contracts. Everything else is decoration.

**I. Write the loop, not the prompt.** The unit of leverage stopped being the
prompt the moment models could follow a procedure without supervision. If you're
iterating on a single message at 3am, you're still in the prompting era. The loop
is short: gather, reason, act, verify, repeat.

**II. Separate the roles.** Three roles, three context windows, three system
prompts. A planner that turns a vague sentence into a sprint spec and never
touches code. A generator that writes everything and is forbidden from grading
its own work. An evaluator that reads diffs, launches the app, and is told from
message one that the code is broken and its job is to prove it. Mixing roles is
the most common failure — the model turns sycophantic the moment it grades itself
and the loop converges on slop.

**III. Negotiate the contract first.** Before the generator writes a line, it
proposes what "done" looks like and the evaluator pushes back. They argue via
markdown files until they agree on a checklist of testable assertions. ~10–30 for
a small app; ten is usually too few and gets rubber-stamped. The planner's spec
is the boundary; the **contract is what gets graded.** This is the single change
that moves runs from broken demos to working products.

**IV. Write to disk, not to context.** Context windows lie — they compact, rot,
and hide what you said an hour ago behind a summary you didn't write. A file does
not. Keep `feature_list.json`, `progress.md`, `contract.md`, and an append-only
`log.md` with `## [YYYY-MM-DD] op | title` entries. The model should be able to
crash, lose its session, and pick up where it left off by reading three files. If
you can't describe your state in three files, your state is too complicated.

**V. Let the loop restart.** The best behavior from a good model is the
willingness to throw everything away and start over when a run goes sideways —
delete the project at iteration nine and ship a working version at iteration
eleven. Do not interrupt this. The restart is the loop working correctly. Insert
a human only when the contract itself is wrong, not when the build is.

**VI. Score the subjective.** Taste is gradable if you write it down. Four axes,
weighted: design, originality, craft, functionality. Calibrate on three reference
examples the evaluator is told are good and three it's told are slop. Output is a
number in `[0,1]` plus a paragraph explaining the gap. The model won't invent
taste; it converges toward the taste you described. The whole game is writing the
rubric carefully enough that converging on it is what you actually wanted.

**VII. Read the traces.** Every debugging insight about loops comes from reading
the raw transcript, not from running another experiment. Pipe output to a file,
grep for the moment its judgment diverged from yours, edit the prompt for that
exact moment, run again. Same muscle as reading a stack trace — the difference is
the trace is in English and most of it is the model talking to itself. Skip this
and you're tuning by vibe.

**VIII. Delete the harness.** The harness exists to compensate for the model. As
the model improves, half of what you wrote last quarter becomes overhead. Re-read
the harness against each new release and delete anything the model now does for
free. A harness that grows monotonically is one you've stopped reading.

**IX. The bottleneck always moves.** When coding stops being the bottleneck,
planning becomes it; when planning is solved, verification; when verification is
automated, taste. You don't finish — you find the next thing to fix. The whole
point of the loop is to make the next bottleneck visible. If everything is going
smoothly, you aren't looking carefully enough.
