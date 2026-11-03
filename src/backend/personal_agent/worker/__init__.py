"""The Worker: a task's own ChatSession, driven by core ChatRuns (SUPERAGENT_DESIGN §6, §8).

* ``inputs``: which task updates the Worker session has not received yet, and the hidden rows it reads;
* ``activation``: the text and run context of one activation, accepted and started as a core ChatRun;
* ``settle``: what a finished or failed run means for the task and its Assignment;
* ``executor``: claims tasks into slots, launches and wakes Workers, delivers input, settles;
* ``tools``: the Worker's own tools (``pa_report``, the guarded core ``Bash``).
"""
