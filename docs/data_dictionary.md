# Data dictionary - nguồn Kaggle, nguồn mô phỏng và đối tượng DSS

Mô tả đầy đủ từng cột nằm trong `HomeCredit_columns_description.csv` (tải cùng bộ dữ liệu; mã hoá latin-1).

| File | Grain | Khoá | Số dòng (full) | Dùng cho |
|---|---|---|---|---|
| application_train.csv | 1 hồ sơ vay, có TARGET | SK_ID_CURR | 307.511 | dim_customer, dim_contract, dim_region, fact_loan_application |
| application_test.csv | 1 hồ sơ vay, không có TARGET | SK_ID_CURR | 48.744 | như trên (`is_train = false`) |
| bureau.csv | 1 khoản tín dụng ở tổ chức khác | SK_ID_BUREAU → SK_ID_CURR | 1.716.428 | fact_bureau_credit, agg_bureau |
| bureau_balance.csv | 1 tháng của 1 khoản bureau | SK_ID_BUREAU, MONTHS_BALANCE | 27.299.925 | agg_bureau_balance |
| previous_application.csv | 1 hồ sơ cũ tại Home Credit | SK_ID_PREV → SK_ID_CURR | 1.670.214 | agg_previous |
| installments_payments.csv | 1 kỳ trả góp | SK_ID_PREV, NUM_INSTALMENT_NUMBER | 13.605.401 | agg_installments |
| POS_CASH_balance.csv | 1 tháng của 1 khoản POS/cash | SK_ID_PREV, MONTHS_BALANCE | 10.001.358 | agg_pos |
| credit_card_balance.csv | 1 tháng của 1 thẻ tín dụng | SK_ID_PREV, MONTHS_BALANCE | 3.840.312 | agg_cc |
| application_dates.csv (**mô phỏng**, tuỳ chọn) | 1 hồ sơ vay | SK_ID_CURR | 356.255 | fact_loan_application.application_date_key, outcome_date_key |

Số dòng của 8 file Kaggle lấy theo trang dữ liệu của Kaggle; `application_dates.csv` không có trên Kaggle, do `hcdw synth-dates` sinh với một dòng mỗi hồ sơ (train + test). Đối chiếu lại bằng `extracted_rows` trong `dq_report.json`.

## Nguồn mô phỏng: `application_dates.csv`

**Đây là dữ liệu MÔ PHỎNG, không có trong bộ Kaggle.** Bộ Kaggle không có ngày lịch thật: mọi cột thời gian chỉ là số ngày tính lùi từ ngày nộp hồ sơ. Để kho có ngày nộp hồ sơ và ngày có kết quả (phân tích theo tháng, quý), nhóm mô phỏng một **hệ thống nguồn thứ hai** ghi lại hai ngày này. File nằm riêng ở `data/synthetic/application_dates.csv` (thư mục cấu hình ở `paths.synthetic_dir`), không nằm trong `data/raw/`.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `SK_ID_CURR` | số nguyên | Mã hồ sơ, khớp `application_train/test` |
| `APPLICATION_DATE` | ngày ISO (`YYYY-MM-DD`) | Ngày nộp hồ sơ |
| `OUTCOME_DATE` | ngày ISO, có thể rỗng | Ngày ghi nhận kết quả khoản vay. Rỗng với hồ sơ mới (tập test) |

**Cách sinh** (`src/hcdw/synth.py`, tham số ở mục `synthetic_dates` của `configs/settings.yaml`; mốc là `time.anchor_date` = 2018-05-17; một tháng tính là 30 ngày):

| Nhóm hồ sơ | `APPLICATION_DATE` | `OUTCOME_DATE` |
|---|---|---|
| Train, `TARGET = 1` (vỡ nợ) | đều trong [mốc − 1080 ngày, mốc − 360 ngày] | ngày nộp + *m* tháng, *m* đều trong 1..12 (`default_after_months`) |
| Train, `TARGET = 0` | như trên | ngày nộp + 12 tháng (`repaid_observed_months`), nghĩa là "đã trả tốt tính đến ngày này" |
| Test (hồ sơ mới) | đều trong [mốc − 360 ngày, mốc] | rỗng |

