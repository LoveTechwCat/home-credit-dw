# Star schema - thiết kế kho dữ liệu

> Mọi thay đổi schema phải cập nhật cả file này, `src/sql/ddl/` và `docs/etl_mapping.md`.

## 1. Bài toán & quy trình nghiệp vụ
Quy trình được mô hình hoá là **thẩm định hồ sơ vay** (loan underwriting). Câu hỏi phân tích chính:
khách hàng thuộc nhóm nào (tuổi, thu nhập, nghề, lịch sử tín dụng) có tỉ lệ vỡ nợ (default rate) cao, và một hồ sơ mới nên được duyệt, xem xét thêm hay từ chối.

## 2. Bus matrix

| Fact \ Dimension | dim_customer | dim_credit_history | dim_contract | dim_region | dim_bureau_category | dim_time |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| fact_loan_application | ✓ | ✓ | ✓ | ✓ |   | ✓ (ngày nộp hồ sơ, ngày có kết quả; mô phỏng) |
| fact_bureau_credit    | ✓ |   |   |   | ✓ | ✓ ×2 (ngày mở, ngày tất toán) |

Phần DSS (xem §7):

| Fact \ Dimension | fact_loan_application (1:1) | dim_risk_grade | dim_rule |
|---|:-:|:-:|:-:|
| fact_risk_assessment | ✓ | ✓ ×3 (hạng điểm, hạng luật, hạng cuối) |   |
| fact_rule_hit        | qua fact_risk_assessment | ✓ (hạng của luật) | ✓ |

Hai fact dùng chung `dim_customer` và `dim_time` (conformed dimension), tạo thành một **fact constellation** và cho phép phân tích drill-across.
Ví dụ: `GET /api/bureau/kpis?age_bucket=25-34` (chỉ API) lọc khách theo thuộc tính của hồ sơ vay, rồi đếm khoản vay bureau của đúng nhóm khách đó qua `customer_key`.

## 3. Grain
- `fact_loan_application`: **một hồ sơ vay** (`SK_ID_CURR`). Gồm cả tập train (có TARGET) và tập test (TARGET = NULL, `is_train = false`; DSS dùng để chấm điểm).
- `fact_bureau_credit`: **một khoản tín dụng** do Credit Bureau báo cáo (`SK_ID_BUREAU`).
- `fact_risk_assessment`: **một hồ sơ đã chấm** (1:1 với `fact_loan_application`).
- `fact_rule_hit`: **một luật bị vi phạm của một hồ sơ**.

## 4. Giả định về thời gian
Bộ dữ liệu **không có ngày thật**. Mọi cột thời gian là số ngày hoặc số tháng tính lùi từ ngày nộp hồ sơ (`DAYS_BIRTH`, `DAYS_CREDIT`, `MONTHS_BALANCE`). Nhóm cố định một **ngày snapshot giả định** `time.anchor_date` (mặc định 2018-05-17) trong `configs/settings.yaml`, và tính ngày = snapshot + offset.
**Ngày nộp hồ sơ và ngày có kết quả là dữ liệu MÔ PHỎNG.** Chúng đến từ một hệ thống nguồn mô phỏng, file `data/synthetic/application_dates.csv` (sinh bằng `hcdw synth-dates`; cách sinh ở [data_dictionary.md](data_dictionary.md)):
- hồ sơ train nộp rải đều trong [mốc − 1080 ngày, mốc − 360 ngày]; hồ sơ test (hồ sơ mới) trong [mốc − 360 ngày, mốc];
- ngày có kết quả: hồ sơ vỡ nợ = ngày nộp + 1..12 tháng (ngẫu nhiên đều), hồ sơ trả tốt = ngày nộp + 12 tháng; hồ sơ test không có;
- mỗi ngày là hàm tất định của (`seed`, `SK_ID_CURR`), độc lập với mọi cột khác, nên **xu hướng theo thời gian không có ý nghĩa nghiệp vụ**.

