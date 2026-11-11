%======================================================================
% hcmut-report.cls
% HCMUT technical report class (built on top of the article class)
%
% File map:
%   1.  Class identification and base class
%   2.  Package loading
%   3.  Colour definitions
%   4.  Page geometry, spacing and numbering depth
%   5.  Hyperref, cross-references and document hooks
%   6.  Date helper commands
%   7.  Report metadata (customisable with setter macros)
%   8.  \startreport - opening page setup
%   9.  Source-code listing styles and helper commands
%   10. Cover page
%   11. Headers and footers
%======================================================================

%---------------------------------------------------------------------
% 1. Class identification and base class
%---------------------------------------------------------------------
\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{hcmut-report}[20/11/24 v1.0 HCMUT Report Class]

\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}} % All options go to 'article'
\ProcessOptions\relax
\LoadClass[12pt,a4paper]{article}

%---------------------------------------------------------------------
% 2. Package loading
%---------------------------------------------------------------------
% --- Text encoding and fonts ---
\RequirePackage[utf8]{vntex,inputenc}
\RequirePackage[T5]{fontenc}
\RequirePackage{anyfontsize}
\RequirePackage[protrusion=false]{microtype}

% --- Localisation ---
\RequirePackage[vietnamese]{babel} % Change to '[vietnamese]' to use Vietnamese labels

% --- Mathematics and colour ---
\RequirePackage{amsmath,amsthm,amssymb}
\theoremstyle{definition}
\newtheorem{definition}{Definition}
\theoremstyle{remark}
\newtheorem{remark}{Remark}[section]
\theoremstyle{proposition}
\newtheorem{proposition}{Proposition}[section]
\RequirePackage{xcolor}
\RequirePackage{algorithm, algpseudocode}

% --- Page layout and spacing ---
\RequirePackage{geometry}
\RequirePackage{indentfirst,setspace}
\RequirePackage{enumitem}

% --- Graphics and figures ---
\RequirePackage{graphicx,tikz}
\RequirePackage{wrapfig}
\RequirePackage{mermaid}
\RequirePackage[center]{caption}
\RequirePackage{subcaption,float}
\RequirePackage{pgfplots}

% --- Tables ---
\RequirePackage{booktabs,array,multirow,multicol}
\RequirePackage{longtable,tabularx,makecell}

% --- Code listings ---
\RequirePackage{listings}
\RequirePackage[newfloat,cache=false]{minted}
\RequirePackage{dirtree}

% --- Boxes, headers, page references, links and bibliography ---
\RequirePackage{tcolorbox}
\RequirePackage{fancyhdr,lastpage}
\RequirePackage[super,square,sort]{natbib}
\RequirePackage{acronym}
\RequirePackage[title,toc,page]{appendix}
\RequirePackage{hyperref}
\RequirePackage{etoolbox}

%---------------------------------------------------------------------
% 3. Colour definitions
%---------------------------------------------------------------------
\definecolor{keywordcolor}{rgb}{0.13,0.13,1}      % Blue keywords
\definecolor{stringcolor}{rgb}{0.7,0.13,0.13}     % Dark red strings
\definecolor{commentcolor}{rgb}{0.25,0.5,0.35}    % Green comments
\definecolor{backgroundcolor}{rgb}{0.97,0.97,0.97}% Light background
\definecolor{numbercolor}{rgb}{0.85,0.65,0.13}    % Numbers
\definecolor{modal-color}{rgb}{0.10,0.74,0.33}    % Modal logo

%---------------------------------------------------------------------
% 4. Page geometry, spacing and numbering depth
%---------------------------------------------------------------------
\geometry{
	paper=a4paper,
	left=2cm,
	right=2cm,
	top=1.5cm,
	bottom=1.5cm,
	includeheadfoot=true,
	headheight=30pt
}
\onehalfspacing
\raggedbottom

\renewcommand{\arraystretch}{1.2}
\renewcommand{\headrulewidth}{0.3pt}
\renewcommand{\footrulewidth}{0.3pt}

\setcounter{secnumdepth}{3}
\setcounter{tocdepth}{3}

%---------------------------------------------------------------------
% 5. Hyperref, cross-references and document hooks
%---------------------------------------------------------------------
\hypersetup{
	colorlinks=true,
	linkcolor=black,
	citecolor=red,
	urlcolor=blue
}

\AtBeginDocument{\renewcommand*{\bibname}{References}}

% Number equations, tables, figures and listings per section
\AtBeginDocument{%
	\counterwithin{equation}{section}%
	\counterwithin{table}{section}%
	\counterwithin{figure}{section}%
	\counterwithin{lstlisting}{section}%
}