- Mỗi ngày là một hàm thuần của (`seed`, `SK_ID_CURR`) qua SHA-256. Chạy lại trên bất kỳ máy nào cũng ra cùng một file, từng byte. Thêm hay bớt hồ sơ không làm đổi ngày của hồ sơ khác.
- Ngày được rải đều trong khoảng và **độc lập với mọi cột khác** (kể cả `TARGET`, ngoài việc chọn công thức ngày kết quả). Vì vậy **xu hướng theo thời gian trong kho (theo tháng, theo quý) không có ý nghĩa nghiệp vụ**: chênh lệch giữa các kỳ chỉ là nhiễu ngẫu nhiên. Không dùng chúng để kết luận về mùa vụ hay xu hướng vỡ nợ.
- Ngày có kết quả của hồ sơ train luôn không sau ngày mốc (cấu hình bị từ chối nếu vi phạm).

**Tạo lại file:** `hcdw synth-dates` (đọc `data/raw/`, ghi `data/synthetic/application_dates.csv`; khoảng 3 giây). `hcdw fixture` tự sinh file này cho dữ liệu giả, và `hcdw sample` cắt file theo cùng tập hồ sơ mẫu.

**ETL xử lý file như mọi nguồn khác:** nạp vào `staging.application_dates` (giữ dạng chuỗi để kiểm tra), kiểm tra chất lượng (một dòng mỗi hồ sơ, ngày đọc được, ngày kết quả không trước ngày nộp, không sau ngày mốc, hồ sơ mới không có kết quả). Không có file thì ETL ghi log và đặt mọi hồ sơ vào ngày mốc, ngày kết quả NULL. Code ETL không biết dữ liệu này là mô phỏng.

## Cột và bảng DSS trong kho

Các đối tượng dưới đây không đến từ Kaggle: chúng được suy ra trong ETL hoặc do `hcdw score` ghi. Thiết kế: [dss_design.md](dss_design.md) §5.

**`dw.dim_credit_history.credit_history_status`** (cũng có trong `mart.v_application_wide`)

| Giá trị | Điều kiện |
|---|---|
| `No bureau` | Không có bản ghi bureau |
| `Current overdue` | Có khoản bureau `Active` đang quá hạn |
| `Clean history` | Còn lại |

**`dw.dim_risk_grade`** (5 dòng, nạp từ `settings.dss`)

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `grade` | VARCHAR, PK | A–E |
| `grade_rank` | INTEGER | 1–5 |
| `min_rate` / `max_rate` | DOUBLE | Khoảng tỷ lệ rủi ro `[min, max)`; `max_rate` NULL với E |
| `label` | VARCHAR | Nhãn UI, ví dụ "Low risk" |
| `decision` / `decision_label` | VARCHAR | `approve` / `review` / `reject` và nhãn UI |
| `max_credit_to_income` | DOUBLE | Hạn mức tối đa (lần thu nhập) |

**`dw.dim_rule`** (6 dòng, nạp từ `settings.dss.rules`)

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `rule_code` | VARCHAR, PK | `BR-SEG-01`, `BR-RF-01..05` |
| `rule_group` | VARCHAR | `segment` hoặc `business` |
| `label` | VARCHAR | Nhãn UI |
| `sql_condition` | VARCHAR | Biểu thức trên `v_application_wide`; NULL với luật phân khúc |
| `requires` | VARCHAR[] | Cột luật cần; khi chấm một hồ sơ nhập tay (`POST /api/simulator/score`, chỉ API) mà thiếu cột thì luật được báo "không tính được" |

