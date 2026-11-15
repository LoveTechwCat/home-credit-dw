# KPI definitions

Định nghĩa 5 KPI chính (KPI-01..05) và 5 chỉ số phụ (SM-1..5) theo [business_requirements.md](business_requirements.md) §6. Mỗi KPI ghi công thức, cột nguồn, ý nghĩa và view SQL tương ứng trong `src/sql/marts/50_kpi_views.sql` (riêng tỷ lệ của luật nằm ở `src/sql/marts/45_dss_views.sql`).

**Quy ước chung.**
- **Phạm vi trên web:** mọi số tổng hợp trên web tính trên **mọi hồ sơ (đã chấm)** (khoản vay lịch sử + hồ sơ mới). Chỉ số cần kết quả trả nợ chỉ tính được trên khoản vay lịch sử (`is_train`). Sơ đồ: [architecture.md](architecture.md) §8.

  | Tính trên mọi hồ sơ đã chấm | Chỉ tính trên khoản vay lịch sử (có kết quả) |
  |---|---|
  | Số hồ sơ; KPI-02 dư nợ cấp mới; KPI-03 dư nợ chịu rủi ro (Σ `amt_credit` × PD, hồ sơ mới cũng có PD); KPI-04, KPI-05 và tỷ lệ từ chối; tiền vay được duyệt và hạn mức khuyến nghị; SM-1 phân bố hạng; PD trung bình; SM-5; cột "Loans hit" của luật trên Playground | KPI-01 tỷ lệ vỡ nợ; số vỡ nợ; SM-2 tỷ lệ vỡ nợ của nhóm được duyệt; phân tách theo kết quả (tốt / xấu); tỷ lệ vỡ nợ của nhóm vi phạm một luật (SM-3); SM-4 AUC |

  Các view KPI trong `src/sql/marts/50_kpi_views.sql` (`kpi_portfolio_summary`, `kpi_grade_distribution`) theo đúng quy ước này, và `GET /api/kpis/summary` đọc thẳng `mart.kpi_portfolio_summary`, nên web và view luôn cùng số. Hai view `kpi_default_by_segment`, `kpi_default_by_credit_history` chỉ chứa chỉ số theo kết quả nên chỉ tính trên khoản vay lịch sử.
- Các cột về DSS (`pd`, `score_grade`, `rule_grade`, `final_grade`, `downgraded`, `decision`, `recommended_credit`) lấy từ `dw.fact_risk_assessment` qua `LEFT JOIN` trong `mart.v_application_wide`.
- **Sau `hcdw etl`, bảng `fact_risk_assessment` rỗng.** Khi đó các KPI cần PD, hạng hoặc quyết định trả về NULL (tỷ lệ, tổng) hoặc 0 (số đếm), không báo lỗi. Chúng có giá trị sau khi chạy `hcdw score` (hoặc `hcdw etl --score`). Cột "Cần `hcdw score`" bên dưới ghi rõ KPI nào thuộc loại này.
- Ngưỡng hạng, tập hạng kích hoạt luật, ngưỡng phân khúc nhỏ và chính sách theo hạng đọc từ `configs/settings.yaml` (mục `dss`), không viết cứng trong SQL.

**KPI chính** (5 KPI):

