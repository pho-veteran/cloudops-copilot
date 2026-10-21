# APPROVE (cho bản sửa planning round4)

Tôi chỉ đọc các file trong round4 và đối chiếu template với round3. Tôi không chạy gì và không kiểm tra lại manifest hash hay kết quả Node vm.Script.

Ý của bạn đã được đáp ứng: hợp đồng hiện hành không còn khóa vào Gemini, Google hay một model cụ thể. Bản sửa cũng không làm phát sinh mâu thuẫn hay regression nào chặn approve.

## Đã kiểm tra

- **Contract provider** (`round4/CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md:156-162`):
  - Dùng OpenAI-compatible Chat Completions, cấu hình `base_url`/`api_key`/`model` phía server. Model ID là chuỗi cấu hình, không phải enum. Đã nêu rõ quy tắc version prefix, client không tự thêm `/v1`.
  - Baseline là non-streaming text. Không bắt buộc tool calling hay JSON Schema native. App tự validate RCAResult, sửa output tối đa một lần, sai tiếp thì failed và không tạo plan.
  - JSON mode chỉ bật khi đã được kiểm chứng. Không có provider router, không có SDK riêng cho từng hãng.
  - Chính sách chi phí tách khỏi protocol. Không có fallback trả phí, không đổi key/model để né quota.
- **Spike** (`:132`): gate Akmatori độc lập với đường chính. Fail một trong ba tiêu chí thì chuyển sang direct adapter, dùng chung RCAResult, UI, planner và quyền executor. Khớp với `:158` ("không là rào cản") và `:269`.
- **Embeddings** (`:176`, `:251`): optional, cấu hình endpoint/model/dimension/index riêng. Không suy ra từ chat endpoint. Đổi model/dimension thì re-index, không trộn vector space. Các chi tiết Embedding 2 được ghi rõ chỉ là profile tham khảo.
- **Stack** (`:249-250`): "không bắt buộc Google SDK hoặc model cụ thể".
- **Acceptance** (`:160`, `:269`): connection test tách "transport OK" khỏi "RCA contract OK". Một model qua được protocol nhưng trượt rubric mục 12 vẫn chưa đủ để release. Đổi endpoint/model tạo config version mới và phải chạy lại smoke/rubric. Đây đúng là tinh thần "tương thích không có nghĩa là mọi model như nhau".
- **Research** (`round4/CLOUDOPS_COPILOT_RESEARCH.md:5`): có banner gắn nhãn phần Gemini (`:61-69`) là case study. Các dòng `:15`, `:57`, `:147` đã đổi sang direct OpenAI-compatible adapter.
- **UI spec** (`round4/CLOUDOPS_COPILOT_UI_UX_SPEC.md:84`): endpoint label/model ID cấu hình phía server, không khóa Google/Gemini, embedding là kết nối riêng.
- **Template HTML** so với round3:
  - Chỉ đổi copy ở dòng 33, 60 và 70.
  - Nhãn `'Model endpoint'` ở dòng 60 khớp với predicate `d.test==='Model endpoint'` ở dòng 70, nên luồng announce không bị gãy.
  - Logic incident state, form và chart không đổi.
  - Hai chỗ match `window.openai` (dòng 26) là widget-state API, không liên quan provider.

## Blockers

Không có.

## Giới hạn không chặn approve

1. **Đường Akmatori với base_url tùy ý chưa có bằng chứng ở mức source.** Research `:40` mới chỉ xác nhận runner có mapping Google, chưa xác nhận pi SDK nhận được OpenAI-compatible base_url tùy ý. Gate ở `:132` đã xử lý bằng cách chuyển sang direct adapter khi fail. Nên ghi thêm một dòng vào cột "Hệ quả" của research `:40` rằng đây là điều cần chứng minh trong spike.
2. **"Ít nhất hai cấu hình endpoint/model"** (`:160`) có thể khó làm nếu chỉ có một đường miễn phí. Nên ghi rõ là cho phép dùng hai model trên cùng một endpoint, hoặc dùng một cấu hình live cộng một cấu hình fixture. Như vậy tránh mâu thuẫn với chính sách không phát sinh phí.
3. Research `:63-69` vẫn nói về Free Tier và format embedding của Gemini. Phần này đã được banner `:5` và proposal `:176` gắn nhãn, nên hợp lệ với vai trò case study, không phải lock-in.
