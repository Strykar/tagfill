"""Pipeline orchestration is a library function, not something living in
cli.main.

External review, on embedding tagfill in a GUI: which stages run, in what
order, the stage-1-needs---convert-wav rule, the --offline skip, submit
staying explicit -- a caller using stages.*.run(ctx) directly had to
reimplement all of it, and would drift the next time it changed. These
tests pin the rules at the library level so that stays true.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tagfill import config, pipeline
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import STAGES, Context


def _ctx(tmp_path, monkeypatch):
    cfg = config.Config()
    cfg.root, cfg.workdir = tmp_path / "Music", tmp_path / "work"
    cfg.root.mkdir(parents=True)
    ctx = Context(cfg=cfg, journal=Journal(cfg.workdir),
                  review=ReviewQueue(cfg.workdir))
    ctx.say = lambda _msg: None
    ran = []
    monkeypatch.setattr(pipeline, "stage_module",
                        lambda m: type("M", (), {
                            "run": staticmethod(lambda _c: ran.append(m))})())
    return ctx, ran


def test_the_cli_runs_the_pipeline_rather_than_its_own_loop(tmp_path,
                                                            monkeypatch):
    """Behavioural rather than a grep for "pipeline.run(" in cli.py: what
    matters is that `tagfill run` goes through the same function an
    embedder calls, so the two cannot drift."""
    from tagfill import cli
    music = tmp_path / "Music"
    music.mkdir()
    ran = []
    monkeypatch.setattr(pipeline, "stage_module",
                        lambda m: type("M", (), {
                            "run": staticmethod(lambda _c: ran.append(m))})())

    rc = cli.main(["--music-dir", str(music), "--workdir",
                   str(tmp_path / "work"), "run", "--offline"])

    assert rc == 0
    assert ran, "the CLI ran no stages through pipeline.stage_module"
    assert "submit" not in ran and "convert" not in ran
    assert ran == [m for num, _n, m, net in STAGES
                   if num not in (1, 9) and not net]


def test_submit_never_runs_unattended(tmp_path, monkeypatch):
    ctx, ran = _ctx(tmp_path, monkeypatch)
    outcomes = pipeline.run(ctx)
    assert "submit" not in ran
    assert ("submit", "skipped: submit stays explicit") in outcomes


def test_convert_is_opt_in(tmp_path, monkeypatch):
    ctx, ran = _ctx(tmp_path, monkeypatch)
    pipeline.run(ctx)
    assert "convert" not in ran

    ctx, ran = _ctx(tmp_path / "again", monkeypatch)
    pipeline.run(ctx, convert_wav=True)
    assert "convert" in ran


def test_offline_skips_exactly_the_network_stages(tmp_path, monkeypatch):
    ctx, ran = _ctx(tmp_path, monkeypatch)
    pipeline.run(ctx, offline=True)
    network = {m for num, _n, m, net in STAGES if net and num != 9}
    assert not (set(ran) & network)
    offline_stages = {m for num, _n, m, net in STAGES
                      if not net and num != 1}
    assert offline_stages <= set(ran)


def test_stages_run_in_registry_order(tmp_path, monkeypatch):
    ctx, ran = _ctx(tmp_path, monkeypatch)
    pipeline.run(ctx, convert_wav=True)
    expected = [m for num, _n, m, _net in STAGES if num != 9]
    assert ran == expected


def test_outcomes_distinguish_ran_from_skipped(tmp_path, monkeypatch):
    """A GUI needs "nothing needed doing" to look different from "never
    ran", and prose on stdout cannot carry that."""
    ctx, _ran = _ctx(tmp_path, monkeypatch)
    outcomes = dict(pipeline.run(ctx, offline=True))
    assert outcomes["census"] == "ran"
    assert outcomes["mb"].startswith("skipped:")
    assert outcomes["convert"].startswith("skipped:")