| ID | KPI | Công thức | Cột nguồn | Ý nghĩa | View (cột) | Cần `hcdw score` |
|---|---|---|---|---|---|:---:|
| KPI-01 | Tỷ lệ vỡ nợ | `AVG(target)` trên khoản vay lịch sử (train); tính chung, theo từng hạng và từng nhóm thuộc tính | `fact_loan_application.target` | Tỷ lệ khách gặp khó khăn trả nợ | `kpi_portfolio_summary.default_rate`; `kpi_grade_distribution.default_rate` (theo hạng); `kpi_default_by_segment`, `kpi_default_by_credit_history` (theo nhóm thuộc tính) | Không (theo hạng thì có) |
| KPI-02 | Dư nợ cấp mới | `SUM(amt_credit)`, theo hạng hoặc nhóm thuộc tính | `fact_loan_application.amt_credit` | Quy mô tiền vay; dùng làm đại diện cho thu nhập lãi (cho vay càng nhiều thì lãi thu về càng nhiều). Không tính lãi cụ thể vì lãi suất và kỳ hạn mỗi hợp đồng khác nhau và dữ liệu không có. Đặt cạnh KPI-03 (Credit at risk) để thấy đánh đổi giữa doanh thu và rủi ro | `kpi_portfolio_summary.total_credit`; `kpi_grade_distribution.total_credit` | Không (theo hạng thì có) |
| KPI-03 | Dư nợ chịu rủi ro | `SUM(amt_credit × pd)` | `amt_credit`, `fact_risk_assessment.pd` | Ước lượng phần tiền vay có nguy cơ mất (giả định mất toàn bộ khi vỡ nợ) | `kpi_portfolio_summary.expected_loss`; `kpi_grade_distribution.expected_loss` | Có |
| KPI-04 | Tỷ lệ duyệt | Số hồ sơ `decision = 'approve'` / số hồ sơ đã chấm | `fact_risk_assessment.decision` | Tỷ lệ hồ sơ được đề xuất Duyệt theo chính sách hiện hành. Mô phỏng chính sách khác: `/api/policy/simulate` | `kpi_portfolio_summary.approval_rate` | Có |
| KPI-05 | Tỷ lệ chuyển thẩm định | Số hồ sơ `decision = 'review'` / số hồ sơ đã chấm | `decision` | Khối lượng công việc của chuyên viên thẩm định. Kèm `reject_rate` để ba tỷ lệ cộng lại bằng 100% | `kpi_portfolio_summary.referral_rate`, `reject_rate` | Có |

**Chỉ số phụ** (giữ định nghĩa, không đánh số KPI):

| Mã | Chỉ số phụ | Công thức | Cột nguồn | Ý nghĩa | View (cột) | Cần `hcdw score` |
|---|---|---|---|---|---|:---:|
| SM-1 | Phân bố hạng | Số hồ sơ và tỷ trọng mỗi hạng A–E = `n / số hồ sơ đã chấm`; % hồ sơ bị luật hạ hạng = `AVG(downgraded)` | `fact_risk_assessment.score_grade`, `rule_grade`, `final_grade`, `downgraded` | Cơ cấu rủi ro theo hạng cuối, và mức độ luật can thiệp vào hạng điểm | `kpi_grade_distribution` (`n`, `share`, `n_downgraded`); `kpi_portfolio_summary.downgraded_share` | Có |
| SM-2 | Tỷ lệ vỡ nợ của nhóm được duyệt | `AVG(target)` trên hồ sơ `decision = 'approve'` | `target`, `decision` | Chất lượng của nhóm được duyệt | `kpi_portfolio_summary.approved_default_rate` | Có |
| SM-3 | Tỷ lệ rủi ro | PD đã hiệu chỉnh của hồ sơ (`AVG(pd)` khi tổng hợp), hoặc tỷ lệ vỡ nợ lịch sử của nhóm vi phạm một luật | `fact_risk_assessment.pd`; `mart.rule_stats.default_rate`, `mart.segment_risk.default_rate` | Căn cứ xếp hạng A–E cho hạng điểm và hạng luật | `kpi_portfolio_summary.avg_pd`; `kpi_grade_distribution.avg_pd`; `mart.rule_stats`, `mart.segment_risk` | PD: có. Tỷ lệ luật: không |
| SM-4 | Độ phân biệt của mô hình | AUC của PD out-of-fold trên tập train (dạng Mann–Whitney: xếp hạng PD, PD bằng nhau lấy hạng trung bình). View tính trên `pd` **đã hiệu chỉnh** được lưu trong kho; hiệu chỉnh isotonic gộp nhiều hồ sơ về cùng một giá trị PD nên có nhiều PD bằng nhau, và AUC tính có xét hạng trung bình | `fact_risk_assessment.pd` với `pd_source = 'oof'`, `target` | Khả năng mô hình tách khách vỡ nợ và không vỡ nợ. `hcdw train` ghi `auc_oof` vào `models/metrics.json` trên cùng PD OOF đã hiệu chỉnh, nên hai số phải khớp nhau | `kpi_model_auc.auc`; `kpi_portfolio_summary.auc` | Có |
| SM-5 | Gánh nặng nợ | Trung vị `debt_to_income` (trả góp / thu nhập) và `credit_to_income` (tiền vay / thu nhập), thu nhập đã cắt ngoại lai | `fact_loan_application.debt_to_income`, `credit_to_income` | Chỉ số tham khảo, không dùng để xếp hạng | `kpi_portfolio_summary.median_debt_to_income`, `median_credit_to_income`; `kpi_grade_distribution` (theo hạng) | Không (theo hạng thì có) |

