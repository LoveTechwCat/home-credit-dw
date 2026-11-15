# ETL mapping

Tài liệu này dùng cho Chương 2 (Data Warehouse & ETL Mapping). Số dòng và thời gian chạy thật lấy trong `data/warehouse/dq_report.json` sau khi chạy `hcdw etl --source raw`.

## Các bước
| Bước | Code | Input → Output |
|---|---|---|
| Sinh nguồn mô phỏng (tuỳ chọn, chạy trước ETL) | `hcdw synth-dates` (`src/hcdw/synth.py`) | `data/raw` → `data/synthetic/application_dates.csv` |
| Extract | `src/hcdw/etl/extract.py` + `ddl/01_staging.sql` | 8 file CSV Kaggle + nguồn tuỳ chọn `application_dates.csv` (**mô phỏng**, ở `data/synthetic/`) → `staging.*` (chỉ lấy cột cần dùng, `TRY_CAST` theo kiểu khai báo) |
| Clean | `src/hcdw/etl/clean.py` | `staging.application` → `staging.application_clean` |
| Aggregate | `transform/10_agg_credit_profile.sql`, `11_application_calendar.sql` | bảng con 1..N dòng → `staging.agg_*` (1 dòng/khách); `staging.application_dates` → `staging.application_calendar` (độ lệch ngày nộp, ngày kết quả so với ngày mốc, 1 dòng/hồ sơ) |
| Load DSS dims | `ddl/04_dss.sql` + `load.py` (`load_dss_dimensions`) | `configs/settings.yaml` (`dss`) → `dw.dim_risk_grade`, `dw.dim_rule`; tạo rỗng `dw.fact_risk_assessment`, `dw.fact_rule_hit` |
| Load dims | `transform/2x_*.sql` | staging → `dw.dim_*` |
| Load facts | `transform/3x_*.sql` | staging + dims → `dw.fact_*` |
| Marts | `marts/*.sql` | dw → `mart.*` views: `40_` view cho web, `45_` `segment_risk` và `rule_stats`, `50_` KPI |
| Quality | `src/hcdw/etl/quality.py` | dw → `dq_report.json`. 50 kiểm tra chạy ngay trong ETL; lúc này bảng DSS còn rỗng nên các kiểm tra DSS đạt mặc nhiên, và `hcdw score` chạy lại cả bộ sau khi chấm |
| Publish | `src/hcdw/etl/pipeline.py` | Mọi bước trên ghi vào `hc.duckdb.building`. Chỉ thay `hc.duckdb` khi chạy xong **và** mọi kiểm tra chất lượng đều PASS. Nếu lỗi hoặc có kiểm tra FAIL: xoá file tạm, kho đang dùng giữ nguyên, `dq_report.json` vẫn được ghi (`published: false`) và lệnh trả mã 1 |
| Train (`--score` hoặc `hcdw train`) | `src/dss/train.py` | `mart.v_application_wide` (train) → `models/model.pkl`, `oof_pd.parquet`, `metrics.json` |
| Score (`--score` hoặc `hcdw score`) | `src/dss/score.py` | kho + model → `dw.fact_risk_assessment`, `dw.fact_rule_hit` (xoá rồi ghi lại), chạy lại kiểm tra chất lượng |

## Quy tắc làm sạch (clean.py)
| # | Quy tắc | Cột |
|---|---|---|
| R1 | `DAYS_EMPLOYED = 365243` là giá trị sentinel (phần lớn là người hưu trí) → NULL, bật cờ `is_employed_anomaly` | DAYS_EMPLOYED |
| R2 | `XNA` / `Unknown` / rỗng → `'Unknown'` | các cột phân loại |
| R3 | Giới tính khác M/F → `'Unknown'` | CODE_GENDER |
| R4 | Thu nhập > quantile 99.9% → bật cờ `is_income_outlier`, dùng thu nhập đã cap khi tính các tỉ lệ | AMT_INCOME_TOTAL |
| R5 | `age_years = -DAYS_BIRTH/365.25`, `years_employed = -DAYS_EMPLOYED/365.25` | |
| R6 | Chia bucket tuổi và thu nhập theo `configs/settings.yaml` | |
| R7 | `debt_to_income = annuity/income`, `credit_to_income`, `credit_to_goods` | |
| R8 | `ext_source_mean` = trung bình các EXT_SOURCE có giá trị | EXT_SOURCE_1..3 |
| R9 | Tạo `appr_hour_band` và `population_density_band`; đổi cờ 0/1 sang boolean | |
| R10 | Hạng vùng ngoài miền 1..3 → NULL (`application_test` có một giá trị -1) | REGION_RATING_CLIENT(_W_CITY) |

