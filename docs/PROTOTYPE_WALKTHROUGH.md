# Hướng dẫn trải nghiệm CloudOps prototype

Đây là prototype React/Vite có thể thao tác, dùng shadcn/ui và fixture trong trình duyệt. Không có backend, Kubernetes, Prometheus, model endpoint hoặc credential thật. Nút duyệt, thực thi, rollback, retry và kiểm tra kết nối chỉ chuyển trạng thái mô phỏng; không gửi lệnh ra hệ thống bên ngoài.

## Chạy trên máy phát triển

Cài Node.js tương thích với phiên bản Vite trong `apps/web/package.json` và npm. Từ thư mục repository, chạy lần lượt:

```powershell
cd apps/web
npm ci
npm run dev
```

Mở URL local mà Vite in trong terminal, thường là `http://127.0.0.1:5173`. Nếu cổng bận, dùng URL thực tế trong output. Giữ terminal chạy trong lúc dùng prototype; `Ctrl+C` để dừng.

Các kiểm tra có thể chạy riêng:

```powershell
npm test
npm run build
```

`npm ci` cần tải dependency. Sau khi cài và khởi động, các thao tác nghiệp vụ trong app dùng dữ liệu local; không gọi API sản phẩm. Build/test thành công không thay thế review giao diện trên browser.

## Cách điều khiển demo

- **Work queue:** lọc/tìm incident và mở workspace; nút **Điều tra workload** tạo một lượt điều tra chủ động.
- **Workloads:** inventory, workload detail, known-good baseline và tab **Recommendations**. Không có một trang Approvals riêng; quyết định nằm trong incident.
- **Knowledge:** tìm/lọc và đọc runbook có source/version, tải Markdown và mô phỏng index degraded/reindex.
- **Connections:** xem health, cấu hình hướng dẫn, unknown usage và mô phỏng transport/RCA contract.

Chọn **Chọn user story** ở sidebar hoặc **Đổi story** trên banner để tải một trong 24 fixture. Chọn story sẽ thay dữ liệu nghiệp vụ hiện tại bằng fixture mới. Nút biểu tượng bắt đầu lại trên banner reset story đang chọn. Menu tài khoản cho đổi Operator/Viewer và mô phỏng hết phiên; nút mặt trời/mặt trăng đổi theme.

Trong incident, **Tiếp tục mô phỏng** đẩy đúng một stage. Không có worker tự chạy nền: hãy bấm tiếp khi nút còn xuất hiện, rồi đọc trạng thái mới. Một luồng thường đi qua queued → collecting → analyzing → kết quả điều tra; sau duyệt đi qua queued → executing → verifying → outcome. Không dùng nút này để thử gửi lại thao tác ghi khi outcome unknown; tình huống đó có controls riêng.

Dữ liệu nghiệp vụ và theme được lưu bằng localStorage. Refresh thường giữ tiến trình demo. Chọn lại story để có điểm bắt đầu xác định; không dùng dữ liệu trình duyệt làm nguồn lưu trữ incident thật. Bộ lọc/tab incident dùng hash URL để điều hướng trong prototype.

## Những hành trình nên thử

