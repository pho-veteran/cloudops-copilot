# Gate trước khi chia epic

Trạng thái hiện tại: **đang review**. Mục tiêu là proposal đủ rõ và hợp lý để chia công việc; không đòi hỏi xây xong app mới được lập epic. Hai issue UI/UX và PROPOSAL phải có kết luận liên kết với cùng một candidate commit.

## Cần đạt trước khi chia epic

- [ ] Người dùng và công việc chính rõ; release scope/in-scope/out-of-scope có lý do, không gọi thiếu workflow là MVP đủ dùng.
- [ ] Năm diagnostic families và ba action templates được đánh giá về giá trị, evidence cần có, manual fallback và chi phí triển khai.
- [ ] UI journeys bao phủ các feature; prototype shadcn theo Aurora direction được walkthrough và feedback đã xử lý hoặc defer có lý do.
- [ ] Incident/run/plan/execution/resolution states không mâu thuẫn; permissions và dữ liệu mỗi bước rõ.
- [ ] Các câu hỏi thiết kế đang mở có quyết định: reconcile-success transition/attribution; tiêu chí có thể kiểm chứng để gỡ target lock sau unknown outcome. Chỉ hết timeout không được coi là đủ.
- [ ] OpenAI-compatible contract, model quality, context budget, errors/retry và chính sách chi phí rõ; không yêu cầu Gemini/native tool calling.
- [ ] Direct workflow/PG jobs/credential boundary đủ để triển khai; không vô tình dựng agent framework hoặc generic scheduler.
- [ ] Feature-to-acceptance mapping đủ: normal/negative/manual/failure paths, UX states và giới hạn benchmark.
- [ ] Dependencies, effort estimate và rủi ro lịch được rà lại. 30/11 là target lịch sử phải forecast, không là bằng chứng đủ thời gian.
- [ ] Không còn blocker thiết kế có trigger/impact cụ thể. Review tĩnh độc lập/Claude trên candidate mới và phản hồi được ghi lại; verdict cũ không tự áp dụng.
- [ ] Người dùng chốt candidate commit là “ready for epic breakdown”; ghi câu hỏi mở có owner/tiêu chí xử lý ở giai đoạn implementation.

## Không nhầm với release gate

Chưa cần toàn bộ recovery live, SQL crash tests, footprint benchmark hay deploy production để chốt kế hoạch. Tuy nhiên proposal phải chỉ ra cách kiểm chứng, pass/fail và tác động nếu không đạt. Spike hữu hạn chỉ chạy trước epic khi thiếu bằng chứng khiến không thể chọn giải pháp; không dùng spike làm lý do trì hoãn mọi quyết định.

Trong implementation/release vẫn phải kiểm endpoint/model thực, collector coverage, job restart/late worker, approval/write/reconciliation, backup/restore, KRR và usability trên app thật. Prototype và source review không thay thế những kiểm tra đó.

## Cách ghi một vòng verify

Ghi trong đúng issue: candidate commit → câu hỏi/findings → evidence → quyết định accept/reject/defer → tài liệu thay đổi → kiểm tra lại → câu hỏi còn mở. Findings phải có trigger và impact; không mở rộng scope chỉ vì framework tham khảo có thêm tính năng.

Issue UI/UX giữ visual/interaction decisions. Issue PROPOSAL giữ feature/architecture/domain decisions. Một thay đổi chạm cả hai phải cross-link và được đối chiếu; khi đóng hai issue, liên kết tới cùng baseline được chốt. Sau đó mới lập epic và acceptance của từng epic.
