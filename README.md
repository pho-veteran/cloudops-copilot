# CloudOps Copilot

Ứng dụng hỗ trợ vận hành Kubernetes: điều tra bằng evidence, đề xuất bước xử lý, duyệt hành động có giới hạn và xác minh kết quả.

**Trạng thái: proposal và UI/UX đang được kiểm chứng, chưa bắt đầu triển khai sản phẩm hoặc chia epic.** Repository hiện chứa nghiên cứu, quyết định kiến trúc, đặc tả và prototype minh họa. Review kiến trúc trước đó là baseline để tiếp tục phản biện, không phải xác nhận mọi thiết kế đã đóng.

## Định hướng đang giữ

- **UI:** React + Vite + TypeScript + Tailwind + shadcn/ui. Học phong cách Aurora: neutral surfaces, sidebar gọn, hierarchy rõ, evidence dễ đọc; thiết kế luồng CloudOps riêng.
- **Backend:** Python/FastAPI, workflow hữu hạn, PostgreSQL jobs/state và executor có credential riêng.
- **AI:** gọi OpenAI-compatible Chat Completions trực tiếp qua cấu hình endpoint/model; không khóa một hãng hoặc một model.
- **Reference:** Akmatori và Aurora là nguồn học thiết kế/tính năng; không chạy runtime của hai nền tảng.
- **Release:** một cluster, Deployment stateless trong allowlist; đủ luồng vận hành nội bộ, không chỉ một demo MVP và không mở rộng sang enterprise hardening.

## Phạm vi chức năng để tiếp tục review

| Nhóm | Baseline |
|---|---|
| Nhận vấn đề | Alertmanager intake, work queue, manual investigation |
| Điều tra | CrashLoop/startup failure, OOM, tải tăng, Pending, ImagePull; evidence/citations và giới hạn kết luận |
| Ra quyết định | Findings, hypotheses, ruled-out/unverified checks, plan diff, manual guidance |
| Phục hồi | Restore known-good template, memory profile, scale; approval, fresh checks, verification và rollback có điều kiện |
| Lifecycle | Acknowledge, notes, re-investigate, compare runs, resolve/reopen, handoff snapshot |
| Tài nguyên | KRR advisory với coverage/exclusions/export, chưa auto-apply |
| Vận hành app | Viewer/operator, connection health, stale/partial/error states, quota/budget |

Không dùng model để tự duyệt, tạo raw patch hoặc thực thi shell. Các điều kiện này cùng phạm vi chức năng vẫn phải được review về tính hữu ích, tính nhất quán và công triển khai.

## Hai luồng làm việc

| Issue | Trách nhiệm |
|---|---|
| [#1 · UI/UX: Aurora-inspired + shadcn/ui](https://github.com/pho-veteran/cloudops-copilot/issues/1) | Style, component system, interaction, prototype và walkthrough |
| [#2 · PROPOSAL: verify trước khi chia epic](https://github.com/pho-veteran/cloudops-copilot/issues/2) | Feature scope, contracts, architecture, assumptions và readiness |

Thay đổi ảnh hưởng cả hai phải được đối chiếu, không để prototype tự đổi domain contract. Hai issue đều mở để tiếp tục cải thiện; chưa có implementation epic.

Chỉ chuyển sang chia epic sau khi hoàn tất [gate sẵn sàng](docs/PROPOSAL_READINESS.md), ghi rõ version/commit đã duyệt và người dùng chốt hướng. Không tự tạo epic hoặc implementation backlog từ trạng thái hiện tại.

## Bắt đầu đọc

| Tài liệu | Vai trò |
|---|---|
| [Release proposal](CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md) | Baseline sản phẩm, feature scope, contracts và acceptance |
| [UI/UX spec](CLOUDOPS_COPILOT_UI_UX_SPEC.md) | Sitemap, từng màn hình, journeys, permissions và failure states |
| [Aurora → shadcn/ui design direction](docs/UI_DESIGN_DIRECTION.md) | Nguồn tham khảo hình ảnh/code, visual language và component mapping |
| [Architecture decision](CLOUDOPS_COPILOT_ARCHITECTURE_DECISION.md) | Debate direct workflow với Akmatori/Aurora/library |
| [Research](CLOUDOPS_COPILOT_RESEARCH.md) | Kiểm chứng nguồn, provider, KRR và giới hạn evidence |
| [Review decisions](CLOUDOPS_COPILOT_REVIEW_DECISIONS.md) | Lịch sử phản biện Claude, quyết định tiếp nhận/bác bỏ và điểm còn mở |
| [Proposal readiness](docs/PROPOSAL_READINESS.md) | Điều kiện để bắt đầu chia epic, tách khỏi release acceptance |

## Prototype hiện có

[Template tương tác](docs/cloudops-ui-template.html) là bản HTML minh họa để xem luồng, có dữ liệu demo và host-preview integration. Nó **chưa phải React/shadcn implementation**, chưa phải bản redesign theo Aurora và không kết nối backend/Kubernetes/model. Hướng redesign mới được ghi trong design direction và theo dõi ở issue UI/UX.

GitHub chỉ hiển thị source HTML. Có thể xem file bằng preview HTML trong công cụ làm việc; với trình duyệt thông thường, cần bọc fragment trong một trang HTML. Không có `npm run dev` hoặc server backend ở trạng thái repository hiện tại.

Bản shadcn tiếp theo phải chứng minh component usage và các tương tác thật bằng fixture; CSS giống shadcn không được tính là đã áp dụng thư viện. Prototype vẫn là công cụ verify thiết kế trước epic, không phải bắt đầu build toàn bộ app.

## Quy tắc làm việc tiếp

1. Nêu câu hỏi cần quyết định trong đúng issue; dẫn tới section tài liệu và evidence.
2. Phân biệt source-audited, demonstrated bằng prototype, measured bằng thử nghiệm và giả định chưa kiểm chứng.
3. Review → phản biện → sửa tài liệu/prototype → kiểm tra lại phần thay đổi. Giữ câu hỏi mở khi chưa đủ bằng chứng.
4. Các thay đổi proposal đáng kể tiếp tục qua Claude Code review; ghi candidate commit, findings, phản hồi và giới hạn review. Không dùng verdict của snapshot cũ để chứng nhận bản mới.
5. Chỉ chia epic khi feature/UX contracts đủ rõ, không có blocker thiết kế và người dùng xác nhận. Các phép đo runtime thuộc implementation/release gates, trừ spike hữu hạn thật sự cần để quyết định kiến trúc.

## Reference và provenance

- [Aurora](https://github.com/Arvo-AI/aurora), audit tại `5af24c46416ed1bc4eec057d88db0af60b6ed3c6`.
- Akmatori audit tại `4b9e984fbfb6818d3e5798228af1010c1fc0ddc8`; nguồn và kết quả trong research.
- [shadcn/ui cho Vite](https://ui.shadcn.com/docs/installation/vite) và [theming](https://ui.shadcn.com/docs/theming).

[Build plan ban đầu](CLOUDOPS_COPILOT_BUILD_PLAN.md) chỉ để đối chiếu lịch sử. Các lựa chọn Next.js/Akmatori runtime trong đó đã bị thay thế. Review và manifest trong `docs/reviews/` gắn với snapshot tại thời điểm review, không tự chứng nhận HEAD.

Chưa copy source/assets Aurora vào app và chưa chọn license phát hành cho CloudOps. Nếu reuse code sau này, ghi nguồn/file/commit và giữ notices tương ứng.