| Vai trò / mục tiêu | Đường thao tác thực tế | Kết quả mong đợi trong demo |
|---|---|---|
| Operator: phục hồi OOM | Chọn **Phục hồi OOM** → đọc Findings → bấm citation hoặc mở Evidence → **Review & duyệt** → đọc diff/target → **Duyệt thay đổi** → **Tiếp tục mô phỏng** qua các stage | Profile bộ nhớ trong giới hạn được mô phỏng; outcome tách khỏi trạng thái incident. OOM không tự được gọi là memory leak. |
| Operator: kiểm tra từ chối/xung đột | Tại plan chờ duyệt, chọn **Từ chối** và ghi lý do; hoặc mở review, bung **Thử xung đột trước khi gửi duyệt**, chọn target đổi/hết hạn rồi gửi | Plan rejected/stale/expired không thể thực thi; muốn có plan mới cần điều tra lại. Reset story để thử nhánh khác. |
| Operator: chuẩn bị restore | Chọn **Chuẩn bị bản tốt để khôi phục** → Workloads → catalog-api → **Xác nhận known-good** → review rev17, `APP_MODE=production`, 1/1 Ready và hash → xác nhận → **Demo: inject rollout lỗi** → mở incident catalog → tiếp tục điều tra → review/duyệt restore | Baseline được xác nhận trước lỗi; revision18 thiếu `APP_MODE` mới là bản lỗi. Confirm chỉ lưu metadata demo. Template đang lỗi không được xác nhận healthy. |
| Operator: scale có điều kiện | Chọn **Lưu lượng tăng đột biến** → Findings/Evidence → mở điều kiện, rủi ro và xác minh → duyệt diff replica → tiếp tục | Scale chỉ trong bounds fixture. Đọc cả traffic/latency và capacity; Ready không tự chứng minh dịch vụ phục hồi. |
| Operator: chẩn đoán thủ công | Chọn **Chẩn đoán Pending** hoặc **Chẩn đoán lỗi kéo image** → Evidence → **Đọc runbook** → **Bàn giao thủ công**, ghi lý do → Handoff | Không có executable plan cho hai pattern này. Handoff vẫn chứa finding, evidence và phần chưa kiểm chứng. |
| Operator: chủ động điều tra | Work queue → **Điều tra workload**, hoặc Workloads → detail → **Bắt đầu điều tra** → chọn workload, symptom, window và nhập câu hỏi → bắt đầu → tiếp tục | Tạo incident/run mới có scope và câu hỏi. Không cần alert thật; câu hỏi không rỗng. |
| Operator: thiếu evidence/quota | Chọn **Thiếu bằng chứng** hoặc **Hết quota model** → đọc phần thiếu/trạng thái chờ → **Thử điều tra lại**, rồi tiếp tục; lượt chờ phù hợp cũng có **Hủy lượt đang chờ** | Evidence cũ còn xem được; lỗi provider không tạo plan từ output không hợp lệ, không tự fallback trả phí. |
| Operator: xem lịch sử điều tra | Chọn **Điều tra lại thất bại** → đọc banner kết quả cũ → **Snapshot điều tra** / **Xem lượt mới nhất** → **So sánh runs**, chọn run khác | Kết luận completed trước vẫn tồn tại; lượt mới failed không được trình bày như kết luận mới thành công. Có thể mở evidence riêng từng snapshot. |
| Operator: rollback | Chọn **Xác minh và rollback** → duyệt action → tiếp tục đến verification failed → **Review rollback** → đọc inverse diff, nhập lý do → xác nhận → tiếp tục | Rollback là quyết định riêng. Outcome rolled_back không bị trộn với initial action succeeded. |
| Operator: rollback bị chặn | Chọn một story rollback hết hạn/target đổi/profile cũ không an toàn, đi đến post-check rồi thử review rollback | Guard giải thích lý do không được inverse write. Với **Rollback không hoàn tất**, tiếp tục nhánh rollback để nhận manual intervention và bàn giao. |
| Operator: xử lý unknown | Chọn **Chưa rõ kết quả ghi** → duyệt và tiếp tục đến uncertain → **Đối soát kết quả** tối đa hai lần → có thể bàn giao/đóng với lý do → **Xác minh thủ công để mở khóa** | Không replay write; đóng incident vẫn giữ unknown/target lock. Để mở khóa cần nhập evidence, chọn kết quả và xác nhận yêu cầu ghi cũ không thể ghi muộn. Đây đều là khai báo fixture, không xác minh hệ thống thật. |
| Operator: ghi chú và đóng/mở lại | Activity → nhập ghi chú → **Thêm ghi chú** → **Đóng incident**, nhập lý do → Handoff → chọn snapshot đã lưu → **Mở lại** với lý do | Resolution snapshot giữ evidence, plan, outcome và note lúc đóng; mở lại không sửa snapshot cũ. Đóng incident không tự xác nhận recovery. |
| Viewer: xem và xuất | Chọn **Quyền chỉ đọc** → mở evidence, history và Handoff → **Sao chép** / **Tải Markdown** | Xem/export được; acknowledge, approve, note và các mutation bị vô hiệu. Clipboard cần browser cho phép; dùng download nếu không copy được. |
| Người dùng hết phiên | Chọn **Phiên đăng nhập hết hạn** hoặc menu tài khoản → mô phỏng hết phiên → **Tiếp tục với tài khoản demo** / xem với Viewer | Không dùng tài khoản/password thật. Khôi phục phiên không tự gửi lại quyết định trước đó. |
| Người review resource | Workloads → Recommendations → chọn report → đọc current/observed/recommended, units, coverage và exclusions → **Xuất JSON** | File tải xuống chứa report 6 giờ và các giới hạn. HPA/recent OOM/insufficient/stale không bị biểu diễn thành recommendation bằng zero; export không phải apply hoặc bill savings. |
| Người review tri thức | Knowledge → tìm theo từ khóa/tag → **Đọc runbook** → xem source/v3 → tải Markdown; thử reindex demo | Catalog vẫn đọc được khi index degraded. Reindex chỉ đổi trạng thái local, không gọi embedding provider. |
| Operator: kiểm tra model | Connections → **Hướng dẫn** → chọn fixture transport/contract/pass/401/429 → **Chạy kiểm tra demo** | Phân biệt timeout, credential rejected, rate limit và structured RCA không hợp lệ. HTTP reachable không đồng nghĩa RCA contract pass; tool calling không phải yêu cầu baseline. Usage/quota chưa có dữ liệu vẫn unavailable/unknown. |

