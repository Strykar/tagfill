"""What `tagfill run` does, as a function.

Which stages run, in what order, that stage 1 needs --convert-wav, that
stage 9 stays explicit, that --offline skips the network ones: all of that
used to live in cli.main, so anything embedding tagfill had to reimplement
it and would drift the next time it changed. The CLI is a client of this,
not the other way round.
"""

from __future__ import annotations

import importlib

from .stages import STAGES, Context, StagePrecondition

SUBMIT = 9      # never runs unattended; it posts to MusicBrainz
CONVERT = 1     # the one stage that replaces a file, so it is opt-in


def stage_module(module: str):
    return importlib.import_module(f".stages.{module}", package=__package__)


def run(ctx: Context, *, offline: bool = False,
        convert_wav: bool = False) -> list[tuple[str, str]]:
    """Run the pipeline in order. Returns (stage, outcome) per stage, where
    outcome is "ran" or the reason it was skipped -- a GUI needs to tell
    "nothing needed doing" from "never ran", and prose on stdout cannot."""
    outcomes = []
    for num, name, module, network in STAGES:
        if num == SUBMIT:
            outcomes.append((name, "skipped: submit stays explicit"))
            continue
        if num == CONVERT and not convert_wav:
            ctx.say(f"-- stage {num} {name}: skipped (pass --convert-wav "
                    f"to enable)")
            outcomes.append((name, "skipped: --convert-wav not given"))
            continue
        if network and offline:
            ctx.say(f"-- stage {num} {name}: skipped (--offline)")
            outcomes.append((name, "skipped: --offline"))
            continue
        ctx.say(f"-- stage {num} {name}")
        try:
            stage_module(module).run(ctx)
        except StagePrecondition as e:
            # One misconfigured optional stage must not end the pipeline.
            ctx.say(f"   {name}: {e}")
            outcomes.append((name, f"refused: {e}"))
            continue
        outcomes.append((name, "ran"))
    return outcomes
