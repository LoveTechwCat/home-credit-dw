"""Build the submission ZIP required by section 5.1 of the assignment.

    python scripts/package_submission.py            # -> submission/CO4031_HK261_HomeCredit.zip
    python scripts/package_submission.py --build    # run `npm run build` first

Layout of the archive:

    report.pdf        reports/latex/main.pdf
    slides.pdf        reports/slides/slides.pdf
    README.md         how to run the project from the archive
    data/             sample data (data/sample + data/README.md)
    src/              the whole project: SQL, Python ETL / API / DSS, React frontend (pre-built), tests, docs

Only files committed to git are packaged (plus the built frontend), so local files such as .venv, node_modules,
data/raw, the warehouse file or private notes never end up in the archive. Commit before packaging.
"""
from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/LoveTechwCat/home-credit-dw"
REPORT = "reports/latex/main.pdf"
SLIDES = "reports/slides/slides.pdf"
# Tracked files that do not belong in the submission (the assignment brief itself).
EXCLUDE = {"docs/CO4031_Assignment_HK261.pdf"}

README = f"""# Bài tập lớn CO4031 - Kho dữ liệu và hệ hỗ trợ quyết định thẩm định tín dụng (Home Credit)

| Thành phần | Vị trí |
|---|---|
| Báo cáo | `report.pdf` |
| Slide thuyết trình | `slides.pdf` |
| Mã nguồn (SQL, Python ETL/API/DSS, frontend React) | `src/` - cũng có trên GitHub: {REPO_URL} |
| Dữ liệu mẫu (5.000 hồ sơ, đủ các bảng con) | `data/sample/` |
| Link video (nếu có) | trang bìa của `report.pdf` |

## Chạy thử từ file ZIP

Cần Python 3.11+. Frontend đã được build sẵn trong `src/frontend/dist`, nên không bắt buộc cài Node.js.
Mọi lệnh chạy **trong thư mục `src/`**.

Windows (PowerShell):

```
cd src
python -m venv .venv
.venv\\Scripts\\pip install -e ".[etl,api,ml,dev]"
.venv\\Scripts\\hcdw etl --source sample --score
.venv\\Scripts\\hcdw serve
```

macOS / Linux:

```
cd src
python3 -m venv .venv
.venv/bin/pip install -e ".[etl,api,ml,dev]"
.venv/bin/hcdw etl --source sample --score
.venv/bin/hcdw serve
```

Sau đó mở http://127.0.0.1:8000. Bước `etl --score` dựng kho DuckDB từ `../data/sample`, train mô hình và chấm điểm
(khoảng 35 giây). Kiểm thử: `.venv\\Scripts\\python -m pytest -q` (Windows) hoặc `.venv/bin/python -m pytest -q`.

Chạy trên toàn bộ dữ liệu Kaggle (2,7 GB) và các lệnh khác: xem `src/README.md`.

Ghi chú: trong bản đóng gói, `src/configs/settings.yaml` trỏ `sample_dir` tới `../data/sample` (thư mục `data/` của
file ZIP); trong repo GitHub, dữ liệu mẫu nằm ở `data/sample/` của repo.
"""


def run(*cmd: str, cwd: Path = ROOT) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def tracked_files() -> dict[str, bytes]:
    """Every file of HEAD, read through `git archive` (exactly what is committed)."""
    blob = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True).stdout
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        for m in tar.getmembers():
            if m.isfile():
                out[m.name] = tar.extractfile(m).read()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "submission" / "CO4031_HK261_HomeCredit.zip")
    ap.add_argument("--build", action="store_true", help="run `npm run build` in frontend/ first")
    args = ap.parse_args()

    dirty = run("git", "status", "--porcelain", "--untracked-files=no").strip()
    if dirty:
        print("warning: uncommitted changes are NOT packaged (only HEAD is):\n" + dirty, file=sys.stderr)

    if args.build:
        subprocess.run("npm run build", cwd=ROOT / "frontend", check=True, shell=True)
    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").exists():
        sys.exit("frontend/dist is missing: run `npm run build` in frontend/ (or pass --build).")

    files = tracked_files()
    for required in (REPORT, SLIDES, "data/sample/application_train.csv.gz"):
        if required not in files:
            sys.exit(f"{required} is not committed; commit it before packaging.")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_src = 0
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("README.md", README)
        z.writestr("report.pdf", files[REPORT])
        z.writestr("slides.pdf", files[SLIDES])
        for name, data in sorted(files.items()):
            if name in EXCLUDE:
                continue
            if name.startswith("data/sample/") or name == "data/README.md":
                z.writestr(name, data)  # top-level data/ of the archive
                continue
            if name == "configs/settings.yaml":
                data = data.replace(b"sample_dir: data/sample", b"sample_dir: ../data/sample")
                assert b"../data/sample" in data, "settings.yaml no longer has `sample_dir: data/sample`"
            z.writestr(f"src/{name}", data)
            n_src += 1
        for p in sorted(dist.rglob("*")):
            if p.is_file():
                z.write(p, f"src/frontend/dist/{p.relative_to(dist).as_posix()}")
                n_src += 1

    size = args.out.stat().st_size / 1e6
    print(f"{args.out}  ({size:.1f} MB, {n_src} files under src/)")
    print(f"commit: {run('git', 'log', '-1', '--format=%h %s').strip()}")


if __name__ == "__main__":
    main()
