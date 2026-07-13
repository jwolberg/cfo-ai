"""Synthetic households — ground truth for the shadow-mode harness.

This is a **simulation**, not a product surface. Nothing in `engine/` may import from it.
It exists so the grader (#3) and the replay driver (#4) can be built and tested against
households whose realized future is known, before there are consented users whose realized
future is real.
"""
