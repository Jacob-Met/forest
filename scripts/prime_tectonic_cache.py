#!/usr/bin/env python3
"""Fetch public TeX dependencies using fixed setup documents, never user source."""
from pathlib import Path
import shutil
import subprocess
import tempfile


# The source is constant: downloads and TeX execution occur only in a trusted
# setup step. Later manuscript compilation runs with --only-cached inside bwrap.
_SETUP_DOCUMENT = r"""
\documentclass[SIZEpt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath,amssymb,booktabs,graphicx,hyperref,natbib,array,longtable,placeins,flafter}
\title{Public TeX package and font preflight}
\author{FOREST setup}
\date{}
\begin{document}
\maketitle
\section{A section}
\subsection{A subsection}
\begin{abstract}Fixed setup text.\end{abstract}
\textbf{Bold} \textit{Italic} \texttt{Monospace}
\small Small \normalsize Normal \large Large \Large Larger
\LARGE Very large \huge Huge \Huge Biggest \normalsize
\[
x + y = \frac{\alpha}{\beta} + \sum_{n=1}^{9} n + \infty
\]
\begin{tabular}{lr} A & 1 \\ B & 2\end{tabular}
\end{document}
"""


# Fonts the manuscript layout can request at fixed table/math sizes, not
# necessarily exercised by a single document's ordinary title/paragraphs.
_PUBLIC_FONTS = tuple(
    f"{family}{size}"
    for family, sizes in (
        ("cmr", (5, 6, 7, 8, 9, 10, 12, 17)),
        ("cmsy", (5, 6, 7, 8, 9, 10)),
        ("cmmi", (5, 6, 7, 8, 9, 10)),
        ("cmex", (7, 8, 9, 10)),
    )
    for size in sizes
)
_FONT_REQUESTS = "\n".join(
    r"\font\warmfont=" + name + r" at 10pt \warmfont\char" + r'"06\par'
    for name in _PUBLIC_FONTS
)


def main():
    tectonic = shutil.which("tectonic")
    if not tectonic:
        raise SystemExit("Install Tectonic before priming its public package cache")
    with tempfile.TemporaryDirectory(prefix="forest-tex-prime-") as temporary:
        directory = Path(temporary)
        source = directory / "paper.tex"
        for size in (10, 11, 12):
            source.write_text(_SETUP_DOCUMENT.replace("SIZE", str(size)).replace(
                r"\end{document}", _FONT_REQUESTS + "\n" + r"\end{document}"))
            process = subprocess.run(
                [tectonic, "--untrusted", "--keep-logs", "--outdir", str(directory),
                 str(source)], cwd=directory, capture_output=True, text=True,
                timeout=180,
            )
            if process.returncode != 0:
                raise SystemExit(f"Failed preparing public TeX resources ({size}pt):\n"
                                 + (process.stdout + process.stderr)[-3500:])
    print("Tectonic public package cache primed for FOREST manuscripts")


if __name__ == "__main__":
    main()