## Danh sách đủ 24 story

| # | Tên trong bộ chọn | Điểm cần quan sát |
|---|---|---|
| 01 | Phục hồi OOM | Approval → verification → resolution. |
| 02 | Chuẩn bị bản tốt để khôi phục | Healthy baseline trước fault injection. |
| 03 | Lưu lượng tăng đột biến | Capacity và metrics trước scale. |
| 04 | Chẩn đoán Pending | Investigation-only, manual handoff. |
| 05 | Chẩn đoán lỗi kéo image | Evidence registry/image; không đọc secret. |
| 06 | Thiếu bằng chứng | Missing evidence và retry. |
| 07 | Hết quota model | Waiting/retry/cancel, không paid fallback. |
| 08 | Target đã thay đổi | Plan stale không thể dùng. |
| 09 | Plan hết hạn | TTL approval được thể hiện riêng. |
| 10 | Xác minh và rollback | Review inverse diff và quyết định rollback. |
| 11 | Rollback không hoàn tất | Manual intervention, không báo rollback thành công. |
| 12 | Hết cửa sổ rollback | Thời hạn rollback riêng. |
| 13 | Target rollback đã đổi | Không ghi đè field đã đổi. |
| 14 | Profile cũ không an toàn | Không trở lại memory profile gây OOM. |
| 15 | HPA quản lý replica | Không tranh quyền sở hữu replicas. |
| 16 | Node không đủ headroom | Không tạo scale plan thiếu capacity. |
| 17 | Chưa có bản tốt | Template lỗi không thể làm baseline. |
| 18 | Cấu hình ngoài template đổi | Restore template không khôi phục ConfigMap/Secret. |
| 19 | Điều tra lại thất bại | Giữ completed run cũ và so sánh lịch sử. |
| 20 | Chưa rõ kết quả ghi | Bounded reconcile, lock, manual verification. |
| 21 | Workload đã tự hồi phục | No-op không tính là action success. |
| 22 | Chưa đủ kết luận phục hồi | Ready nhưng coverage/tải chưa đủ. |
| 23 | Quyền chỉ đọc | Viewer xem và export, không mutation. |
| 24 | Phiên đăng nhập hết hạn | Login demo và khôi phục quyền rõ ràng. |

## Những điều prototype chưa chứng minh

State machine, guards và màn hình là mô phỏng để review sản phẩm. Backend production vẫn cần enforce authorization, immutable approval/hash/expiry, job persistence, idempotency, identity/precondition checks, scoped executor, reconciliation, telemetry collection và actual post-check. LocalStorage không thay database/audit; fixture hash không phải checksum một tài nguyên thật; kiểm tra endpoint local không xác minh một provider thật.

Các biểu đồ, timestamps, coverage, KRR suggestions và runbook nội dung dùng để review luồng, không phải kết quả benchmark hoặc resource/billing evidence. Các nút có phản hồi local và file export thật, nhưng không phải bằng chứng backend đã triển khai. Hướng dẫn này không tuyên bố browser QA toàn diện hoặc thiết kế đã được người dùng chấp thuận; cần walkthrough trực tiếp ở dark/light, desktop/mobile và keyboard trước khi chốt trải nghiệm.
