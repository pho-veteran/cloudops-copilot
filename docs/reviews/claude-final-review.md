# Xác nhận cuối CloudOps Copilot, round 3

## Kết luận: APPROVE (chỉ áp dụng cho bản kế hoạch)

Blocker F1 duy nhất của round 2 đã được sửa ở cả hai nơi: bảng hợp đồng action và ba đoạn copy trong prototype. F2–F8 cũng đã được xử lý trong tài liệu. Tôi không thấy lỗi mới nào do các bản sửa gây ra mà làm sai lệch an toàn, contract hay scope. Còn hai chỗ nhỏ ở file research chưa khớp hoàn toàn với lời author nói, đều không chặn (xem mục 3).

APPROVE ở đây nghĩa là duyệt định hướng sản phẩm, kiến trúc và prototype UI. Nó không xác nhận runtime sẵn sàng: provider, pi SDK, binding, quota, build và accessibility đều chưa được chứng minh.

## 1. Đối chiếu F1–F8 (đường dẫn tương đối từ `round3/`)

| # | Trạng thái | Bằng chứng |
|---|---|---|
| F1 post-check | Đã xử lý | Bảng action `CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:79-81` giờ dùng rollout Ready, delta OOM/restart trong verification window, ngưỡng version hóa và coverage. Riêng `:81` ghi "tải giảm mạnh trả inconclusive". `:89` giữ "tải tương đương" chỉ làm điều kiện benchmark. Prototype `docs/cloudops-ui-template.html:40`, `:48`, `:50` đã đổi sang "fresh telemetry, configured metric thresholds and adequate coverage; falling traffic or missing coverage → inconclusive". Tôi grep toàn bộ round3: không còn "comparable" trong HTML. "Tương đương" chỉ còn ở `:89`, đúng ngữ cảnh benchmark. So với round2 HTML, chỉ ba câu copy này thay đổi. |
| F2 spike | Đã xử lý | `:131`: spike 22–23/10, kết thúc cuối 23/10; nếu kickoff dời thì phải ghi ngày trước khi chạy. Ba tiêu chí phải cùng đạt; một tiêu chí chưa đạt thì chuyển direct Gemini adapter ngay, không đợi 04/11. `:297`: quyết định runtime phải xong khi hết spike, và mốc 04/11 phải có estimate theo từng chặng. |
| F3 baseline restore | Đã xử lý | Acceptance `:267` yêu cầu confirm known-good khi workload còn healthy, trước khi inject lỗi; thiếu baseline thì manual-only, có lý do và link. UI spec `:76` có câu "No confirmed known-good baseline" và link sang Workload detail, không đề nghị xác nhận workload đang lỗi. |
| F4 deferred | Đã xử lý | `:159`: deferred là `queued` kèm `queue_reason=capacity`, không thêm enum, khớp `:178`. UI `:134`: Blocked filter có tính trạng thái này. |
| F5 rollback window | Đã xử lý | `:203`: cửa sổ rollback riêng theo template, tính từ khi verification có outcome xác định, không dùng TTL approval; write còn uncertain phải reconcile trước. UI `:163` ghi tương tự. |
| F6 restore | Đã xử lý | `:79`: "chưa đổi kể từ lúc lập plan". |
| F7 ablation | Đã xử lý | `:277` và research `:139`: tối thiểu là rules so với Investigator dùng evidence và full-text; nhánh semantic là tùy chọn. Khớp với `:297`. |
| F8 prototype lệch enum | Đã ghi rõ (cách chấp nhận được) | UI `:163` liệt kê các chỗ rút gọn: queue 3 filter, stale thay cho invalidated, failed gộp vào Manual intervention, chưa click-through rollback/uncertain/no-op. Spec yêu cầu bản React theo enum ở `:178-180` và đủ filter ở UI `:58`. Mã prototype không đổi; đúng với tuyên bố "chỉ ghi chú". |

Follow-up phụ của round 2: bảng stack `:244` đã nhắc direct adapter. Connections trong prototype (`html:60`) vẫn ghi Akmatori, nhưng UI `:163` đã quy định bản thật hiển thị runtime đang chọn. Chấp nhận được.

## 2. Blocker còn lại

Không có.

## 3. Điểm lệch nhỏ, không chặn

- Author nói "threshold rules deploy/ explicit". Thực tế `deploy/` chỉ xuất hiện ở proposal `:159`. Research `:97` chưa nhắc. Không ảnh hưởng implementer vì proposal là hợp đồng chính.
- Research `:13` vẫn dùng tiêu chí mơ hồ "nếu contract đòi hỏi patch rộng". Proposal `:131` đã có ba tiêu chí khách quan. Nếu hai chỗ khác nhau thì theo proposal `:131`. Nên đồng bộ câu này khi viết decision log.

## 4. Về tradeoff runtime

Tôi giữ phản biện của round 2. Sau khi tắt direct K8s tool, Investigator chỉ đọc lại evidence CloudOps đã thu sẵn. Interface `analyze(run, evidence, knowledge)` ở `:131` cũng nhận evidence trực tiếp. Vì vậy, xét thuần kỹ thuật, direct adapter rẻ hơn và không cần patch identity binding.

Dù vậy, cách chọn của author vẫn bảo vệ được:

- Chi phí sai bị giới hạn ở hai ngày làm việc và một interface.
- Tiêu chí chuyển adapter là nhị phân và có hiệu lực ngay khi hết timebox.
- UI, planner và quyền executor không đổi khi chuyển adapter.
- Author ghi rõ đây là tradeoff đã chấp nhận, không giả định Akmatori là bắt buộc (`author-response-round2.md:15`).

Rủi ro trễ ngầm mà round 2 lo đã được chặn.

## 5. Các gate implementation còn mở (không chặn bản kế hoạch)

- Spike 22–23/10 qua đủ ba tiêu chí: Gemini qua pinned pi SDK, typed result, trusted incident identity. Nếu không qua thì chuyển adapter ngay.
- Quota và tier Free thực tế của tài khoản. Kiểm tra stack 4 vCPU/8 GiB. Lock dependency và image sau khi build.
- Ngưỡng post-check (5/3 phút, coverage 90%) là giá trị thiết kế, cần hiệu chỉnh trên lab.
- Bản React phải theo enum và đủ queue filter, cùng các luồng rollback/uncertain/no-op mà prototype chưa mô phỏng.
- Accessibility và contrast phải test trên bản React.
- Checkpoint 04/11 phải có estimate theo từng chặng. Mốc 30/11 vẫn là target, chưa phải cam kết.

## 6. Giới hạn kiểm chứng

- Tôi chỉ đọc tĩnh round3 (3 file Markdown, HTML, manifest) cùng review và response của round 2, không chạy gì.
- Tôi không tự tính hash. Hash HTML trong `manifest.json:16` khớp với giá trị author công bố, nhưng đó là đối chiếu giữa hai tài liệu chứ không phải kiểm hash.
- Kết luận "không đổi logic" dựa trên việc grep đối chiếu round2 và round3 ở các đoạn liên quan, cùng QA của bên khác. Tôi không diff toàn bộ file.
- Hành vi browser và harness lấy từ `verification-round2.md`, tôi chưa tự kiểm chứng.
