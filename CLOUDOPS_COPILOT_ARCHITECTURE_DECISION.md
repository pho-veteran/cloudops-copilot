# Quyết định kiến trúc trực tiếp và phân tích Aurora

Ngày 21/10/2026. Quyết định hiện hành: **bỏ Akmatori runtime; không thay bằng Aurora runtime; CloudOps tự điều phối workflow hữu hạn và gọi OpenAI-compatible endpoint trực tiếp.** Akmatori và Aurora là nguồn học tính năng, UX và cách giải quyết vấn đề. Không fork cả app, không clone giao diện, không tự xây một agent framework tổng quát.

Quyết định này thay thế phương án “ưu tiên Akmatori, spike hai ngày rồi fallback” trong các review trước. Phạm vi release vẫn giữ năm nhóm chẩn đoán, ba action template, lifecycle/handoff, KRR advisory và frontend Vite.

## 1 Bằng chứng và cách nghiên cứu

Aurora được đọc tại commit [`5af24c46416ed1bc4eec057d88db0af60b6ed3c6`](https://github.com/Arvo-AI/aurora/tree/5af24c46416ed1bc4eec057d88db0af60b6ed3c6), Akmatori tại `4b9e984fbfb6818d3e5798228af1010c1fc0ddc8`. Hai nhánh GPT-6.1 Sol độc lập đọc Aurora: product/UI và backend/runtime/provider. Parent đối chiếu thêm prompt evidence/conclusion, artifacts, actions, hooks và deployment. Chỉ đọc source; không cài dependencies, chạy code upstream, dùng credential hay thử live các connector.

Phân biệt bốn mức: README quảng bá; code có đường thực hiện; cấu hình mặc định; hành vi runtime đã đo. Nghiên cứu này xác nhận ba mức đầu ở các phần được nêu, không tuyên bố mức cuối. Các số tiết kiệm thời gian trong README không được dùng làm business case đã chứng minh cho CloudOps.

## 2 Aurora là gì và tại sao đáng học

Aurora là một ứng dụng SRE tích hợp dựa trên LangGraph/LangChain, Flask, Celery và Next.js. Nó bao phủ investigation, alert intake, chat, cloud command execution, postmortem, artifacts, actions, graph và nhiều integrations. Có thể học như một mẫu kiến trúc sản phẩm lớn; không nên coi toàn repo là một thư viện orchestration nhỏ. [README tại commit](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/README.md), [deployment](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/docker-compose.yaml).

| Nhóm khả năng | Bằng chứng đã đọc | Điều rút ra cho CloudOps |
|---|---|---|
| Findings có nguồn | Citation extractor nối report với tool call/output; modal có command, timestamp, nội dung | Dùng evidence IDs và drawer nguồn gốc; không chỉ thêm hyperlink trang trí |
| Độ đầy đủ của điều tra | UI và prompt tách Ruled Out/Not Checked | Phải phân biệt giả thuyết có dữ liệu bác bỏ với nguồn chưa đọc được |
| Bước xử lý tiếp | NextStepsConsole chọn Run/Copy/Create PR theo loại gợi ý | Có hành động theo ngữ cảnh; CloudOps chỉ Approve template hoặc Manual guidance |
| Kết thúc incident | Resolve UX nối sang postmortem; server có persistence/version routes | Giữ resolution snapshot và handoff để người khác tiếp tục công việc |
| Artifacts | Store dùng chung logic upsert/version, khóa row khi cấp version | Version/provenance cần rõ; chưa cần một màn hình tài liệu do agent tự duy trì |
| Actions | Executor dispatch action thành background chat; scheduler dùng Celery | Học tách trigger/run/outcome; không nhập hệ thống automation vào ba action Kubernetes |
| Correlation | Có time-window, similarity, topology và recurrence | Không tự coi alert cùng thời gian là cùng nguyên nhân; release giữ dedup episode rõ ràng |
| Investigation prompt | Evidence standard, conclusion gate, alternatives và missing information | Chất lượng kết luận đến từ bằng chứng và giới hạn, không từ số tool calls |

Nguồn product: [citation extractor](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/background/citation_extractor.py#L295), [CitationModal](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/incidents/components/CitationModal.tsx#L88), [RuledOutConsole](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/incidents/components/RuledOutConsole.tsx#L57), [summarization prompt](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/background/summarization.py#L275), [NextStepsConsole](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/incidents/components/NextStepsConsole.tsx#L62), [postmortem persistence](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/routes/postmortem_routes.py#L241).

Nguồn backend: [artifact store](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/services/artifacts/store.py), [action executor](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/services/actions/executor.py), [scheduler](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/services/actions/scheduler.py), [time correlation](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/services/correlation/strategies/time_window.py).

## 3 Những phát hiện kiến trúc quan trọng

### LangGraph không đồng nghĩa workflow nghiệp vụ đã bền vững

Graph mặc định là START → agentic_tool_flow → END. Triage/parallel subagents/synthesis được bật bằng `ORCHESTRATOR_ENABLED`, mặc định false trong Compose. Node đơn này vẫn chứa agent tool loop phức tạp; không thể suy “một node” thành “chỉ một model call”. [Workflow](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/workflow.py#L104), [inner agent](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/agent.py#L678).

SafeMemorySaver kế thừa MemorySaver trong RAM; conversation/context history được lưu PostgreSQL qua đường riêng. Không được coi đây là bằng chứng graph-node checkpoint tự tiếp tục sau process restart. Celery/Redis xử lý task nền, timeout, late acknowledgement và dedup; đó là những lớp khác với lịch sử chat và approval/execution nghiệp vụ. [Checkpointer](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/utils/safe_memory_saver.py), [history](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/utils/llm_context_manager.py#L158), [background task](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/background/task.py#L442).

Compose được audit cấu hình Redis bounded memory/allkeys-lru, không gắn volume Redis hoặc bật AOF rõ ràng. Đây là nhận xét về cấu hình cụ thể, không khẳng định mọi deployment Aurora đều mất dữ liệu. CloudOps không sao chép cấu hình này làm cơ sở cho durable job. [Redis service](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/docker-compose.yaml#L395), [Celery](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/celery_config.py#L39).

Phân biệt hai đường dedup: background chat task tự đặt Redis SETNX theo task ID, không phải đảm bảo exactly-once do Celery cung cấp; actions executor dùng PostgreSQL advisory lock cho đường dispatch riêng. Không gộp hai cơ chế này. [Task dedup](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/background/task.py#L492), [action dispatch](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/services/actions/executor.py).

### Confirmation của chat khác với approval của một plan bất biến

Aurora có backend command gate, org policy và confirmation; không đúng nếu nói nó chỉ có nút UI mà không có safeguards. Tuy nhiên suggestion execution mở chat session rồi gửi yêu cầu thực thi command. Pending UI có persistence, còn waiter confirmation giữ trong process. Mô hình này khác approval gắn plan hash, TTL, target preconditions và executor cố định mà CloudOps cần. [SuggestionModal](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/incidents/components/SuggestionModal.tsx#L88), [confirmation](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/utils/cloud/infrastructure_confirmation.py#L29), [command gate](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/utils/auth/command_gate.py).

### Provider flexibility không miễn phí về độ phức tạp

Aurora có registry/provider classes, model mapping và nhiều model roles cho RCA, main chat, summarization, guardrails, recurrence và orchestrator. Cấu hình main model không đủ chứng minh mọi call đi cùng endpoint. Đây là năng lực hữu ích cho sản phẩm đa nhà cung cấp, nhưng quá rộng so với một client tương thích chung của CloudOps. [Registry](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/providers/__init__.py#L107), [model roles](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/llm.py#L43).

### Đừng mang cả deployment chỉ để dùng một vòng điều tra

Compose gồm API, chatbot, Celery worker/beat, Redis, PostgreSQL, Vault, SeaweedFS, Memgraph, MCP, frontend cùng integrations tùy cấu hình. Image còn chứa cloud CLIs, Terraform, kubectl và các công cụ phục vụ command execution. Không suy rằng mọi service đều bắt buộc cho mọi cấu hình, nhưng việc gỡ các phần không dùng là một dự án thu gọn dependency thực sự. [Compose](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/docker-compose.yaml), [Dockerfile](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/Dockerfile), [requirements](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/requirements.txt).

## 4 Debate các phương án

| Phương án | Lập luận mạnh nhất ủng hộ | Chi phí với phạm vi hiện tại | Quyết định |
|---|---|---|---|
| Giữ Akmatori runtime | Có worker/session/tool/provider plumbing; hữu ích nếu agent tự khám phá nhiều công cụ | CloudOps vẫn phải làm gần hết nghiệp vụ; thêm API/worker/gateway, DB, mapping ID và hai patch upstream | Bỏ khỏi release |
| Dùng Aurora làm nền | Có nhiều workflow sản phẩm, tool loop, UX thực và tích hợp đã phát triển | Mang thêm phạm vi chat/command/multi-cloud và nhiều dịch vụ; approval/state không tự khớp | Tham khảo, không fork làm nền |
| Dùng LangGraph từ đầu | Có lợi khi branching/tool loop/checkpoints phức tạp | Thêm abstraction khi flow chỉ có vài stage; vẫn phải thiết kế durable state/executor | Chưa cần |
| Thư viện queue trên PostgreSQL | Dùng lại claim/retry/worker mà không cần thêm broker, ví dụ Procrastinate | Vẫn phải thiết kế transaction approval, stage outputs và write reconciliation; thêm schema/API/library lifecycle | Đã cân nhắc; chưa thêm ở release này, giữ module jobs hẹp có contract/test cụ thể |
| CloudOps workflow trực tiếp | Một nguồn trạng thái, một Python codebase, contract riêng và model client đơn giản | Phải tự làm job lifecycle, context budget, validation/retry và tests; ít tự khám phá hơn | Chọn |

Queue library là phương án trung gian hợp lệ, không phải một agent runtime. [Procrastinate](https://procrastinate.readthedocs.io/en/stable/) dùng PostgreSQL cho tasks/locks; [tài liệu stalled jobs](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html) vẫn yêu cầu chính sách phát hiện/retry phù hợp. Chọn module riêng vì chỉ có vài loại job với transaction domain cụ thể; đây là tradeoff có chi phí kiểm thử, chưa có benchmark chứng minh ít công hơn thư viện. Contract claim/lease/fencing/stage idempotency và takeover được chốt ở proposal mục 4, write-uncertain ở mục 8. Nếu module bắt đầu cần scheduler/generic queues thì xem lại lựa chọn thư viện, không phát triển engine riêng.

**Phản biện mạnh nhất với lựa chọn trực tiếp:** nếu sau này cần một agent tự chọn hàng chục tools, khám phá lỗi chưa biết, song song nhiều chuyên gia và trò chuyện dài, tự viết tool pairing, cancellation, context trimming, provider quirks sẽ trở thành agent framework tự chế. Khi đó library/runtime có thể rẻ hơn. Không phủ nhận giá trị Akmatori/Aurora trong bài toán đó.

**Vì sao vẫn chọn trực tiếp:** release đã chốt năm nhóm triệu chứng, collection packs hữu hạn, một Investigator và ba template. Collector, evidence store, plan/approval/executor, UI và KRR đều thuộc CloudOps dù có dùng runtime ngoài. Sau khi bỏ direct cluster tools và general chat, phần runtime được tận dụng còn quá ít so với các lớp tích hợp phải duy trì. Lựa chọn đơn giản nhất là bỏ dependency thừa, không giảm luồng sản phẩm.

**Điều kiện mở lại quyết định:** evaluation chỉ ra fixed evidence packs bỏ lỡ một nhóm lỗi quan trọng mà thêm collector có scope không giải quyết được, hoặc nhu cầu exploratory branching đã được người dùng chọn vào scope. Khi đó thử một library/tool-loop có giới hạn bên trong Investigator. Chưa dựng sẵn plugin/adapter matrix hoặc một runtime dự phòng để chờ khả năng này.

## 5 Kiến trúc đơn giản đã chốt

Một codebase Python/FastAPI; PostgreSQL là nguồn trạng thái và hàng đợi bền vững. Ba entrypoint API, workflow worker và executor dùng chung image/module; executor có writer credential riêng. React/Vite là frontend độc lập. Prometheus/Alertmanager, Kubernetes và KRR giữ theo tính năng hiện hành.

Investigation: intake → claim job → scoped collect → context/retrieval → một model analysis → schema/citation validation → deterministic plan/manual guidance. Retry có giới hạn và tối đa một output repair. Missing evidence không kích hoạt terminal hoặc vòng agent tự mở rộng quyền.

Action: approved plan → durable job/outbox → fresh preconditions → conditional write → verification/reconciliation → outcome → operator resolve/handoff. Approval không giữ một HTTP request hoặc in-process waiter mở trong lúc chờ người dùng.

Takeover giữa write/rollback chuyển uncertain, không tự gửi lại write. Hết reconciliation budget chuyển manual intervention/outcome unknown; cho operator bàn giao incident nhưng giữ khóa target xuyên incident đến khi xác minh kết quả và loại trừ write muộn. Không biến thiếu kết quả thành success hoặc dùng resolve để bỏ khóa.

Jobs lưu stage, attempt, lease token/heartbeat, retry time và references trong DB; commit giữa stage, không giữ transaction qua network call. Kết quả attempt cũ bị từ chối nếu lease không còn hợp lệ. Model/read có thể retry có giới hạn; write uncertain phải reconcile. Cần thiết kế và kiểm thử đúng phần này, nhưng không biến nó thành workflow engine tổng quát.

Không còn Go API, TypeScript agent worker, MCP Gateway, pi SDK, upstream database, trusted upstream-ID forwarding patch hay machine-result patch. Bỏ spike giữ/bỏ Akmatori; thay bằng direct vertical-slice validation. Không có bằng chứng để hứa tốc độ/chi phí giảm bao nhiêu trước khi đo.

## 6 Áp dụng ý hay của Aurora với phạm vi nhỏ

1. **Completeness trong Findings:** thêm structured ruled-out hypotheses và unverified checks. Citation hợp lệ chưa tự chứng minh claim đúng; rubric phải kiểm tra coverage và supporting evidence. Timeout/403/thiếu metrics không được biến thành “đã loại trừ”.
2. **Decision preview truy xuất nguồn:** giữ lý do, target, diff, prerequisites, verification và citation trong một panel. Manual guidance hữu ích ngay cả khi không có action; không chuyển sang agent chat để thực thi lệnh tự do.
3. **Resolution snapshot:** bổ sung nội dung đã loại trừ/chưa kiểm tra vào handoff hiện có, lưu snapshot cùng reason lúc resolve. Không gọi model lần nữa chỉ để viết văn; không thêm postmortem editor, external export integration hoặc artifacts platform.

Ba điểm này là phần hoàn thiện cùng incident workspace, không thêm ba module sản phẩm. Giữ bốn navigation Work queue, Workloads, Knowledge, Connections. Progress là stage/data-source status thực; không cần live thoughts hay SSE ngay từ đầu.

Defer: topology/blast-radius graph, semantic recurrence/auto-merge, generated fix PRs, broad command tools, autonomous knowledge writes, scheduled actions builder, multi-cloud inventory, chat execution và persistent agent-maintained artifact workspace.

## 7 Những thứ không nên sao chép máy móc

Aurora có prompt yêu cầu số tool call/thời gian điều tra tối thiểu. CloudOps dùng tiêu chí đủ evidence và budget tối đa, không bắt model gọi thêm cho đủ số. Prompt kết luận cũng yêu cầu xem symptom đang xảy ra và cơ chế gây lỗi; học nguyên tắc này nhưng vẫn cho điều tra historical window, nêu rõ active versus historical. [Investigation skill](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/skills/core/investigation.md), [conclusion gate](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/prompt/rca_sections/conclusion_gate.md).

Không đọc thấy lỗi chỉ có giá trị khi nguồn/time window/coverage phù hợp. Không coi thiếu dữ liệu là bằng chứng lỗi không tồn tại. Không dùng prompt-based guardrails thay cho quyền, bounds hoặc schema validation. [Evidence standard](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/chat/backend/agent/prompt/rca_sections/evidence_standard.md).

Hooks là một ý hay để tập trung enforcement/usage, nhưng CloudOps chỉ cần hàm/module thông thường. Aurora hook defaults và error policies tùy loại; không copy hook fail-open làm kiểm soát bắt buộc của action. [Hooks](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/server/utils/hooks.py).

Root Aurora ghi Apache-2.0 và có danh mục third-party riêng. Lần này chỉ tham khảo thiết kế, chưa copy code/assets. Nếu sau này reuse source, ghi file/commit, giữ notices và kiểm tra license từng thành phần; không coi mọi dependency/asset đều theo root license. [LICENSE](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/LICENSE), [third-party](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/LICENSE-THIRD-PARTY.md).

## 8 Kiểm chứng quyết định khi triển khai

Giữ bộ evaluation release hiện tại; bổ sung check vào các case đã có: lỗi nguồn không thành ruled-out; handoff giữ snapshot trước reopen; worker restart/lease expiry không mất stage hoặc tạo duplicate action; invalid RCA không tạo plan. Walkthrough phải cho reviewer phân biệt được điều có bằng chứng, điều đã bác bỏ và điều chưa biết.

Endpoint protocol, chất lượng model, SQL job recovery, collector coverage, full-stack footprint và live recovery vẫn phải thử thực tế. Nghiên cứu source và planning review không thay thế các gate đó. Quyết định kiến trúc đã chốt; gate dùng để sửa implementation cụ thể, không tự khôi phục dependency Akmatori/Aurora.