% Tighten math spacing inside list environments
\newcommand{\tightenmathspacing}{%
	\setlength{\abovedisplayskip}{3pt}%
	\setlength{\belowdisplayskip}{3pt}%
	\setlength{\abovedisplayshortskip}{3pt}%
	\setlength{\belowdisplayshortskip}{3pt}%
}
\AtBeginEnvironment{equation}{\tightenmathspacing}
\AtBeginEnvironment{align}{\tightenmathspacing}
% \AtBeginEnvironment{description}{\tightenmathspacing}

%---------------------------------------------------------------------
% 6. Date helper commands
%---------------------------------------------------------------------
% Return current date
\newcommand{\Month}{%
	\ifcase\the\month
	\or January
	\or February
	\or March
	\or April
	\or May
	\or June
	\or July
	\or August
	\or September
	\or October
	\or Tháng mười một
	\or December
	\fi % End of \ifcase
}
\newcommand{\DateToday}{%
	\the\day\ \Month
}

%---------------------------------------------------------------------
% 7. Report metadata
% Override any of these from the preamble using the matching setters,
% e.g. \stuname{...}, \reportplace{...}, \reporttime{...}.
%---------------------------------------------------------------------
\newcommand{\@upperuniname}{ĐẠI HỌC QUỐC GIA THÀNH PHỐ HỒ CHÍ MINH}
\newcommand{\@uniname}{TRƯỜNG ĐẠI HỌC BÁCH KHOA}
\newcommand{\@deptname}{KHOA KHOA HỌC \& KỸ THUẬT MÁY TÍNH}
\newcommand{\@coursename}{Kho Dữ liệu và Hệ hỗ trợ Quyết định\\CO4031 -- HK261}
\newcommand{\@reporttype}{Báo cáo Bài tập lớn}
\renewcommand{\@title}{Quy trình Thẩm định Tín dụng\\Home Credit Default Risk}
\newcommand{\@advisor}{& Thạc sĩ Đỗ Thanh Thái}
\newcommand{\@stuname}{& Doãn Phương Hùng Cường & L02 2310381\\
	& Nguyễn Thiên Hào & L02 2310853\\
	& Nguyễn Khắc Khánh Huy & L02 2311186}
