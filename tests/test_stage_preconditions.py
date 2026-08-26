"""A stage that cannot start says so in a way a caller can act on.

External review, on embedding tagfill: every run(ctx) returned None, and a
missing MusicBrainz contact or an absent fpcalc became a ctx.say() and a
bare return. So there was no machine-readable difference between "ran,
wrote 40 files", "ran, nothing needed doing" and "refused, misconfigured"
-- all three looked identical to anything but a human reading stdout.

Preconditions raise now. Per-file resilience is deliberately unchanged: a
single unwritable file is still a journalled skip, because that is a
result, not a refusal to start.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from tagfill import cli, config, pipeline
from tagfill.journal import Journal, ReviewQueue
from tagfill.stages import Context, StagePrecondition, acoustid, mb


def _ctx(tmp_path, **cfg_overrides):
    cfg = config.Config()
    cfg.root, cfg.workdir = tmp_path / "Music", tmp_path / "work"
    cfg.root.mkdir(parents=True, exist_ok=True)
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    return Context(cfg=cfg, journal=Journal(cfg.workdir),
                   review=ReviewQueue(cfg.workdir))


def test_mb_without_a_contact_refuses(tmp_path):
    with pytest.raises(StagePrecondition, match="contact"):
        mb.run(_ctx(tmp_path, mb_contact=""))


def test_acoustid_without_a_key_refuses(tmp_path, monkeypatch):
    # acoustid_key is a property over three sources; empty all of them.
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    ctx = _ctx(tmp_path, acoustid_api_key="",
               acoustid_key_file=str(tmp_path / "no-such-key"))
    with pytest.raises(StagePrecondition, match="API key"):
        acoustid.run(ctx)


def test_the_pipeline_keeps_going_past_a_refusal(tmp_path, monkeypatch):
    """One unconfigured optional stage must not end the run. mb needs a
    contact nobody set; census and the rest still have work to do."""
    ctx = _ctx(tmp_path, mb_contact="")
    ctx.say = lambda _m: None
    ran = []

    real = pipeline.stage_module

    def fake(module):
        if module == "mb":
            return real(module)
        return type("M", (), {"run": staticmethod(
            lambda _c: ran.append(module))})()

    monkeypatch.setattr(pipeline, "stage_module", fake)
    outcomes = dict(pipeline.run(ctx))

    assert outcomes["mb"].startswith("refused: ")
    assert "contact" in outcomes["mb"]
    assert outcomes["census"] == "ran"
    assert "report" in ran, "stages after the refusal still ran"


def test_asking_for_that_stage_by_name_is_an_error(tmp_path, capsys):
    """Different context, different answer: you named this stage, and it
    cannot start."""
    (tmp_path / "Music").mkdir()
    rc = cli.main(["--music-dir", str(tmp_path / "Music"),
                   "--workdir", str(tmp_path / "work"), "mb"])
    assert rc == 1
    assert "contact" in capsys.readouterr().out


def test_nothing_to_do_is_not_a_refusal(tmp_path, capsys):
    """census over an empty collection ran fine and found nothing. That is
    a result, and it must not look like a misconfiguration."""
    (tmp_path / "Music").mkdir()
    rc = cli.main(["--music-dir", str(tmp_path / "Music"),
                   "--workdir", str(tmp_path / "work"), "census"])
    assert rc == 0
    assert "0 files" in capsys.readouterr().out