Ngưỡng sửa được của luật (`param` trong `configs/settings.yaml`) chỉ dùng để mô phỏng trên Playground, không bao giờ lưu vào kho: `dim_rule` không có cột này (`src/sql/ddl/04_dss.sql`).

**`dw.fact_risk_assessment`** (một dòng mỗi hồ sơ đã chấm)

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `application_id` | BIGINT, PK, FK | Hồ sơ |
| `pd` | DOUBLE | PD đã hiệu chỉnh |
| `pd_source` | VARCHAR | `oof` (train, PD out-of-fold) hoặc `model` (test) |
| `score_grade` / `rule_grade` / `final_grade` | VARCHAR, FK | Ba hạng; `rule_grade` NULL = không vi phạm |
| `grade_source` | VARCHAR | `score`, `rule` hoặc `both`: hạng nào quyết định hạng cuối |
| `downgraded` | BOOLEAN | Hạng cuối khác hạng điểm |
| `n_rule_hits` | INTEGER | Số luật vi phạm |
| `segment_key` / `segment_rate` | VARCHAR / DOUBLE | Phân khúc của hồ sơ và tỷ lệ của nó (lưu cả khi không kích hoạt) |
| `decision`, `max_credit_to_income`, `recommended_credit` | | Chính sách của hạng cuối và hạn mức khuyến nghị |
| `scored_at` | TIMESTAMP | Thời điểm chấm |

**`dw.fact_rule_hit`** (một dòng mỗi luật bị vi phạm)

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `application_id`, `rule_code` | PK, FK | Hồ sơ và luật |
| `rate` | DOUBLE | Tỷ lệ vỡ nợ train của nhóm vi phạm |
| `grade` | VARCHAR, FK | Hạng của luật |
| `segment_key` / `rate_key` | VARCHAR | Chỉ `BR-SEG-01`: phân khúc và nhóm cung cấp tỷ lệ (`*` = thuộc tính đã bỏ khi lùi cấp) |

**`mart.segment_risk`**: `segment_key`, `credit_history_status`, `age_band`, `income_group`, `region_rating_client`, `n`, `level`, `rate_key`, `rate_n`, `default_rate`, `grade`, `is_trigger`.

**`mart.rule_stats`**: `rule_code`, `rule_group`, `label`, `n_violators`, `default_rate`, `grade`.

**`mart.v_application_wide`** gồm cả: `credit_history_status`, `pd`, `score_grade`, `rule_grade`, `final_grade`, `downgraded`, `decision`, `recommended_credit` (cột DSS NULL trước khi chấm).

**View KPI** `mart.kpi_*` (`kpi_portfolio_summary`, `kpi_grade_distribution`, `kpi_model_auc`, `kpi_default_by_segment`, `kpi_default_by_credit_history`; file `src/sql/marts/50_kpi_views.sql`): định nghĩa và phạm vi ở [kpi_definitions.md](kpi_definitions.md).

## Cột ngày của hồ sơ (từ nguồn mô phỏng `application_dates.csv`)

**`dw.fact_loan_application`**: `application_date_key` (FK `dim_time`, ngày nộp hồ sơ; ngày mốc nếu không có nguồn), `outcome_date_key` (FK `dim_time`, có thể NULL: hồ sơ mới hoặc không có nguồn).

**`mart.v_application_wide`** có các cột ngày:

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `application_date` | DATE | Ngày nộp hồ sơ |
| `application_month` | VARCHAR | `YYYY-MM` |
| `application_quarter` | VARCHAR | `YYYY-Qn` |
| `application_year` | INTEGER | Năm nộp |
| `outcome_date` | DATE | Ngày có kết quả; NULL với hồ sơ mới |
| `months_to_outcome` | INTEGER | Số tháng tròn (30 ngày) từ ngày nộp tới ngày có kết quả; NULL khi chưa có kết quả |

Nhắc lại: các ngày này là **mô phỏng**, xu hướng theo thời gian không có ý nghĩa.
