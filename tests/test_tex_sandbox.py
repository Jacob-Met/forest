"""Real-TeX regression for manuscript reads outside the paper bundle."""
import shutil
import os
import subprocess

from pypdf import PdfReader
import pytest

from research.paper.manuscript import compile_paper, _sandboxed_tex_command


def _document(include):
    return (
        r"\documentclass{article}" + "\n"
        r"\begin{document}" + "\n"
        + include + "\n"
        r"\end{document}" + "\n"
    )


@pytest.mark.parametrize("compiler", ["pdflatex", "tectonic"])
def test_compiler_reads_local_includes_but_not_host_files(tmp_path, monkeypatch, compiler):
    if not shutil.which(compiler) or not shutil.which("bwrap"):
        pytest.skip(f"{compiler} and Linux bubblewrap are required for real sandbox regression")

    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name in {"pdflatex", "tectonic"} and name != compiler else which(name))

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "allowed.tex").write_text("Bundled source is readable.")
    (paper / "paper.tex").write_text(_document(r"\input{allowed.tex}"))
    good = compile_paper(paper)
    assert good["status"] == "completed", good["log"]
    assert "Bundled source is readable" in PdfReader(good["pdf_path"]).pages[0].extract_text()

    private = tmp_path / "host-private-fixture.tex"
    private.write_text(r"\typeout{HOST_PRIVATE_SENTINEL_593}" + "\n" + "Host private document contents.\n")
    (paper / "paper.tex").write_text(_document(r"\input{../host-private-fixture.tex}"))
    escaped = compile_paper(paper)
    assert escaped["status"] != "completed", escaped["log"]
    assert "HOST_PRIVATE_SENTINEL_593" not in escaped["log"]
    assert not (paper / "paper.pdf").exists()

    (paper / "link.tex").symlink_to(private)
    (paper / "paper.tex").write_text(_document(r"\input{link.tex}"))
    symlink = compile_paper(paper)
    assert symlink["status"] != "completed", symlink["log"]
    assert "HOST_PRIVATE_SENTINEL_593" not in symlink["log"]
    assert not (paper / "paper.pdf").exists()


def test_compilation_does_not_return_a_pdf_for_source_changed_during_tex(tmp_path, monkeypatch):
    if not shutil.which('pdflatex') or not shutil.which('bwrap'):
        pytest.skip('Real sandboxed pdflatex required')
    from research.paper import manuscript
    (tmp_path / 'paper.tex').write_text(_document('Original at invocation.'))
    real_run = subprocess.run
    calls = 0

    def changed_after_first_pass(*args, **kwargs):
        nonlocal calls
        result = real_run(*args, **kwargs)
        calls += 1
        if calls == 1:
            (tmp_path / 'paper.tex').write_text(_document('Changed during compilation.'))
        return result

    monkeypatch.setattr(manuscript.subprocess, 'run', changed_after_first_pass)
    result = compile_paper(tmp_path)
    assert calls >= 1
    assert result['status'] == 'failed'
    assert 'Manuscript inputs changed during compilation' in result['log']
    assert result['compilation_provenance'] is None
    assert not (tmp_path / 'paper.pdf').exists()


def test_missing_os_sandbox_fails_closed(tmp_path, monkeypatch):
    if not (shutil.which("pdflatex") or shutil.which("tectonic")):
        pytest.skip("A real TeX compiler is required")
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "bwrap" else which(name))
    (tmp_path / "paper.tex").write_text(_document("No unrestricted fallback."))
    (tmp_path / "paper.pdf").write_bytes(b"stale")
    result = compile_paper(tmp_path)
    assert result["status"] == "unavailable", result["log"]
    assert not (tmp_path / "paper.pdf").exists()


def test_worker_secrets_not_inherited_by_sandbox_supervisor(tmp_path, monkeypatch):
    if not shutil.which("pdflatex") or not shutil.which("bwrap"):
        pytest.skip("Real pdflatex and bwrap are required")

    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "paper.tex").write_text(_document("Public source."))
    monkeypatch.setenv("FOREST_PRIVATE_ENV_PROBE", "FOREST_TEST_SENTINEL_593")

    import research.paper.manuscript as manuscript
    actual_run = subprocess.run
    observed = []

    def capture_run(*args, **kwargs):
        observed.append(kwargs.get("env"))
        return actual_run(*args, **kwargs)

    monkeypatch.setattr(manuscript.subprocess, "run", capture_run)
    result = compile_paper(paper)
    assert result["status"] == "completed", result["log"]
    assert observed and all(env == {} for env in observed)

    # An actual process inside the same namespace must not see the caller's
    # environment through the bwrap supervisor's /proc/*/environ.
    probe = _sandboxed_tex_command(
        ["/usr/bin/sh", "-c",
         'for p in /proc/[0-9]*/environ; do tr "\\000" "\\n" < "$p" 2>/dev/null | grep -q FOREST_TEST_SENTINEL_593 && echo EXPOSED; done'],
        paper,
    )
    tested = actual_run(probe, env={}, capture_output=True, text=True, timeout=10)
    assert "EXPOSED" not in tested.stdout



def test_compiler_rejects_option_like_source_name(tmp_path):
    (tmp_path / "-output.tex").write_text(_document("Argument must not become a CLI option."))
    with pytest.raises(ValueError, match="Compile a .tex source"):
        compile_paper(tmp_path, "-output.tex")
