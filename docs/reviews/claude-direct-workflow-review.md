# Review xác nhận độc lập, vòng 6: APPROVE

Tôi duyệt bản round6. Không có P0, P1 hay P2 mới. Cả năm P2 của vòng 5 đã được xử lý trong tài liệu, riêng P2-5 được chấp nhận một phần với lý do hợp lý. Còn hai điểm P3 làm rõ, nên chốt khi viết spec state machine nhưng không chặn việc duyệt kế hoạch.

Phạm vi tôi đã đọc: toàn văn proposal, architecture decision và UI spec. Research và template HTML tôi đọc qua grep có chủ đích. Tôi đối chiếu round5 qua grep. Source Aurora tôi đọc trực tiếp ở `task.py:480-514` và `actions/executor.py:115-154`. Tôi không tính SHA256 so với manifest và không mở các link ngoài (Procrastinate, OpenAI reference).

## 1. Đối chiếu các phát hiện

| Phát hiện | Kết quả | Vị trí trong round6 | Nhận xét |
|---|---|---|---|
| P2-1 Thư viện queue trên PostgreSQL | Addressed | `ARCHITECTURE_DECISION.md:63`, `:66`; `RELEASE_PROPOSAL.md:132` | Bảng debate có thêm phương án này. Contract đã đủ: SKIP LOCKED, lease token kiểm ở heartbeat/checkpoint/result, unique key `(run_id, stage, attempt)`, test worker cũ quay lại muộn. Tài liệu ghi thẳng rằng lease DB không thu hồi được request đã gửi tới Kubernetes. Tradeoff được trình bày trung thực, không tự nhận là rẻ hơn thư viện. |
| P2-2 Không ghi lặp khi takeover | Addressed | `RELEASE_PROPOSAL.md:208`, `ARCHITECTURE_DECISION.md:82` | Takeover ở `executing` hoặc `rolling_back` chuyển sang `uncertain` và không tự ghi lại. Tài liệu nói rõ giá trị khớp không chứng minh actor hay nhân quả. Bỏ annotation `execution-id` cũng hợp lý vì không còn giả định quy kết. |
| P2-3 `uncertain` kẹt vĩnh viễn | Addressed, kèm P3-A | `RELEASE_PROPOSAL.md:212`, `:239`; `UI_UX_SPEC.md:157`, `:169` | Có deadline/attempt cap, sau đó chuyển `manual_intervention` với `outcome_unknown`. Operator resolve được, snapshot giữ trạng thái unknown, target vẫn khóa xuyên incident. Không có nút force-unlock, không phải sửa database. Thiết kế này không mâu thuẫn với `:192`, vì `manual_intervention` là trạng thái khác `uncertain`. |
| P2-4 Tham số provider | Addressed | `RELEASE_PROPOSAL.md:156` | `output_limit_param` chọn một trong ba: `max_tokens`, `max_completion_tokens` hoặc omit. `temperature` mặc định không gửi. Có fixture cho từng trường hợp. Chỉ omit khi endpoint tự giới hạn, và tài liệu ghi timeout không phải cost cap. |
| P2-5 Thay đổi config ngoài template | Partial, chấp nhận | `RELEASE_PROPOSAL.md:83`, `:174`; `UI_UX_SPEC.md:80` | Không xin quyền đọc Secret là lựa chọn đúng. Tài liệu ghi rõ `resourceVersion` hiện tại không chứng minh được lịch sử; dữ liệu thiếu ghi unverified; diff rỗng thì manual-only. Nếu restore có diff nhưng nguyên nhân thật là ConfigMap, post-check và rollback vẫn bắt được. Không cần thêm gì. |
| P3 baseline UI | Addressed | `UI_UX_SPEC.md:80`, `RELEASE_PROPOSAL.md:87` | |
| P3 `resourceVersion` đọc mới | Addressed | `RELEASE_PROPOSAL.md:207` | |
| P3 nguồn bắt buộc/tùy chọn | Addressed | `RELEASE_PROPOSAL.md:158` | |
| P3 cấu hình thứ hai miễn phí | Addressed | `RELEASE_PROPOSAL.md:158` | |
| P3 chọn client | Addressed | `RELEASE_PROPOSAL.md:156` (httpx) | |
| P3 handoff template | Addressed | `docs/cloudops-ui-template.html:52` có "Ruled out" và "Not checked"; `:55` cùng `UI_UX_SPEC.md:169` ghi thiếu snapshot preview là giới hạn của demo | |
| P3 `window.openai` | Rejected, hợp lý | `template.html:26`, `UI_UX_SPEC.md:169` | Template chạy trong host preview. Spec đã loại đoạn này khỏi bản React. |