`dim_time` đóng hai vai trò trên `fact_loan_application`: `application_date_key` (luôn có) và `outcome_date_key` (NULL với hồ sơ mới). Không có file nguồn thì ETL đặt mọi hồ sơ vào ngày snapshot và `outcome_date_key` NULL. **Phải ghi rõ cả hai giả định (ngày snapshot và ngày mô phỏng) trong Chương 2 của báo cáo.**

`dim_time` cũng là **role-playing dimension** trên `fact_bureau_credit`: `open_date_key` = ngày nộp hồ sơ của khách + `DAYS_CREDIT` (luôn có), `close_date_key` = ngày nộp hồ sơ + `DAYS_ENDDATE_FACT` (chỉ khoản đã tất toán). Các offset của Kaggle tính từ ngày nộp hồ sơ, nên ngày bureau dịch theo ngày nộp của từng khách. Nguồn có vài giá trị `DAYS_ENDDATE_FACT` vô lý (hơn 100 năm trước, hoặc trước ngày mở), nên ETL để `close_date_key = NULL` cho các dòng đó thay vì kéo dài lịch. Khoá ngày được tra theo `dim_time.day_offset`, nên FK luôn trỏ tới một dòng có thật.

## 5. ERD

![Star schema (fact constellation)](diagrams/03_star_schema.svg)

File: [`docs/diagrams/03_star_schema.svg`](diagrams/03_star_schema.svg). Toàn bộ sơ đồ thiết kế: [architecture.md](architecture.md).

## 6. Quyết định thiết kế
| Quyết định | Lý do |
|---|---|
| Star schema chứ không dùng snowflake | Truy vấn dashboard chỉ cần 1 lần join mỗi chiều. Số dimension ít nên không cần chuẩn hoá thêm |
| `dim_credit_history` là **mini-dimension** (các thuộc tính đã chia bucket) | Lịch sử tín dụng gốc có tới hàng chục triệu dòng. Gộp thành vài trăm "hồ sơ" giúp lọc và nhóm nhanh, và không làm phình `dim_customer` |
| `dim_bureau_category` là **junk dimension** (trạng thái × loại tín dụng × tiền tệ, vài chục dòng) | Fact chỉ còn khoá và measure, không lặp lại chuỗi văn bản 1,7 triệu lần. Slice theo trạng thái hay loại vẫn chỉ cần một join |
| `dim_time` đóng hai vai trò trên mỗi fact: ngày nộp / ngày có kết quả (`fact_loan_application`), ngày mở / ngày tất toán (`fact_bureau_credit`) | Dùng chung một bảng lịch, không phải tạo bảng ngày thứ hai. Ngày tất toán không dùng được thì để NULL (FK cho phép NULL) |
| Measure chi tiết (`max_dpd_days`, `prev_approval_rate`...) đặt trên fact | Bucket nằm ở dimension để slice; số liệu chính xác nằm ở fact để tính toán và cho model |
| Surrogate key bằng `ROW_NUMBER()` | Có thể tạo lại y hệt mỗi lần full refresh (dữ liệu là snapshot tĩnh) |
| SCD type 1 | Chỉ có 1 snapshot nên không có lịch sử thay đổi để lưu |
| Full refresh thay vì incremental | Nguồn là file tĩnh. Chạy lại toàn bộ chỉ mất khoảng 15 giây với DuckDB |
| Build vào file tạm `hc.duckdb.building`, chỉ thay file đang dùng khi mọi bước xong và mọi kiểm tra chất lượng đạt | Lần chạy lỗi hoặc dữ liệu không đạt kiểm tra không làm hỏng kho đang dùng: file tạm bị xoá, lệnh thoát với mã 1 |
| Ràng buộc PK / FK / CHECK / NOT NULL | Kiểm tra toàn vẹn ngay lúc load. `quality.py` kiểm tra lại và xuất báo cáo |

## 7. Phần DSS: hạng A–E và luật