## Gộp nhiều dòng (10_agg_credit_profile.sql)
| Nguồn | Gộp theo | Chỉ số |
|---|---|---|
| bureau_balance | SK_ID_BUREAU | `max_status` (STATUS 1..5 = DPD theo khoảng 30 ngày; C và X → NULL), `months_reported` |
| bureau (+ agg_bureau_balance) | SK_ID_CURR | số khoản đang active, số khoản bad debt, tổng nợ, tổng quá hạn, max ngày quá hạn, max status. "Đang quá hạn" (`has_current_overdue`) chỉ tính trên khoản **Active**: khoản đã đóng mà còn số quá hạn là dữ liệu cũ, không phải nợ hiện tại |
| previous_application | SK_ID_CURR | số hồ sơ cũ, tỉ lệ được duyệt = Approved / (Approved + Refused) |
| installments_payments | SK_ID_CURR | tỉ lệ trả trễ, số ngày trễ tối đa, số kỳ trả thiếu |
| POS_CASH_balance / credit_card_balance | SK_ID_CURR | max SK_DPD, mức sử dụng hạn mức thẻ |
| → agg_credit_profile | SK_ID_CURR | `max_dpd_days` = max các nguồn trên; `overdue_flag`; các bucket của mini-dimension; `credit_history_status` (xem dưới) |

`credit_history_status` (thuộc tính đầu của luật phân khúc `BR-SEG-01`):

```sql
CASE WHEN NOT has_bureau      THEN 'No bureau'
     WHEN has_current_overdue THEN 'Current overdue'
     ELSE 'Clean history' END
```

Đi vào `dim_credit_history` (có trong khoá `UNIQUE`) và `fact_loan_application` join theo cả cột này.

## Ánh xạ cột chính: nguồn → đích
| Nguồn (Kaggle) | Đích | Quy tắc |
|---|---|---|
| application_*.SK_ID_CURR | fact_loan_application.application_id, dim_customer.sk_id_curr | |
| application_train.TARGET | fact_loan_application.target | Tập test → NULL |
| AMT_INCOME_TOTAL | fact.amt_income, dim_customer.income_bracket | R4, R6 |
| AMT_ANNUITY / AMT_INCOME_TOTAL | fact.debt_to_income | R7 |
| DAYS_BIRTH | dim_customer.age_years, age_bucket | R5, R6 |
| DAYS_EMPLOYED | dim_customer.years_employed, is_employed_anomaly | R1, R5 |
| NAME_INCOME_TYPE | dim_customer.income_type | R2 |
| NAME_CONTRACT_TYPE | dim_contract.contract_type | R2 |
| REGION_RATING_CLIENT(_W_CITY) | dim_region.region_rating_client / _city | |
| application_dates.APPLICATION_DATE (**mô phỏng**) | fact_loan_application.application_date_key | Tra theo `dim_time`; không có nguồn → ngày mốc |
| application_dates.OUTCOME_DATE (**mô phỏng**) | fact_loan_application.outcome_date_key | Rỗng → NULL (hồ sơ mới) |
| bureau.DAYS_CREDIT | fact_bureau_credit.open_date_key | **ngày nộp hồ sơ của khách** + DAYS_CREDIT, tra theo `dim_time.day_offset` |
| bureau.DAYS_ENDDATE_FACT | fact_bureau_credit.close_date_key, credit_duration_days | ngày nộp hồ sơ + DAYS_ENDDATE_FACT; NULL nếu ngoài lịch hoặc trước ngày mở |
| bureau.CREDIT_ACTIVE / CREDIT_TYPE / CREDIT_CURRENCY | dim_bureau_category (→ fact.bureau_category_key); CREDIT_ACTIVE còn dùng cho dim_credit_history.bureau_active_bucket | NULL → `'Unknown'` |
| bureau (có / không có bản ghi) + khoản Active đang quá hạn | dim_credit_history.credit_history_status | CASE ở trên |
| `configs/settings.yaml` → `dss.grade_thresholds`, `grade_labels`, `policy`, `decision_labels` | dw.dim_risk_grade | Nạp bằng tham số bind, không nối chuỗi |
| `configs/settings.yaml` → `dss.rules` | dw.dim_rule | `sql` → `sql_condition`; `requires` → mảng |
| PD của mô hình (OOF cho train, model đầy đủ cho test) + luật | dw.fact_risk_assessment, dw.fact_rule_hit | `hcdw score`; [dss_design.md](dss_design.md) §5.3 |

## Cột DSS của `mart.v_application_wide`

| Cột | Nguồn | Ghi chú |
|---|---|---|
| `credit_history_status` | `dim_credit_history` | Luôn có giá trị |
| `pd` | `fact_risk_assessment` | PD đã hiệu chỉnh |
| `score_grade`, `rule_grade`, `final_grade` | `fact_risk_assessment` | `rule_grade` NULL = không vi phạm luật |
| `downgraded` | `fact_risk_assessment` | Hạng cuối khác hạng điểm |
| `decision`, `recommended_credit` | `fact_risk_assessment` | Chính sách của hạng cuối |