## Các view

| View | Hạt (một dòng là) | Ghi chú |
|---|---|---|
| `mart.kpi_portfolio_summary` | mọi hồ sơ (một dòng) | Dùng cho 5 ô KPI của Overview; chỉ số theo kết quả chỉ trên khoản vay lịch sử |
| `mart.kpi_grade_distribution` | loại hạng (`score`, `rule`, `final`) × hạng A–E | Luôn đủ 15 dòng, kể cả hạng chưa có hồ sơ (`n = 0`). `share` của hạng điểm và hạng cuối cộng lại bằng 100%; của hạng luật thì nhỏ hơn 100%, phần còn lại là hồ sơ không vi phạm luật nào. `decision` là chính sách của hạng, chỉ có nghĩa với hạng cuối. `n_downgraded` là số hồ sơ bị hạ hạng: với `grade_type = 'score'` là số hồ sơ bị hạ **từ** hạng này, với `grade_type = 'final'` là số hồ sơ bị hạ **xuống** hạng này |
| `mart.kpi_model_auc` | khoản vay lịch sử có PD OOF (một dòng) | `n`, `n_default`, `auc` |
| `mart.kpi_default_by_segment` | nhóm tuổi × nhóm thu nhập × loại thu nhập | KPI-01 theo nhóm thuộc tính |
| `mart.kpi_default_by_credit_history` | `credit_history_status` | KPI-01 theo lịch sử tín dụng (thuộc tính đầu của luật phân khúc) |
| `mart.segment_risk` | phân khúc (225 dòng trên dữ liệu thật) | Tỷ lệ và hạng của luật phân khúc BR-SEG-01, xem [dss_design.md](dss_design.md) §1 |
| `mart.rule_stats` | luật | Số hồ sơ vi phạm, tỷ lệ vỡ nợ và hạng của mỗi luật, chỉ trên khoản vay lịch sử |

## Hiển thị trên web

