# Lịch sử phiên bản

Ghi lại các thay đổi đáng nhớ, mới nhất trước. Tài liệu chính thức chỉ mô tả phiên bản hiện tại.

## 15/11 — phiên bản hiện tại

**Phạm vi KPI trên web**
- Trước: KPI của Overview, Playground và `/api/grades` chỉ tính trên khoản vay lịch sử (tập train).
- Nay: mọi số tổng hợp tính trên mọi hồ sơ đã chấm. Tỷ lệ vỡ nợ, tỷ lệ của luật và AUC vẫn chỉ tính trên khoản vay lịch sử.
- `/api/grades` đổi mặc định từ `scope=train` sang `scope=all`.
- `POST /api/policy/simulate` chạy trên mọi hồ sơ. Response thêm `n_historical`, `transitions[].n_historical` và `rules[].n_violators_all`.
- Định nghĩa "danh mục" trong business_requirements.md đổi từ "khoản vay lịch sử" sang "mọi hồ sơ (đã chấm)".

**Giao diện**
- Trang OLAP (gồm tab Drill-down và Saved queries) gộp vào trang Analytics. Analytics gồm hai tab Cube (`POST /api/analytics/cube`) và SQL queries.
- Bỏ trạng thái lọc dùng chung giữa các trang. Cube không cache; các truy vấn khác của API (Overview, Grades, Rules, SQL queries, Explorer `book`) được nhớ bằng `lru_cache`.
- Overview thêm ô waffle "Out of every 100 applications · Historical loans".
- Playground: bảng Policy impact và bảng By grade thu gọn được thay bằng 5 ô KPI chính, Decision mix kèm Who moves, bảng Policy by grade và thẻ Rules. Thẻ Rules bật/tắt được từng luật và đổi ngưỡng của luật có `param`.
- Grades & model thêm nút chọn phạm vi All applications / Historical loans / New applications.
- Bỏ các trang Segments, Credit history, Score an applicant, và phần what-if trên Application details. Các endpoint what-if và simulator chỉ còn dùng qua API.
- Đường dẫn cũ chuyển hướng như sau:
  - `/segments?dim=X` → `/?dim=X`;
  - `/portfolio` → `/`;
  - `/credit-history` → `/?dim=credit_history_status`;
  - `/olap`, `/drilldown` → `/analytics`;
  - `/policy` → `/playground`;
  - `/assessment`, `/simulator` → `/applications-details`.

**Dữ liệu**
- Thêm nguồn mô phỏng `data/synthetic/application_dates.csv` (`hcdw synth-dates`). Nguồn này cho ngày nộp hồ sơ và ngày có kết quả.
- Khi có nguồn này, `dim_time` làm role-playing dimension trên cả fact hồ sơ vay.
- `data/sample/` được tạo lại và có thêm `application_dates.csv.gz`.

**Số liệu đo lại**

| Hạng mục | Trước | Nay |
|---|---|---|
| `hcdw etl` | khoảng 40 giây | khoảng 15 giây (13,6 giây) |
| `hcdw etl --score` | 81 giây | khoảng 1,5 phút (86 giây) |
| Dung lượng CSV | ghi 2,6 GB | 2,7 GB |
| Số kiểm tra chất lượng | 25 | 50 |
| Số trường lọc của Explorer | 44 | 45 |
| `POST /api/policy/simulate` | 27–35 ms (lần đầu 140 ms); 42–50 ms | đọc dữ liệu một lần khoảng 0,5 giây, sau đó khoảng 35–45 ms mỗi lần |
| Truy vấn OLAP | 50–200 ms lần đầu, dưới 5 ms khi có cache | cube khoảng 15–60 ms, không cache |
| Python (ghi trong tài liệu) | 3.12 | 3.11+ (theo `pyproject.toml`) |

- Số của dss_design.md §9.5 trước đây tính trên tập train: Duyệt mặc định 52,07%, Approve C 74,9%, Loosen 59,3%, Tighten 29,6%.
- Nay §9.5 tính trên mọi hồ sơ: 51,86% / 74,90% / 59,10% / 29,31%.
- Các số trên khoản vay lịch sử đo lại và không đổi so với 14/11.

## 14/11 — chốt thiết kế DSS

**Quyết định của người dùng**
- Giữ luật BR-RF-05. Bỏ luật DTI.
- Hạn mức A/B/C/D/E = 8/6/4/3/0 lần thu nhập, là giá trị ban đầu.
- Mọi chữ hiển thị trên web viết tiếng Anh. Quyết định này thay cho dòng "nhãn và chữ hiển thị trên giao diện viết tiếng Việt" trong plan.
- Simulator chỉ bắt buộc `amt_income` và `amt_credit`.
- Mốc QA cho `credit_history_status` lấy theo định nghĩa của kho: No bureau 10,12%, Clean history 7,62%, Current overdue 16,11%. Mốc này thay cho con số 15,90% trong plan và data_profiling.md.

**Ước tính trước khi có mô hình (thang tạm theo `ext_source_mean`)**
- Duyệt 48,0%, Thẩm định thêm 45,4%, Từ chối 6,5%.
- Vỡ nợ nhóm duyệt 3,59%; 15,7% hồ sơ bị hạ hạng.
- PD thật sau đó cho tỷ lệ duyệt cao hơn và nhóm duyệt sạch hơn (dss_design.md §9.3).

**Thay đổi giao diện code so với plan (chỉ thêm, không đổi tên hay bỏ)**
- `segment_risk` thêm `rate_key`, `rate_n`, `is_trigger`.
- `fact_risk_assessment` và `fact_rule_hit` thêm cột.
- `v_application_wide` thêm `downgraded`.
- `rules.load_catalog` đọc thêm `mart.rule_stats`.
- `model.pkl` thêm `features` và `fold_models`.
- `oof_pd.parquet` thêm `fold` và `pd_raw`.
- `predict.score` và `predict.explain` thêm tham số `folds`.
- `decision` dùng mã `approve|review|reject`, kèm `decision_label`.

**Điểm triển khai khác bản thiết kế Stage 1**
- Kiểm tra config chuyển từ `load_policy`/`load.py` sang `hcdw.config.validate_dss`.
- Ngưỡng cắt thu nhập 900.000 không còn viết cứng; giá trị được đọc từ kho lúc chạy.
- Nhãn `*` của phân khúc hiển thị thành `Any`.
- Hạng luật NULL hiển thị thành `No rule hit` trong các API phân tích.
- Thêm kiểm tra điểm lệch mô hình (`trained_at`) sau khi train lại.

**Các stage**

| Stage | Nội dung |
|---|---|
| 1 | Thiết kế DSS |
| 2 | Config, ETL, view DSS |
| 3 | Mô hình |
| 4 | API |
| 5 | QA |

## 10/11 — chốt star schema

- Bản nháp Day 1 được rà soát: thêm `dim_bureau_category` và `close_date_key`.
- Cả nhóm review và chốt (freeze) lúc 21:00.