Các cột DSS lấy qua `LEFT JOIN`, nên NULL cho tới khi chạy `hcdw score`.

## Kiểm tra chất lượng
| Nhóm | Kiểm tra | Fail thì ETL báo lỗi? |
|---|---|:-:|
| Khoá | PK không trùng, không NULL (mọi bảng `dw`) · không có FK mồ côi (FK cho phép NULL thì bỏ qua NULL) | ✓ |
| Đầy đủ | số dòng fact = số dòng `staging.application` · số dòng `fact_bureau_credit` = số dòng bureau nạp được (có khách, có ngày mở) | ✓ |
| Đối soát | tổng `AMT_CREDIT` ở staging = ở fact · tổng `AMT_CREDIT_SUM` bureau ở staging = ở fact | ✓ |
| Nghiệp vụ | TARGET chỉ có ở tập train | ✓ |
| DSS | `dim_risk_grade` và `dim_rule` khớp `settings.dss` · số hồ sơ đã chấm bằng 0 (chưa chấm) hoặc bằng số hồ sơ · hạng điểm khớp PD · hạng luật là hạng xấu nhất của các luật vi phạm · hạng cuối không tốt hơn hạng điểm · chính sách khớp hạng cuối · hạng của luật khớp tỷ lệ. Bảng rỗng thì các kiểm tra này tự đạt | ✓ |
| Thông tin (`info:`) | tỉ lệ bản ghi bureau được nạp · tỉ lệ khoản đã đóng có ngày tất toán dùng được · default rate tập train | luôn pass |

Nếu có kiểm tra không đạt, ETL dừng trước bước hoán đổi: xoá file tạm `hc.duckdb.building`, giữ nguyên kho đang dùng, vẫn ghi `dq_report.json` và thoát với mã 1.

`dq_report.json` còn ghi tỉ lệ NULL của các measure trong `fact_loan_application` (`null_pct_fact_loan_application`); đây là thông tin, không phải kiểm tra.

Ngoài ra ràng buộc PK / FK / CHECK / NOT NULL trong DDL chặn dữ liệu sai ngay lúc INSERT.

## Ngày nộp hồ sơ và ngày có kết quả (nguồn mô phỏng)

`application_dates.csv` là nguồn **mô phỏng** (xem [data_dictionary.md](data_dictionary.md), mục "Nguồn mô phỏng"). Mọi xu hướng theo tháng hay quý dựng trên các ngày này **không có ý nghĩa nghiệp vụ**.

| Bước | Quy tắc |
|---|---|
| Extract | Tìm `application_dates.csv` trong thư mục nguồn, rồi trong thư mục nguồn phụ (`hcdw etl --source raw` thêm `data/synthetic/`). Không có file: ghi log, mọi hồ sơ nằm ở ngày mốc, `outcome_date_key` NULL |
| `11_application_calendar.sql` | `TRY_CAST` hai cột sang DATE, đổi thành độ lệch ngày so với ngày mốc. Dòng trùng hoặc không đọc được: lấy giá trị đọc được sớm nhất (DQ báo lỗi) |
| `20_dim_time.sql` | Lịch phủ mọi ngày nộp, ngày kết quả và ngày mở khoản bureau sớm nhất |
| `30_fact_loan_application.sql` | `application_date_key`, `outcome_date_key` tra theo `dim_time.day_offset` |
| `31_fact_bureau_credit.sql` | Ngày mở / tất toán = ngày nộp hồ sơ của khách + `DAYS_CREDIT` / `DAYS_ENDDATE_FACT` (các offset của Kaggle tính từ ngày nộp hồ sơ) |
| `40_v_application_wide.sql` | Thêm `application_date`, `application_month`, `application_quarter`, `application_year`, `outcome_date`, `months_to_outcome` |

Kiểm tra chất lượng (`quality.py`), đều PASS khi không có file:

| Check | Nội dung |
|---|---|
| `source:application_dates_one_row_per_application` | Không trùng, không có mã lạ, mọi hồ sơ có một dòng |
| `source:application_dates_parse` | Ngày nộp có và đọc được; ngày kết quả (nếu có) đọc được |
| `source:outcome_not_before_application` | Ngày kết quả không trước ngày nộp |
| `source:dates_not_after_snapshot` | Không ngày nào sau ngày mốc |
| `source:test_rows_have_no_outcome` | Hồ sơ mới (test) không có ngày kết quả |
| `dates:bureau_open_not_after_application` | Không khoản bureau nào mở sau ngày nộp hồ sơ |
| `fk:fact_loan_application.outcome_date_key->dim_time` | FK của ngày kết quả |

Có file hay không, mọi số liệu khác của kho (số dòng, tổng tiền, tỷ lệ vỡ nợ, thống kê luật) không đổi; chỉ số dòng của `dim_time` thay đổi theo khoảng lịch.
