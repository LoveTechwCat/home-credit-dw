# Home Credit Data Warehouse & DSS (CO4031 - HK261)

Kho dữ liệu (star schema) + ETL + OLAP + web dashboard + DSS xếp hạng tín dụng A–E trên bộ
[Home Credit Default Risk](https://www.kaggle.com/competitions/home-credit-default-risk/data).

## Kiến trúc

```
Kaggle CSV (data/raw | data/sample)  +  data/synthetic/application_dates.csv (mô phỏng, hcdw synth-dates)
   │  hcdw etl: extract → clean → aggregate → load → quality   (Python + DuckDB SQL)
   ▼
DuckDB hc.duckdb:  staging.*  →  dw.dim_* + dw.fact_*  →  mart.* (views)
   │  hcdw train: LightGBM 5-fold OOF + isotonic → models/
   │  hcdw score: PD → hạng điểm, luật → hạng luật, hạng cuối + chính sách → dw.fact_risk_assessment
   ▼  read-only SQL
FastAPI (src/hcdw/api)  ──  query layer + cube + explorer + DSS (src/dss)   hcdw serve  → /api/*
   ▼  HTTP JSON
React + TypeScript + Tailwind + Recharts (frontend/)                            → /
```

Sơ đồ chi tiết (kiến trúc hệ thống, ETL, star schema, data lineage, luồng truy vấn, DSS, phép OLAP trên Analytics, phạm vi KPI) nằm trong
[docs/architecture.md](docs/architecture.md). Mỗi sơ đồ là một file SVG vẽ tay trong `docs/diagrams/`; trang **Architecture** của web app hiển thị đúng các file đó.

Kho dữ liệu là **DuckDB**: chạy local bằng file `data/warehouse/hc.duckdb`, hoặc trên
**MotherDuck** (DuckDB cloud) để cả nhóm dùng chung. Chọn nơi dùng bằng biến `DW_TARGET` trong `.env`.

## Bắt đầu nhanh

Cần Python 3.11+ và Node.js 20+. Các bước chạy theo thứ tự, từ thư mục gốc của repo.

**1. Cài đặt**

| | Windows (PowerShell) | macOS / Linux |
|---|---|---|
| Môi trường Python | `python -m venv .venv` | `uv sync --all-extras` (bỏ qua dòng dưới) |
| Gói Python | `.venv\Scripts\pip install -e ".[etl,api,ml,dev]"` | |
| Giao diện web | `cd frontend; npm install; npm run build; cd ..` | `cd frontend && npm install && npm run build && cd ..` |
| Cấu hình | `copy .env.example .env` | `cp .env.example .env` |

Sau bước này lệnh `hcdw` nằm ở `.venv\Scripts\hcdw` (Windows) hoặc `.venv/bin/hcdw` (macOS/Linux); kích hoạt môi trường (`.venv\Scripts\activate` hoặc `source .venv/bin/activate`) để gõ thẳng `hcdw`.

**2. Dựng kho và chấm điểm.** Chọn một trong hai nguồn dữ liệu:

| Nguồn | Lệnh | Thời gian |
|---|---|---|
| Dữ liệu mẫu có sẵn trong repo (khoảng 5.000 khách, không cần tài khoản Kaggle) | `hcdw etl --source sample --score` | khoảng 35 giây |
| Dữ liệu thật | `hcdw download` → `hcdw synth-dates` → `hcdw etl --source raw --score` | khoảng 1,5 phút cho bước cuối |

`hcdw download` cần đã tham gia cuộc thi trên Kaggle và có `KAGGLE_API_TOKEN` trong `.env` (hoặc `~/.kaggle/kaggle.json`). `hcdw synth-dates` sinh nguồn ngày mô phỏng; dữ liệu mẫu đã kèm sẵn file này.

**3. Mở web app:** `hcdw serve`, rồi vào http://127.0.0.1:8000.

**4. Kiểm thử (tùy chọn):** `pytest -q` (macOS: `.venv/bin/python -m pytest -q`).

Báo cáo và slide build riêng: xem [reports/latex](reports/latex) (`latexmk -pdf -shell-escape main.tex`) và [reports/slides/README.md](reports/slides/README.md) (`latexmk -xelatex slides.tex`).

## Các lệnh hcdw

| Lệnh | Việc làm |
|---|---|
| `hcdw download` | Tải dữ liệu Kaggle về `data/raw/`. Trước đó phải join cuộc thi trên Kaggle và đặt `KAGGLE_API_TOKEN` trong `.env` (hoặc `~/.kaggle/kaggle.json`) |
| `hcdw fixture` | Sinh dữ liệu giả nhỏ vào `data/fixture/` để thử pipeline khi chưa tải dữ liệu thật (kèm `application_dates.csv`) |
| `hcdw synth-dates` | Sinh nguồn **mô phỏng** `data/synthetic/application_dates.csv` (ngày nộp hồ sơ, ngày có kết quả) từ `data/raw/`. Tất định, không có xu hướng; chạy trước `hcdw etl`. Không có file thì mọi hồ sơ nằm ở ngày mốc. Chi tiết: [docs/data_dictionary.md](docs/data_dictionary.md) |
| `hcdw etl --source raw` | Chạy ETL đầy đủ (khoảng 15 giây trên dữ liệu thật). `--source` nhận `raw`, `sample`, `fixture` hoặc một thư mục |
| `hcdw etl --score` | ETL rồi train và chấm điểm luôn (khoảng 1,5 phút trên dữ liệu thật). Cần `.[ml]` |
| `hcdw train` | Train mô hình DSS (LightGBM 5-fold + hiệu chỉnh isotonic) vào `models/`. In AUC và các yếu tố SHAP lớn nhất |
| `hcdw score` | Chấm mọi hồ sơ (PD, hạng A–E, chính sách) vào `dw.fact_risk_assessment` và `dw.fact_rule_hit`. Chạy lại sau mỗi lần `hcdw train` |
| `hcdw sample --n 5000` | Tạo `data/sample/` (dữ liệu mẫu để nộp bài) từ `data/raw/` |
| `hcdw dq` | Chạy lại các bước kiểm tra chất lượng dữ liệu |
| `hcdw olap [01]` | Chạy các truy vấn OLAP mẫu (ROLLUP, CUBE, drill-down, slice/dice, PIVOT, RANK) |
| `hcdw serve` | Mở web app tại http://127.0.0.1:8000 (API + dashboard đã build). Tài liệu API: `/docs` |
| `hcdw push` | Đẩy kho local lên MotherDuck (cần `MOTHERDUCK_TOKEN`) |
| `hcdw info` | Xem kho đang dùng và số dòng của từng bảng |
| `hcdw ui` | Mở DuckDB UI (giao diện SQL trên trình duyệt) tại http://localhost:4213, kho gắn ở chế độ chỉ đọc (catalog `hc`) |
| `python scripts/capture_figures.py` | Chụp ảnh các trang và sơ đồ vào `reports/figures/` (cần `hcdw serve` đang chạy) |

Lưu ý: với file DuckDB local, chỉ một tiến trình được ghi vào kho. Hãy **tắt `hcdw serve` trước khi chạy `hcdw etl` hoặc `hcdw score`**
(nếu quên, lệnh sẽ báo lỗi rõ ràng và kho đang dùng vẫn giữ nguyên). Sau `hcdw train`, chạy `hcdw score` trước khi mở lại web; nếu không, trang Application details báo mô hình mới hơn điểm đã lưu và yêu cầu chạy `hcdw score`.

Các trang web (trang DSS cần chạy `hcdw etl --score` trước `hcdw serve`):

| Nhóm | Trang | Việc làm |
|---|---|---|
| Menu chính | Overview `/` | Ô waffle "Out of every 100 applications" (khoản vay lịch sử theo quyết định × kết quả; bấm một ô để mở Explorer); 5 KPI trên mọi hồ sơ đã chấm, tỷ lệ vỡ nợ chỉ trên khoản vay lịch sử (không theo bộ lọc); phân tích nhóm khách theo một thuộc tính tự chọn (Group by); bấm một cột để mở Explorer với nhóm đó |
| Menu chính | Analytics `/analytics` | Tab **Cube**: thanh lọc dạng chip (slice/dice), Interval Year / Quarter / Month cho các biểu đồ theo thời gian, 4 biểu đồ cố định, bảng Cube với ▲/▼ (roll-up / drill-down theo năm ↔ quý ↔ tháng), × bỏ chiều, + thêm chiều, ⇄ pivot, chọn measure; bấm một ô hoặc một cột để mở Explorer. Trạng thái lưu trên URL (`?state=`). Tab **SQL queries**: 6 truy vấn OLAP mẫu |
| Menu chính | Explorer `/explore` | Lọc một nhóm hồ sơ bằng điều kiện dạng chip (trường · toán tử · giá trị, lưu trên URL); so với mọi hồ sơ: quy mô, tỷ lệ quyết định, rủi ro còn lại, rủi ro đã chặn, kết quả của nhóm thẩm định thêm; danh sách hồ sơ phân trang, chọn cột, bấm dòng để mở Application details |
| Menu chính | Application details `/applications-details` | Tra một hồ sơ: ba hạng, đề xuất, hạn mức, SHAP, luật vi phạm, thông tin khách |
| Menu chính | Playground `/playground` | Mô phỏng chính sách: preset (Approve C, Loosen, Tighten, Rules off) và Reset; 5 KPI hiện hành → mô phỏng; Decision mix kèm Who moves; bảng Policy by grade (ngưỡng PD, quyết định, hạn mức); thẻ Rules (bật/tắt từng luật, đổi ngưỡng của luật có tham số) |
| Tham khảo (chân sidebar) | Grades & model `/grades` | Chọn phạm vi All applications / Historical loans / New applications (mặc định All) và loại hạng Score / Rule / Final; thang hạng, yếu tố ảnh hưởng điểm, PD so với vỡ nợ thực tế |
| Tham khảo (chân sidebar) | Architecture `/architecture` | Sơ đồ thiết kế |


API chính (tài liệu đầy đủ ở `/docs` khi chạy `hcdw serve`; hợp đồng DSS ở [docs/dss_design.md](docs/dss_design.md) §7):

| API | Dùng cho |
|---|---|
| `GET /api/kpis/summary` | 5 KPI của Overview |
| `POST /api/analytics/cube` | Khối OLAP của trang Analytics (slice/dice, roll-up/drill-down, pivot) |
| `GET /api/by/{dim}`, `GET /api/olap/*` | Phân tích theo chiều (Overview); truy vấn SQL mẫu (Analytics, tab SQL queries) |
| `GET /api/meta` | Danh sách chiều, measure, giá trị của từng chiều, phân cấp thời gian (mọi trang) |
| `GET /api/explore/fields`, `POST /api/explore/metrics`, `POST /api/explore/applications` | Explorer: danh sách trường lọc, chỉ số nhóm so với mọi hồ sơ, danh sách hồ sơ. `POST /api/explore/metrics` cũng cấp số cho ô waffle của Overview |
| `GET /api/assessment/{id}` | Application details |
| `GET /api/grades`, `GET /api/explain/global` | Grades & model; `/api/grades` còn dùng cho Playground |
| `GET /api/rules`, `POST /api/policy/simulate` | Playground |
| `POST /api/assessment/what-if`, `/api/simulator/*` | Chỉ API: what-if một hồ sơ, chấm hồ sơ nhập tay |
| `GET /api/pivot`, `GET /api/kpis`, `GET /api/cross`, `/api/hierarchy`, `/api/bureau/*` | Chỉ API |

### Phát triển giao diện
Chạy hai terminal: `hcdw serve --reload` (API ở cổng 8000) và `cd frontend && npm run dev` (giao diện ở cổng 5173, tự chuyển `/api` sang cổng 8000).

### Dùng chung kho qua MotherDuck
1. Chạy `hcdw etl --source raw --push` (hoặc chạy `hcdw push` sau khi ETL xong).
2. Máy dùng chung đặt `DW_TARGET=motherduck` và `MOTHERDUCK_TOKEN=...` trong `.env`. Web app và `hcdw olap` sẽ đọc thẳng từ cloud, không cần tải 2,7 GB dữ liệu. `hcdw train` và `hcdw score` chỉ chạy trên kho local, nên chạy `hcdw etl --score --push` để đẩy cả điểm lên cloud.

## Cấu trúc thư mục

| Thư mục | Nội dung |
|---|---|
| `src/sql/ddl/` | Schema `staging` / `dw` / `mart`, DDL star schema có ràng buộc |
| `src/sql/transform/` | `1x` gộp nhiều dòng, `2x` dimension, `3x` fact |
| `src/sql/marts/` | View cho web app (`40_`), luật DSS (`45_`) và KPI (`50_`) |
| `src/sql/olap/` | Truy vấn OLAP mẫu để đưa vào báo cáo |
| `src/hcdw/` | Package Python: config, kết nối DB, ETL, CLI, sample, fixture |
| `src/hcdw/api/` | FastAPI: toàn bộ SQL của web app (`queries.py`) và các endpoint (`app.py`) |
| `frontend/` | React + Tailwind: các trang ở bảng trên (menu chính và hai trang tham khảo) |
| `src/dss/` | Features, train, predict (PD + SHAP), rules, grading (A–E, chính sách), score, policy (mô phỏng) |
| `models/` | `model.pkl` và `oof_pd.parquet` (không commit, sinh lại bằng `hcdw train`), `metrics.json` |
| `docs/` | `business_requirements.md`, `dss_design.md`, `star_schema.md`, `etl_mapping.md`, `architecture.md`, `diagrams/`, `kpi_definitions.md`, `data_dictionary.md`, `data_profiling.md`, `CHANGELOG.md` (lịch sử phiên bản) |
| `reports/` | LaTeX báo cáo, hình, slide |
| `notebooks/` | EDA và thử nghiệm |
| `tests/` | pytest trên dữ liệu fixture (ETL, API, DSS) |
| `data/` | `raw/`, `warehouse/` và `synthetic/` không commit (`synthetic/` sinh lại bằng `hcdw synth-dates`) · `sample/` được commit để nộp bài |


## Kiểm thử

```bash
pytest                          # Windows (venv đã kích hoạt)
.venv/bin/python -m pytest -q   # macOS
```

```bash
cd frontend && npm run typecheck
```

## Nộp bài (đúng mục 5.1 của đề)
`report.pdf` (lấy từ `reports/latex`) · `src/` + `frontend/` (không gồm `node_modules`) · `data/` (chỉ `sample/` và README) · `slides.pdf` · link video đặt ở trang bìa báo cáo.

Tạo file ZIP nộp bài (sau khi đã commit báo cáo và slide mới nhất, và đã `npm run build`):

```bash
python scripts/package_submission.py
```

Kết quả: `submission/CO4031_2311186_2310853_2310381.zip` gồm báo cáo `CO4031_2311186_2310853_2310381.pdf`, `slides.pdf`, `README.md` (cách chạy), `data/sample/`
và `src/` chứa toàn bộ project kèm frontend đã build sẵn. Trong ZIP, chạy từ `src/` theo README đi kèm
(`hcdw etl --source sample --score` rồi `hcdw serve`); chỉ những file đã commit mới được đóng gói.