DDL: `src/sql/ddl/04_dss.sql`. Thiết kế đầy đủ: [dss_design.md](dss_design.md) §5.

| Bảng | Loại | Grain / khoá | Nạp bởi | Cột chính |
|---|---|---|---|---|
| `dw.dim_risk_grade` | dimension (5 dòng) | `grade` A–E | `load.py`, từ `settings.dss` | `grade_rank`, `min_rate` (tính cận dưới), `max_rate` (NULL với E), `label`, `decision`, `decision_label`, `max_credit_to_income` |
| `dw.dim_rule` | dimension (6 dòng) | `rule_code` | `load.py`, từ `settings.dss.rules` | `rule_group` (`segment`/`business`), `label`, `sql_condition` (NULL với luật phân khúc), `requires` (mảng cột cần) |
| `dw.fact_risk_assessment` | fact | `application_id` (FK tới `fact_loan_application`) | `hcdw score` | `pd`, `pd_source` (`oof`/`model`), `score_grade`, `rule_grade` (NULL = không vi phạm), `final_grade`, `grade_source`, `downgraded`, `n_rule_hits`, `segment_key`, `segment_rate`, `decision`, `max_credit_to_income`, `recommended_credit`, `scored_at` |
| `dw.fact_rule_hit` | fact | (`application_id`, `rule_code`) | `hcdw score` | `rate`, `grade`, `segment_key`, `rate_key` (hai cột cuối chỉ cho `BR-SEG-01`) |

Cột DSS trên dimension của star schema chính:
- `dw.dim_credit_history` có `credit_history_status` (`No bureau` / `Clean history` / `Current overdue`), có trong khoá `UNIQUE`.

Ràng buộc đáng chú ý:
- Hạng là một chữ cái, nên so sánh chuỗi `'A' < 'E'` cũng là so mức rủi ro. `CHECK (final_grade >= score_grade)`: hạng cuối không bao giờ tốt hơn hạng điểm.
- `CHECK (downgraded = (final_grade <> score_grade))` và `CHECK ((rule_grade IS NULL) = (n_rule_hits = 0))`.
- Sau `hcdw etl`, hai bảng fact DSS **rỗng**; `hcdw score` xoá và ghi lại toàn bộ.

| Quyết định | Lý do |
|---|---|
| Kết quả chấm điểm là fact riêng, không thêm cột vào `fact_loan_application` | ETL và chấm điểm chạy độc lập; chấm lại không phải nạp lại kho |
| Hạng và luật là dimension nạp từ config | Đổi ngưỡng hoặc chính sách chỉ sửa `configs/settings.yaml`; SQL và code không viết cứng |
| `segment_rate` lưu cả khi luật phân khúc không kích hoạt | Mô phỏng chính sách xếp hạng lại phân khúc theo ngưỡng mới mà không chạy lại mô hình |

**Mart của DSS** (`src/sql/marts/45_dss_views.sql`, chỉ tính trên khoản vay lịch sử, tức tập train):
- `mart.segment_risk`: một dòng cho mỗi tổ hợp `credit_history_status × age_band × income_group × region_rating_client` (225 dòng). Phân khúc dưới 500 hồ sơ lấy tỷ lệ của cấp cha (`level`, `rate_key`).
- `mart.rule_stats`: số hồ sơ vi phạm, tỷ lệ vỡ nợ và hạng của mỗi luật.

`mart.v_application_wide` có `credit_history_status` và kết quả chấm điểm qua `LEFT JOIN` (NULL trước khi chấm). Danh sách cột: [etl_mapping.md](etl_mapping.md).

**View KPI** (`src/sql/marts/50_kpi_views.sql`): `mart.kpi_portfolio_summary`, `mart.kpi_grade_distribution`, `mart.kpi_model_auc`, `mart.kpi_default_by_segment`, `mart.kpi_default_by_credit_history`. Định nghĩa và phạm vi (mọi hồ sơ hay chỉ khoản vay lịch sử): [kpi_definitions.md](kpi_definitions.md).