## 2. Claim về dedup

Tôi kiểm lại trong source, author đúng:
- `sources-aurora/server/chat/background/task.py:491-504` dùng Redis `SET nx=True ex=1800` theo task ID. Nếu lỗi thì log "proceeding anyway", tức fail-open.
- `sources-aurora/server/services/actions/executor.py:129-140` dùng `pg_advisory_xact_lock` theo cặp `(action, incident)` trên đường dispatch action.

Review vòng 5 nói "dedup không thuộc Celery mà là advisory lock". Câu đó đúng một nửa. `ARCHITECTURE_DECISION.md:42` giờ đã tách đúng hai cơ chế. Có một followup tùy chọn: ghi thêm rằng dedup Redis là fail-open, vì đây chính là lý do không nên dựa vào nó.

## 3. Điểm P3 còn lại (không chặn)

**P3-A. Tiêu chí "worker/request cũ không còn ghi muộn" chưa đo được** (`RELEASE_PROPOSAL.md:212`, `UI_UX_SPEC.md:169`)
- Trigger: operator ghi manual verification nhưng không có quy tắc nào để chứng minh vế "không thể ghi muộn".
- Impact: khóa xuyên incident sẽ hoặc bị mở theo cảm tính, hoặc không bao giờ mở được.
- Fix nhỏ nhất: định nghĩa xác định, ví dụ thời gian từ heartbeat cuối vượt quá lease TTL cộng client request timeout cộng margin, và lần đọc target sau mốc đó do executor hiện hành thực hiện. Server tự kiểm vế này; operator chỉ xác nhận vế outcome.

**P3-B. Chưa có transition khi reconcile thành công trong budget** (`RELEASE_PROPOSAL.md:126`, `:194`, `:208`)
- `:208` chỉ nói rõ trường hợp không khớp. Trường hợp target khớp desired thì chưa ghi chuyển sang đâu.
- `:126` còn câu "reconcile uncertain write trước retry", dễ đọc thành có ghi lại sau reconcile.
- Fix: thêm một câu. Khớp desired thì `uncertain → verifying`, kèm cờ attribution unknown, và chạy post-check bình thường. Mọi trường hợp khác giữ unknown hoặc chuyển manual. Sửa `:126` thành "reconcile, không tự retry write".

## 4. Regression mới

Tôi không thấy regression nào. Scope không tăng: không thêm engine, không có admin console, không có workflow phê duyệt nhiều cấp. Cơ chế khóa xuyên incident chỉ là một ràng buộc trạng thái trên target, không phải hardening kiểu enterprise.

## 5. Khuyến nghị kiến trúc

Giữ workflow trực tiếp gồm FastAPI, PostgreSQL jobs, deterministic planner và executor có credential riêng. Debate trong `ARCHITECTURE_DECISION.md:58-72` đã công bằng, kể cả với phương án trung gian là queue trên PostgreSQL. Điều kiện xem lại ở `:66` cũng đúng: nếu module jobs bắt đầu cần scheduler hoặc queue tổng quát thì chuyển sang Procrastinate, không tự phát triển tiếp. Không cần generic tool loop.

## 6. Giới hạn kiểm chứng

- Tôi chưa tính hash so với manifest. Template HTML có nhiều dòng dài bị cắt khi grep, nên tôi chưa đọc hết.
- Tôi chưa mở link ngoài (Procrastinate, OpenAI Chat Completions reference, Gemini) nên chưa xác nhận nội dung của chúng.
- Chưa có gì được chạy thật: endpoint/model, crash hay hết lease của job, các luồng uncertain/manual/snapshot trên trình duyệt, footprint toàn stack. Đây là các gate ở bước implementation, không phải điều kiện để duyệt kế hoạch.