| KPI / chỉ số phụ | Trang | Phạm vi | Nguồn API |
|---|---|---|---|
| Số hồ sơ theo quyết định × kết quả (ô waffle "Out of every 100 applications · Historical loans") | Overview | Khoản vay lịch sử, không theo bộ lọc; bấm một ô để mở Explorer | `POST /api/explore/metrics` (nhóm rỗng, trường `book.outcome_by_decision`) |
| KPI-01..05 (5 ô KPI, nhãn "All applications · N") | Overview | Mọi hồ sơ đã chấm (KPI-01 chỉ khoản vay lịch sử), **không theo bộ lọc** | `GET /api/kpis/summary` (= `mart.kpi_portfolio_summary`) |
| KPI-01, KPI-02 theo nhóm | Overview (Segment analysis: Group by 13 thuộc tính, mặc định `credit_history_status`; biểu đồ Default rate by segment, bảng Segment size) | Không theo bộ lọc | `/api/by/{dim}` |
| Số hồ sơ theo thời gian, chia theo quyết định | Analytics (biểu đồ Applications over time) | Mọi hồ sơ, theo điều kiện slice/dice | measure `n_applications`, cột `decision` của `POST /api/analytics/cube` |
| KPI-01 theo thời gian | Analytics (biểu đồ Default rate over time) | Khoản vay lịch sử, theo điều kiện slice/dice. Đường dừng ở khoảng 2017-05 vì hồ sơ nộp sau đó là hồ sơ mới, chưa có kết quả | measure `default_rate` |
| SM-1 theo thời gian | Analytics (biểu đồ Grade mix over time, 100%) | Mọi hồ sơ, theo điều kiện slice/dice | measure `n_applications`, cột `final_grade` |
| KPI-01 theo nhóm | Analytics (biểu đồ Default rate by segment, chọn chiều bằng "Split by") | Khoản vay lịch sử, theo điều kiện slice/dice | measure `default_rate` |
| KPI-01..04, SM-5 theo chiều | Analytics (bảng Cube) | Theo điều kiện slice/dice; chọn một measure (`default_rate` chỉ trên khoản vay lịch sử) | measure trong `MEASURES`: `default_rate` (KPI-01), `n_applications`, `overdue_share`, `avg_credit`, `total_credit` (KPI-02), `median_dti` (SM-5), `avg_ext_source`, `expected_loss` (KPI-03, Credit at risk), `approval_rate` (KPI-04) |
| Chỉ số Explorer (xem dưới) | Explorer | Nhóm hồ sơ tự chọn so với mọi hồ sơ | `POST /api/explore/metrics` |
| KPI-01..05 hiện hành → mô phỏng | Playground (5 ô KPI chính; Decision mix kèm Who moves; bảng Policy by grade) | **Nhóm được duyệt** của mọi hồ sơ đã chấm; tỷ lệ vỡ nợ chỉ trên khoản vay lịch sử (xem dưới) | `POST /api/policy/simulate` |
| Luật: Loans hit, Default rate | Playground (thẻ Rules) | Loans hit = số hồ sơ vi phạm trong mọi hồ sơ (`n_violators_all`); Default rate = tỷ lệ vỡ nợ của các khoản vay lịch sử vi phạm | `GET /api/rules`, `rules` của `POST /api/policy/simulate` |
| SM-1 (phân bố hạng, % bị hạ hạng), hiệu chỉnh PD | Grades & model | Chọn All applications / Historical loans / New applications, mặc định All applications; tỷ lệ vỡ nợ chỉ trên khoản vay lịch sử | `/api/grades?scope=all\|train\|test`, `/api/explain/global` |
| SM-4 | Grades & model (ô Model AUC) | PD out-of-fold của khoản vay lịch sử | `/api/explain/global` |

**Định nghĩa trên Playground** (mỗi ô KPI ghi giá trị hiện hành → mô phỏng. Giá trị hiện hành luôn là chính sách đang cấu hình, có luật, nên trùng KPI-04/05 của Overview; mọi hồ sơ đã chấm, tỷ lệ vỡ nợ chỉ trên khoản vay lịch sử):

| KPI | Ô trên Playground |
|---|---|
| KPI-01 | Default rate (approved): tỷ lệ vỡ nợ của nhóm được duyệt (= SM-2) |
| KPI-02 | Approved credit: tiền vay được duyệt sau khi cắt hạn mức (`approved_credit`) |
| KPI-03 | Credit at risk (approved): dư nợ chịu rủi ro của nhóm được duyệt = Σ hạn mức khuyến nghị × PD (`approved_expected_loss`) |
| KPI-04 | Approval rate: tỷ lệ duyệt |
| KPI-05 | Manual review rate: tỷ lệ chuyển thẩm định |

Vì vậy KPI-01..03 trên Playground nhỏ hơn giá trị cùng mã trên Overview (Overview tính trên mọi hồ sơ, không chỉ nhóm được duyệt).

## Chỉ số của Explorer

Explorer so một **nhóm hồ sơ** (các điều kiện lọc nối bằng AND) với **mọi hồ sơ** (không điều kiện). Mã nguồn: `src/hcdw/api/explore.py`. Các chỉ số này là chỉ số phụ, không đánh số KPI.

