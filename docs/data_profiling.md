# Khảo sát dữ liệu

Các thống kê dưới đây là căn cứ số liệu cho [business_requirements.md](business_requirements.md).

- **Dữ liệu:** bản [Kaggle Datasets](https://www.kaggle.com/datasets/megancrenshaw/home-credit-default-risk) của cuộc thi Home Credit Default Risk, lưu ở `data/raw/`.
- **Cách tính:** truy vấn DuckDB trực tiếp trên `data/raw/` (12/11/2026).
- **Định dạng số:** dấu chấm ngăn hàng nghìn, dấu phẩy là dấu thập phân.
- **Tỷ lệ vỡ nợ:** là tỷ lệ hồ sơ có `TARGET = 1`, chỉ tính trên `application_train`.

## 1. Quy mô dữ liệu

Bộ dữ liệu có 8 bảng, khoảng 58,5 triệu dòng, 2,7 GB CSV.

| Bảng | Số dòng | Số cột |
|---|---:|---:|
| `application_train` | 307.511 | 122 |
| `application_test` | 48.744 | 121 |
| `bureau` | 1.716.428 | 17 |
| `bureau_balance` | 27.299.925 | 3 |
| `previous_application` | 1.670.214 | 37 |
| `installments_payments` | 13.605.401 | 8 |
| `POS_CASH_balance` | 10.001.358 | 8 |
| `credit_card_balance` | 3.840.312 | 23 |

## 2. Nhãn

| `TARGET` | Số hồ sơ | Tỷ lệ |
|---|---:|---:|
| 0 (trả nợ bình thường) | 282.686 | 91,93% |
| 1 (gặp khó khăn trả nợ) | 24.825 | 8,07% |

## 3. Độ phủ giữa các bảng

| Chỉ số | Giá trị |
|---|---:|
| Khách trong `application_train` | 307.511 |
| Khách trong `application_test` | 48.744 |
| Khách có dữ liệu trong `bureau` | 305.811 |
| Khách có dữ liệu trong `previous_application` | 338.857 |
| Khách train có dữ liệu bureau | 263.491 (85,7%) |
| Khách train có hồ sơ cũ tại Home Credit | 291.057 (94,6%) |
| Khách train **không** có dữ liệu bureau | 44.020 (14,3%) |

## 4. Phạm vi thời gian

Mọi cột thời gian đều tính tương đối so với ngày nộp hồ sơ. Giá trị âm nghĩa là trước ngày nộp.

| Cột | Nhỏ nhất | Lớn nhất |
|---|---:|---:|
| `bureau.DAYS_CREDIT` (ngày) | −2.922 | 0 |
| `previous_application.DAYS_DECISION` (ngày) | −2.922 | −1 |
| `bureau_balance.MONTHS_BALANCE` (tháng) | −96 | 0 |

## 5. Chất lượng dữ liệu (`application_train`)

| Vấn đề | Giá trị |
|---|---:|
| `DAYS_EMPLOYED = 365243` (giá trị giả) | 55.374 hồ sơ (18,0%); tỷ lệ vỡ nợ của nhóm này 5,4% |
| `CODE_GENDER = 'XNA'` | 4 hồ sơ |
| Số cột thiếu trên 40% giá trị | 49 / 122 |
| `OCCUPATION_TYPE` thiếu | 31,3% |
| `EXT_SOURCE_1` thiếu | 56,4% |
| `EXT_SOURCE_3` thiếu | 19,8% |
| `OWN_CAR_AGE` thiếu | 66,0% |
| `AMT_REQ_CREDIT_BUREAU_YEAR` thiếu | 13,5% |

Hầu hết hồ sơ có `DAYS_EMPLOYED = 365243` là người nghỉ hưu, nên tỷ lệ vỡ nợ của nhóm này thấp.

## 6. Phân phối số tiền (`application_train`)

| Chỉ số | Nhỏ nhất | Trung vị | P99 | Lớn nhất |
|---|---:|---:|---:|---:|
| Thu nhập (`AMT_INCOME_TOTAL`) | 25.650 | 147.150 | 472.500 | 117.000.000 |
| Tiền vay (`AMT_CREDIT`) | 45.000 | 513.531 | 1.854.000 | 4.050.000 |
| Trả góp (`AMT_ANNUITY`) | 1.615,5 | 24.903 | 70.006,5 | 258.025,5 |
| Giá hàng (`AMT_GOODS_PRICE`) | 40.500 | 450.000 | 1.800.000 | 4.050.000 |
| Tiền vay / thu nhập | 0,005 | 3,27 | 13,03 | 84,74 |
| Trả góp / thu nhập (DTI) | 0,0002 | 0,163 | 0,483 | 1,876 |

## 7. Tỷ lệ vỡ nợ theo thuộc tính (`application_train`)

Tỷ lệ vỡ nợ chung của danh mục là **8,07%**.

**Loại hợp đồng**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| Cash loans | 278.232 | 90,5% | 8,35% |
| Revolving loans | 29.279 | 9,5% | 5,48% |

**Giới tính** (chỉ để tham khảo, không dùng làm căn cứ quyết định)

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| F | 202.448 | 65,8% | 7,00% |
| M | 105.059 | 34,2% | 10,14% |
| XNA | 4 | 0,0% | 0,00% |

**Loại thu nhập**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| Working | 158.774 | 51,6% | 9,59% |
| Commercial associate | 71.617 | 23,3% | 7,48% |
| Pensioner | 55.362 | 18,0% | 5,39% |
| State servant | 21.703 | 7,1% | 5,75% |
| Unemployed | 22 | 0,0% | 36,36% |
| Student | 18 | 0,0% | 0,00% |
| Businessman | 10 | 0,0% | 0,00% |
| Maternity leave | 5 | 0,0% | 40,00% |

**Học vấn**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| Secondary / secondary special | 218.391 | 71,0% | 8,94% |
| Higher education | 74.863 | 24,3% | 5,36% |
| Incomplete higher | 10.277 | 3,3% | 8,48% |
| Lower secondary | 3.816 | 1,2% | 10,93% |
| Academic degree | 164 | 0,1% | 1,83% |

**Tình trạng hôn nhân**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| Married | 196.432 | 63,9% | 7,56% |
| Single / not married | 45.444 | 14,8% | 9,81% |
| Civil marriage | 29.775 | 9,7% | 9,94% |
| Separated | 19.770 | 6,4% | 8,19% |
| Widow | 16.088 | 5,2% | 5,82% |
| Unknown | 2 | 0,0% | 0,00% |

**Nhà ở**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| House / apartment | 272.868 | 88,7% | 7,80% |
| With parents | 14.840 | 4,8% | 11,70% |
| Municipal apartment | 11.183 | 3,6% | 8,54% |
| Rented apartment | 4.881 | 1,6% | 12,31% |
| Office apartment | 2.617 | 0,9% | 6,57% |
| Co-op apartment | 1.122 | 0,4% | 7,93% |

**Hạng vùng (`REGION_RATING_CLIENT`)**

| Hạng | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| 1 | 32.197 | 10,5% | 4,82% |
| 2 | 226.984 | 73,8% | 7,89% |
| 3 | 48.330 | 15,7% | 11,10% |

**Nghề nghiệp** (12 nhóm đông nhất)

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| (thiếu) | 96.391 | 31,3% | 6,51% |
| Laborers | 55.186 | 17,9% | 10,58% |
| Sales staff | 32.102 | 10,4% | 9,63% |
| Core staff | 27.570 | 9,0% | 6,30% |
| Managers | 21.371 | 6,9% | 6,21% |
| Drivers | 18.603 | 6,0% | 11,33% |
| High skill tech staff | 11.380 | 3,7% | 6,16% |
| Accountants | 9.813 | 3,2% | 4,83% |
| Medicine staff | 8.537 | 2,8% | 6,70% |
| Security staff | 6.721 | 2,2% | 10,74% |
| Cooking staff | 5.946 | 1,9% | 10,44% |
| Cleaning staff | 4.653 | 1,5% | 9,61% |

**Nhóm tuổi**

| Nhóm | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| 18–24 | 12.233 | 4,0% | 12,29% |
| 25–34 | 72.429 | 23,6% | 10,66% |
| 35–44 | 84.261 | 27,4% | 8,41% |
| 45–54 | 70.190 | 22,8% | 7,05% |
| 55–64 | 60.522 | 19,7% | 5,42% |
| 65+ | 7.876 | 2,6% | 3,66% |

**Điểm nguồn ngoài `EXT_SOURCE_2`** (chia 5 nhóm, nhóm 1 là điểm thấp nhất)

| Nhóm | Số hồ sơ | Tỷ lệ vỡ nợ |
|---|---:|---:|
| 1 (thấp nhất) | 61.503 | 15,21% |
| 2 | 61.502 | 9,13% |
| 3 | 61.502 | 7,03% |
| 4 | 61.502 | 5,36% |
| 5 (cao nhất) | 60.842 | 3,58% |
| Thiếu | 660 | 7,88% |

**Trả góp / thu nhập (DTI)**

| Nhóm DTI | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ |
|---|---:|---:|---:|
| < 10% | 54.268 | 17,6% | 7,26% |
| 10–20% | 147.867 | 48,1% | 8,01% |
| 20–30% | 73.936 | 24,0% | 8,76% |
| 30–40% | 23.492 | 7,6% | 8,11% |
| ≥ 40% | 7.948 | 2,6% | 8,25% |

## 8. Lịch sử tín dụng và vỡ nợ

**Hành vi trong quá khứ**

| Nhóm | Số hồ sơ | Tỷ lệ vỡ nợ |
|---|---:|---:|
| Hơn 50% hồ sơ cũ tại Home Credit bị từ chối | 11.158 | 15,93% |
| Chưa có hồ sơ cũ nào bị từ chối | 190.763 | 7,07% |
| Trả trễ hơn 20% số kỳ trả góp | 37.475 | 12,01% |
| Không trễ kỳ trả góp nào | 136.636 | 6,80% |
| Đang quá hạn ở bureau | 3.397 | 15,90% |
| Có lịch sử bureau, không quá hạn | 260.094 | 7,62% |
| Không có dữ liệu bureau | 44.020 | 10,12% |

**Trạng thái lịch sử tín dụng**

| Trạng thái | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ | Tổng tiền vay (tỷ) |
|---|---:|---:|---:|---:|
| Không có dữ liệu bureau | 44.020 | 14,3% | 10,12% | 24,4 |
| Có lịch sử, không quá hạn | 260.094 | 84,6% | 7,62% | 157,9 |
| Đang quá hạn ở bureau | 3.397 | 1,1% | 15,90% | 1,9 |

**Trạng thái lịch sử tín dụng × nhóm tuổi** (tỷ lệ vỡ nợ)

| Trạng thái | 18–24 | 25–34 | 35–44 | 45–54 | 55+ |
|---|---:|---:|---:|---:|---:|
| Không có dữ liệu bureau | 12,57% | 13,46% | 10,63% | 8,37% | 6,68% |
| Có lịch sử, không quá hạn | 12,01% | 10,01% | 7,98% | 6,76% | 4,91% |
| Đang quá hạn ở bureau | 28,28% | 21,28% | 16,96% | 12,76% | 10,34% |

## 9. Các bảng lịch sử

**`bureau`: trạng thái khoản vay**

| Trạng thái | Số khoản vay | % đang quá hạn | Dư nợ (tỷ) |
|---|---:|---:|---:|
| Closed | 1.079.273 | 0,01% | 2,6 |
| Active | 630.607 | 0,64% | 196,8 |
| Sold | 6.527 | 1,23% | 0,6 |
| Bad debt | 21 | 47,62% | 0,0 |

**`bureau`: loại khoản vay**

| Loại | Số khoản vay |
|---|---:|
| Consumer credit | 1.251.615 |
| Credit card | 402.195 |
| Car loan | 27.690 |
| Mortgage | 18.391 |
| Microloan | 12.413 |
| Loan for business development | 1.975 |
| Another type of loan | 1.017 |
| Unknown type of loan | 555 |

**`bureau_balance`: trạng thái theo tháng**

Ý nghĩa mã: 0 là không quá hạn; 1 đến 5 là các mức quá hạn tăng dần (1: 1–30 ngày, …, 5: trên 120 ngày hoặc đã bán hoặc xoá nợ); C là đã đóng; X là không rõ.

| Mã | Số dòng |
|---|---:|
| 0 | 7.499.507 |
| 1 | 242.347 |
| 2 | 23.419 |
| 3 | 8.924 |
| 4 | 5.847 |
| 5 | 62.406 |
| C | 13.646.993 |
| X | 5.810.482 |

**`previous_application`: kết quả hồ sơ**

| Kết quả | Số hồ sơ | Tỷ lệ |
|---|---:|---:|
| Approved | 1.036.781 | 62,1% |
| Canceled | 316.319 | 18,9% |
| Refused | 290.678 | 17,4% |
| Unused offer | 26.436 | 1,6% |

**`previous_application`: tỷ lệ duyệt theo loại hợp đồng**

| Loại | Số hồ sơ | Tỷ lệ duyệt |
|---|---:|---:|
| Cash loans | 747.553 | 41,8% |
| Consumer loans | 729.151 | 85,9% |
| Revolving loans | 193.164 | 50,6% |
| XNA | 346 | 0,0% |

**`previous_application`: lý do từ chối**

| Mã lý do | Số hồ sơ |
|---|---:|
| HC | 175.231 |
| LIMIT | 55.680 |
| SCO | 37.467 |
| SCOFR | 12.811 |
| XNA | 5.236 |
| VERIF | 3.535 |
| SYSTEM | 717 |
| XAP | 1 |

**`previous_application`: tỷ lệ duyệt theo kênh**

| Kênh | Số hồ sơ | Tỷ lệ duyệt |
|---|---:|---:|
| Credit and cash offices | 719.968 | 40,1% |
| Country-wide | 494.690 | 81,4% |
| Stone | 212.083 | 89,2% |
| Regional / Local | 108.528 | 88,8% |
| Contact center | 71.297 | 35,4% |
| AP+ (Cash loan) | 57.046 | 54,7% |
| Channel of corporate sales | 6.150 | 43,1% |
| Car dealer | 452 | 63,3% |

**`previous_application`: nhóm lãi suất**

| Nhóm | Số hồ sơ |
|---|---:|
| XNA | 517.215 |
| middle | 385.532 |
| high | 353.331 |
| low_normal | 322.095 |
| low_action | 92.041 |

**`installments_payments`**

| Chỉ số | Giá trị |
|---|---:|
| Số kỳ trả góp | 13.605.401 |
| Kỳ trả trễ | 8,43% |
| Kỳ trả trễ trên 30 ngày | 0,28% |
| Kỳ trả thiếu | 9,52% |

**`POS_CASH_balance`: trạng thái hợp đồng**

| Trạng thái | Số dòng | % có quá hạn (`SK_DPD > 0`) |
|---|---:|---:|
| Active | 9.151.119 | 3,07% |
| Completed | 744.883 | 1,36% |
| Signed | 87.260 | 0,13% |
| Demand | 7.065 | 49,64% |
| Returned to the store | 5.461 | 0,05% |
| Approved | 4.917 | 0,02% |

**`credit_card_balance`**

| Chỉ số | Giá trị |
|---|---:|
| Số dòng (thẻ × tháng) | 3.840.312 |
| Số thẻ | 104.307 |
| Trung vị dư nợ / hạn mức | 0,011 |
| % dòng có quá hạn | 3,99% |

## 10. Thử nghiệm thang hạng A–E

**Ngưỡng hạng theo tỷ lệ rủi ro (xác suất vỡ nợ)**

| Hạng | Tỷ lệ rủi ro |
|:---:|---|
| A | < 1% |
| B | 1% – 7% |
| C | 7% – 13% |
| D | 13% – 20% |
| E | ≥ 20% |

**Hồ sơ có rủi ro rất thấp có tồn tại không**

Nhóm dùng tạm điểm EXT_SOURCE trung bình làm điểm cá nhân, rồi chia hồ sơ thành 100 nhóm bằng nhau theo điểm này. Ngay cả 10% hồ sơ có điểm cao nhất vẫn có tỷ lệ vỡ nợ từ 1,59% đến 2,60%. Riêng 1% điểm cao nhất là 1,76%.

Như vậy, chỉ dùng một chỉ số thì không có nhóm nào đạt dưới 1%. Hạng A (dưới 1%) dự kiến chỉ gồm một số ít hồ sơ mà mô hình đầy đủ (kết hợp nhiều thuộc tính) đánh giá rất tốt.

**Xếp hạng thử các phân khúc 4 thuộc tính** (phương án ứng viên cho luật phân khúc, để tham khảo khi thiết kế DSS)

Bốn thuộc tính là: trạng thái lịch sử tín dụng (3 nhóm), nhóm tuổi (5 nhóm), loại thu nhập (5 nhóm), hạng vùng (3 nhóm). Tỷ lệ rủi ro của mỗi tổ hợp là tỷ lệ vỡ nợ lịch sử của tổ hợp đó.

| Hạng | Số tổ hợp | Số hồ sơ | Tỷ trọng | Tỷ lệ vỡ nợ thực tế |
|:---:|---:|---:|---:|---:|
| A | 26 | 74 | 0,0% | 0,00% |
| B | 51 | 116.460 | 37,9% | 5,17% |
| C | 63 | 170.667 | 55,5% | 9,17% |
| D | 32 | 18.136 | 5,9% | 14,58% |
| E | 23 | 2.174 | 0,7% | 23,83% |

- **Hạng A ở đây là nhiễu.** Cả 26 tổ hợp hạng A đều có tối đa 9 hồ sơ và tỷ lệ 0%. Theo quy tắc gộp tổ hợp dưới 500 hồ sơ lên cấp cha, chúng sẽ không còn là hạng A.
- **Tổ hợp thực sự an toàn nhất** có từ 500 hồ sơ trở lên là "Có lịch sử, không quá hạn, 55+, Commercial associate, vùng 1": 1.205 hồ sơ, tỷ lệ vỡ nợ 2,99% (hạng B). Vì vậy luật phân khúc không bao giờ cho hạng A. Đây là căn cứ để khi thiết kế, luật phân khúc chỉ nên tính là vi phạm với các phân khúc rủi ro cao, ví dụ hạng D hoặc E.
- **Nếu chỉ tính vi phạm với phân khúc hạng D và E:** các tổ hợp này chiếm 20.310 hồ sơ (6,6%). Tức là luật phân khúc sẽ kích hoạt với khoảng 6,6% hồ sơ. Có 6 tổ hợp hạng D đủ từ 500 hồ sơ trở lên, chiếm 15.468 hồ sơ.
- **Hạng E của phân khúc rất hiếm.** Có 23 tổ hợp đạt từ 20% trở lên, nhưng tổng cộng chỉ 2.174 hồ sơ. Chỉ 2 tổ hợp có từ 300 hồ sơ trở lên. Tổ hợp E lớn nhất là "Không có dữ liệu bureau, 25–34, Working, vùng 3" với 1.387 hồ sơ.
- **Số tổ hợp:** tập train có 195 tổ hợp. Chỉ 62 tổ hợp có từ 500 hồ sơ trở lên, nhưng chúng chiếm 293.759 hồ sơ (95,5%).

Ba tổ hợp rủi ro cao nhất trong số các tổ hợp có từ 300 hồ sơ trở lên:

| Tổ hợp | Số hồ sơ | Tỷ lệ rủi ro | Hạng |
|---|---:|---:|:---:|
| Đang quá hạn ở bureau, 25–34, Working, vùng 2 | 404 | 23,76% | E |
| Không có dữ liệu bureau, 25–34, Working, vùng 3 | 1.387 | 22,78% | E |
| Có lịch sử, không quá hạn, 18–24, Working, vùng 3 | 993 | 18,33% | D |

**Hạng của một số luật nghiệp vụ ứng viên** (tham khảo cho thiết kế DSS)

| Luật ứng viên | Tỷ lệ rủi ro (tỷ lệ vỡ nợ của nhóm vi phạm) | Hạng |
|---|---:|:---:|
| Hơn 50% hồ sơ cũ tại Home Credit bị từ chối | 15,93% | D |
| Trả trễ hơn 20% số kỳ trả góp | 12,01% | C |
| Từng quá hạn trên 90 ngày | chưa tính | |

## 11. Nhận xét chính

- **Mất cân bằng lớp:** nhãn 1 chỉ chiếm 8,07%.
- **Thuộc tính phân biệt rủi ro rõ:** `EXT_SOURCE_2` (nhóm thấp nhất có tỷ lệ vỡ nợ gấp hơn 4 lần nhóm cao nhất), tuổi, hạng vùng, loại thu nhập, học vấn, nhà ở và lịch sử tín dụng.
- **DTI gần như không phân biệt được rủi ro:** tỷ lệ vỡ nợ chỉ dao động 7,26% đến 8,76% giữa các nhóm. Vì vậy DTI chỉ dùng làm chỉ số tham khảo.
- **Kết hợp lịch sử tín dụng với tuổi** cho chênh lệch rất lớn. Riêng nhóm 25–34 tuổi, tỷ lệ vỡ nợ đi từ 10,01% lên 21,28%.
- **Phân khúc 4 thuộc tính phân biệt được rủi ro:** tỷ lệ vỡ nợ thực tế tăng đều từ hạng B đến hạng E (2,81% lên 23,83%). Tuy nhiên không phân khúc đông người nào đạt dưới 1%, nên hạng A chỉ có thể đến từ điểm cá nhân.