\newcommand{\stuname}[1]{\renewcommand{\@stuname}{#1}}
\newcommand{\@reportplace}{Thành phố Hồ Chí Minh}
\newcommand{\reportplace}[1]{\renewcommand{\@reportplace}{#1}}
\newcommand{\@reporttime}{\the\day\ \Month Năm\the\year}
\newcommand{\reporttime}[1]{\renewcommand{\@reporttime}{#1}}

%---------------------------------------------------------------------
% 8. \startreport - opening page setup
%---------------------------------------------------------------------
\newcommand{\startfooter}[1]{%
	\fancyfoot[L]{%
		\scriptsize
		\ttfamily
		#1
	}

	\fancyfoot[R]{%
		\scriptsize
		\ttfamily
		Trang \thepage/\pageref{LastPage}%
	}%
}
% Table of Contents
\let\originaltableofcontents\tableofcontents
\renewcommand{\tableofcontents}{%
	\begingroup
	\let\oldcontentsname\contentsname
	\renewcommand{\contentsname}{\huge\bfseries\oldcontentsname}%
	\originaltableofcontents
	\endgroup
}

% List of Figures
\let\originallistoffigures\listoffigures
\renewcommand{\listoffigures}{%
	\begingroup
	\let\oldlistfigurename\listfigurename
	\renewcommand{\listfigurename}{\huge\bfseries\oldlistfigurename}%
	\originallistoffigures
	\endgroup
}

% List of Tables
\let\originallistoftables\listoftables
\renewcommand{\listoftables}{%
	\begingroup
	\let\oldlisttablename\listtablename
	\renewcommand{\listtablename}{\huge\bfseries\oldlisttablename}%
	\originallistoftables
	\endgroup
}

\newcommand{\inlinelogo}[1]{%
	\includegraphics[height=1em]{#1}%
}

%---------------------------------------------------------------------
% 9. Source-code listing styles and helper commands
%---------------------------------------------------------------------
% Shared global style (override per snippet with lstlisting options).
% Individual languages below define their own keywords/comments/strings.
\lstset{
	basicstyle=\ttfamily\small,
	backgroundcolor=\color{white},
	frame=single,
	numbers=left,
	numberstyle=\tiny\color{gray},
	stepnumber=1,
	numbersep=10pt,
	tabsize=2,
	showspaces=false,
	showstringspaces=false,
	breaklines=true,
	breakatwhitespace=true,
	columns=flexible,
	keepspaces=true,
}

%---------------------------------------------------------------------
% 10. Cover page
%---------------------------------------------------------------------
\newcommand{\coverpage}{
	\begin{tikzpicture}[remember picture,overlay,inner sep=0,outer sep=0]
		\draw[blue!70!black,line width=4pt]
		([xshift=-1.5cm,yshift=-2cm]current page.north east) coordinate (A)--
			([xshift=1.5cm,yshift=-2cm]current page.north west) coordinate(B)--
			([xshift=1.5cm,yshift=2cm]current page.south west) coordinate (C)--
			([xshift=-1.5cm,yshift=2cm]current page.south east) coordinate(D)--cycle;

		\draw
		([yshift=0.5cm,xshift=-0.5cm]A)--
			([yshift=0.5cm,xshift=0.5cm]B)--
			([yshift=-0.5cm,xshift=0.5cm]B) --
			([yshift=-0.5cm,xshift=-0.5cm]B)--
			([yshift=0.5cm,xshift=-0.5cm]C)--
			([yshift=0.5cm,xshift=0.5cm]C)--
			([yshift=-0.5cm,xshift=0.5cm]C)--
			([yshift=-0.5cm,xshift=-0.5cm]D)--
			([yshift=0.5cm,xshift=-0.5cm]D)--
			([yshift=0.5cm,xshift=0.5cm]D)--
			([yshift=-0.5cm,xshift=0.5cm]A)--
			([yshift=-0.5cm,xshift=-0.5cm]A)--
			([yshift=0.5cm,xshift=-0.5cm]A);

		\draw
		([yshift=-0.3cm,xshift=0.3cm]A)--
			([yshift=-0.3cm,xshift=-0.3cm]B)--
			([yshift=0.3cm,xshift=-0.3cm]B) --
			([yshift=0.3cm,xshift=0.3cm]B)--
			([yshift=-0.3cm,xshift=0.3cm]C)--
			([yshift=-0.3cm,xshift=-0.3cm]C)--
			([yshift=0.3cm,xshift=-0.3cm]C)--
			([yshift=0.3cm,xshift=0.3cm]D)--
			([yshift=-0.3cm,xshift=0.3cm]D)--
			([yshift=-0.3cm,xshift=-0.3cm]D)--
			([yshift=0.3cm,xshift=-0.3cm]A)--
			([yshift=0.3cm,xshift=0.3cm]A)--
			([yshift=-0.3cm,xshift=0.3cm]A);
	\end{tikzpicture}

	\pagestyle{empty}
	\vspace*{-3\baselineskip}

	\begin{center}
		\MakeUppercase{\@upperuniname}

		\MakeUppercase{\@uniname}

		\MakeUppercase{\@deptname}
		\vfill
		\includegraphics[scale=0.45]{images/hcmut-logo.png}
		\vfill
		\bfseries
		\begin{tabular}{c}
			\LARGE\@coursename                                  \\
			{}                                                  \\
			\midrule                                            \\
			\Large\@reporttype                                  \\
			{}                                                  \\
			\Large\parbox[c]{0.9\linewidth}{\centering \@title} \\
			{}                                                  \\
			\midrule
		\end{tabular}
		\vfill
		\mdseries
		\begin{tabular}{rll}
			Giảng viên hướng dẫn: \@advisor \\
			Nhóm sinh viên: \@stuname       \\
		\end{tabular}
		\vfill

		\MakeUppercase{\@reportplace, \@reporttime}
	\end{center}

	\clearpage
	\pagestyle{fancy}
	\newpage
}

%---------------------------------------------------------------------
% 11. Headers and footers
%---------------------------------------------------------------------
\pagestyle{fancy}
\fancyhf{}

\if@twoside
	% Even pages (left side): logo on the left
	\fancyhead[EL]{%
		\bfseries
		\begin{tabular}{c l}
			$\vcenter{\hbox{\includegraphics[width=20pt]{images/hcmut-logo.png}}}$ &
			\begin{tabular}{@{}l@{}}
				\ttfamily\@uniname \\
				\ttfamily\@deptname
			\end{tabular}
		\end{tabular}%
	}

	% Odd pages (right side): logo on the right
	\fancyhead[OR]{%
		\bfseries
		\begin{tabular}{r c}
			\begin{tabular}{@{}r@{}}
				\ttfamily\@uniname \\
				\ttfamily\@deptname
			\end{tabular} &
			$\vcenter{\hbox{\includegraphics[width=20pt]{images/hcmut-logo.png}}}$
		\end{tabular}%
	}
\else
	% Single-sided: logo on the right
	\fancyhead[R]{%
		\bfseries
		\begin{tabular}{r c}
			\begin{tabular}{@{}r@{}}
				\ttfamily\@uniname \\
				\ttfamily\@deptname
			\end{tabular} &
			$\vcenter{\hbox{\includegraphics[width=20pt]{images/hcmut-logo.png}}}$
		\end{tabular}%
	}
\fi