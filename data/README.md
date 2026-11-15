# data/

| Thư mục | Nội dung | Commit? |
|---|---|---|
| `raw/` | 10 file CSV của Kaggle, tải và giải nén bằng `hcdw download`: 8 bảng dữ liệu (khoảng 2,7 GB, 58,5 triệu dòng), file mô tả cột `HomeCredit_columns_description.csv` và `sample_submission.csv` (ETL không dùng) | Không |
| `synthetic/` | `application_dates.csv`: nguồn **mô phỏng** (ngày nộp hồ sơ, ngày có kết quả; 356.255 dòng, mỗi hồ sơ một dòng), sinh bằng `hcdw synth-dates`. `hcdw etl --source raw` đọc file ở đây | Không (sinh lại bằng `hcdw synth-dates`) |
| `sample/` | Tập con khoảng 5.000 khách (phân tầng theo TARGET) cùng mọi dòng con liên quan, giữ nguyên tên file và tên cột của Kaggle (8 bảng dạng `.csv.gz` + file mô tả cột), kèm `application_dates.csv.gz` cắt từ `synthetic/` cho đúng các hồ sơ trong mẫu. Tạo bằng `hcdw sample` | **Có** (đây là dữ liệu mẫu để nộp bài) |
| `fixture/` | Dữ liệu giả để test, tạo bằng `hcdw fixture` | Không |
| `warehouse/` | `hc.duckdb` (kho dữ liệu) + `dq_report.json` | Không |

Nguồn dữ liệu: https://www.kaggle.com/competitions/home-credit-default-risk/data (dùng theo luật của cuộc thi, chỉ cho mục đích học tập).

Chạy lại toàn bộ trên dữ liệu mẫu:

```bash
hcdw etl --source sample
```
