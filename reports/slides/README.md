# Risk Studio – slides (LaTeX Beamer)

Beamer version of the HTML deck "Risk Studio – Thuyết trình CO4031" (17 slides, 16:9).
It reproduces the deck's layout, colours and type: Be Vietnam Pro 400/600/800 on a
1920×1080 px canvas with 128 px margins.

## Files

| Path | What it is |
|---|---|
| `slides.tex` | The deck: one `frame` per slide, speaker notes in `\note{...}` |
| `beamerthemeriskstudio.sty` | Theme: colours, fonts, footer band, drawing macros |
| `fonts/` | Be Vietnam Pro (Regular, SemiBold, ExtraBold, Italic) + `OFL.txt` licence |
| `images/` | Screenshots used on slides 9, 14 and 15 |

## Build

The deck needs **XeLaTeX** because of `fontspec`. The fonts load from `fonts/` with
`Path=fonts/`, so no system font is needed.

```sh
latexmk -xelatex slides.tex
```

Two passes are normal. latexmk runs them for you.

**Overleaf:** upload the whole folder (keep `fonts/` and `images/` next to
`slides.tex`), then set *Menu → Compiler* to **XeLaTeX**.

Required packages (all in TeX Live / Overleaf): `beamer` (with its dependency
`translator`), `fontspec`, `tikz`/`pgf`, `xcolor`, `graphicx`.

## Speaker notes

Each frame ends with `\note{...}`, which holds the HTML deck's speaker notes word for
word. Edit the text inside the braces. In note text, escape `%` as `\%` and `_` as `\_`.

By default notes are hidden. To print them, uncomment **one** line near the top of
`slides.tex`:

```latex
\setbeameroption{show notes on second screen=right} % slide + notes side by side
\setbeameroption{show only notes}                   % notes-only handout
```

Second-screen mode makes each page twice as wide: the slide on the left, the notes on
the right. Presenter tools such as `pympress` or `dspdfviewer` can split it across two
screens.

## Editing slides

Coordinates are CSS pixels from the top-left corner of a 1920×1080 canvas, the same
numbers as in the HTML/CSS. The main macros (documented in the theme file):

- `\rsslide[light|accent|dark]{presenter}{drawing}`: one per frame. Sets the
  background and the footer ("Risk Studio · CO4031", "Trình bày: …", page number).
  Leave the presenter empty for no footer (cover and closing slide).
- `\rsHead{eyebrow}{title}`: eyebrow and title at the top margin.
- `\rsT[opts]{x}{y}{font}{text}` for one line, `\rsTw[opts]{x}{y}{width}{font}{text}`
  for wrapped text. Fonts: `\fBig \fTitle \fNsixty \fHthree \fBody \fBodyS \fSmall
  \fSmallS \fEyebrow` (112/60/60/36/28/24 px; `S` = SemiBold).
- `\rsCard`, `\rsCardSoft`, `\rsPill`, `\rsChip`, `\rsNode`, `\rsShot`,
  `\rsTable` + `\rsRow`, and the `rs conn` arrow style for connectors.

If you change a text so that it wraps onto more lines, move the elements below it
down by one line height: body 37.8 px, small 32.4 px.

## Names and placeholders

The deck uses the real team data: three students of class L02 (Doãn Phương Hùng
Cường, Nguyễn Thiên Hào, Nguyễn Khắc Khánh Huy) and the advisor (GVHD: ThS. Đỗ Thanh
Thái). They appear on the cover (frame 1), the closing slide (frame 17), the footer
presenter (`\rsslide{…}` on frames 2–16) and in the notes. There are no `[...]`
placeholders left to fill.