| Chỉ số (trường API) | Công thức | Phạm vi | Ý nghĩa |
|---|---|---|---|
| Quy mô (`n_applications`, `n_historical`, `n_new`, `share_of_book`, `total_credit`) | Số hồ sơ; tỷ trọng = n nhóm / n mọi hồ sơ; Σ `amt_credit` | Mọi hồ sơ | Nhóm lớn tới đâu |
| Tỷ lệ quyết định (`auto_approve_rate`, `manual_review_rate`, `reject_rate`) | Số hồ sơ theo `decision` / số hồ sơ đã chấm | Mọi hồ sơ đã chấm (nhóm chỉ có hồ sơ mới vẫn có tỷ lệ) | Chính sách hiện hành xử lý nhóm ra sao. Giá trị của mọi hồ sơ ở đây trùng KPI-04/05 trên Overview |
| Rủi ro còn lại (`remaining_risk.approved_default_rate`) | Số vỡ nợ / số hồ sơ tự động duyệt | Khoản vay lịch sử | Rủi ro lọt qua chính sách; cần giảm trước |
| Dư nợ chịu rủi ro của nhóm duyệt (`remaining_risk.approved_expected_loss`) | Σ `recommended_credit` × PD của hồ sơ tự động duyệt | Mọi hồ sơ | Cùng cách tính KPI-03 trên Playground |
| Rủi ro đã chặn (`blocked_risk.blocked_share`) | Số vỡ nợ bị chuyển thẩm định hoặc từ chối / tổng số vỡ nợ | Khoản vay lịch sử | Phần rủi ro chính sách đã bắt được |
| Kết quả nhóm thẩm định thêm (`manual_review.review_repaid_share`, `review_defaulted_share`) | Số hồ sơ thẩm định thêm đã trả nợ (hoặc vỡ nợ) / số hồ sơ thẩm định thêm | Khoản vay lịch sử | Giả định thẩm định luôn đúng: phần trả nợ tốt có thể tự động duyệt, phần vỡ nợ có thể tự động từ chối |

## Số liệu trên dữ liệu thật (sau `hcdw etl --score`)

"Tất cả" = mọi hồ sơ (đã chấm), là giá trị hiện trên web. "Lịch sử" = khoản vay lịch sử (tập train, có kết quả trả nợ). "Mới" = hồ sơ mới (tập test).

| Mã | Chỉ số | Tất cả (web) | Lịch sử / mới |
|---|---|---|---|
| – | Số hồ sơ | 356.255 | 307.511 / 48.744 |
| KPI-01 | Tỷ lệ vỡ nợ | 8,07% (chỉ trên khoản vay lịch sử; theo `credit_history_status`: No bureau 10,12%, Clean history 7,62%, Current overdue 16,11%) | – |
| KPI-02 | Dư nợ cấp mới | 209,4 tỷ (đơn vị tiền gốc) | 184,2 tỷ / 25,2 tỷ |
| KPI-03 | Dư nợ chịu rủi ro | 15,76 tỷ | 13,84 tỷ / 1,92 tỷ |
| KPI-04 | Tỷ lệ duyệt | 51,86% | 52,07% / 50,59% |
| KPI-05 | Tỷ lệ chuyển thẩm định | 39,54% (từ chối 8,59%) | 39,26% / 41,37% (từ chối 8,68% / 8,04%) |
| SM-1 | Phân bố hạng cuối | A 2,91% · B 48,95% · C 23,03% · D 16,51% · E 8,59%; bị hạ hạng 13,55% | Lịch sử: A 3,04% · B 49,02% · C 22,86% · D 16,40% · E 8,68%; bị hạ hạng 13,58% |
| SM-2 | Vỡ nợ nhóm được duyệt | 3,37% (chỉ trên khoản vay lịch sử) | – |
| SM-3 | Tỷ lệ rủi ro | PD trung bình 8,08%; tỷ lệ của từng luật ở [dss_design.md](dss_design.md) §2 | PD trung bình 8,07% / 8,10% |
| SM-4 | AUC (OOF) | 0,7586 (chỉ trên khoản vay lịch sử) | – |
| SM-5 | Gánh nặng nợ | Trung vị DTI 0,163; trung vị tiền vay / thu nhập 3,16 (chỉ tham khảo) | Tiền vay / thu nhập 3,27 / 2,67 |

Chỉ KPI-01, KPI-02 và SM-5 có giá trị ngay sau `hcdw etl`; các chỉ số còn lại cần `hcdw score`. Các view KPI (`mart.kpi_portfolio_summary`…) đã tính trên mọi hồ sơ, nên cột "Tất cả" chính là số của view và của `GET /api/kpis/summary`.
