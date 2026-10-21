# Phản biện và quyết định cuối CloudOps Copilot

> Cập nhật sau vòng 6: người dùng yêu cầu tiếp tục verify proposal trước khi chia epic, với một issue UI/UX riêng và một issue PROPOSAL riêng. UI direction mới học Aurora và áp dụng shadcn/ui; xem [design direction](docs/UI_DESIGN_DIRECTION.md) và [readiness gate](docs/PROPOSAL_READINESS.md). Review vòng 6/manifest “final-candidate” bên dưới là snapshot lịch sử trước cập nhật này, không chứng nhận HEAD hiện hành hoặc bản redesign chưa xây.

Phạm vi: định hướng sản phẩm, kiến trúc, proposal và template UI/UX. Đây không phải xác nhận ứng dụng đã được xây dựng, triển khai hoặc đủ điều kiện production. Tài liệu hiện hành là [release proposal](CLOUDOPS_COPILOT_RELEASE_PROPOSAL.md); [kế hoạch ban đầu](CLOUDOPS_COPILOT_BUILD_PLAN.md) được giữ để đối chiếu.

## Quyết định mới nhất: workflow CloudOps trực tiếp

Theo yêu cầu debate lại runtime và nghiên cứu Aurora, **bỏ Akmatori runtime, không nhận Aurora làm runtime thay thế**. Chọn state machine hữu hạn trong codebase Python, PostgreSQL jobs/lease/outbox và OpenAI-compatible client; executor riêng credential. Bỏ spike ưu tiên Akmatori, gateway/typed-result patches và upstream ID mapping. [Báo cáo Aurora và quyết định kiến trúc](CLOUDOPS_COPILOT_ARCHITECTURE_DECISION.md) là phân tích mới nhất, thay thế các tradeoff runtime ở vòng 1–4 bên dưới.

Học ba điểm trong phạm vi incident workspace: phân biệt đã loại trừ/chưa kiểm tra, decision preview có evidence, resolution handoff snapshot. Không thêm chat execution, graph, artifacts platform, general workflow engine hoặc multi-agent orchestration. Hai audit GPT-6.1 Sol độc lập cùng khuyến nghị phương án trực tiếp sau khi đọc Aurora tại commit đã pin. Phản biện mạnh nhất được giữ: tự viết tool-loop tổng quát sẽ là false simplicity; scope hiện tại cố ý không xây phần đó.

## Cập nhật provider theo yêu cầu người dùng

Sau vòng 3, người dùng làm rõ: chỉ cần OpenAI-compatible endpoint, không khóa Gemini. Đã thay direct Gemini adapter bằng direct OpenAI-compatible adapter và bỏ yêu cầu Google SDK, model ID cố định, dimension/format embedding cố định. Cấu hình inference gồm base URL, credential và model; Chat Completions non-streaming là baseline. Tool calling/JSON Schema là capability bổ sung, không điều kiện bắt buộc cho direct adapter; app chuẩn bị context và validate RCAResult. Embedding là cấu hình riêng, full-text vẫn là baseline. Connections/template dùng nhãn provider chung. Chi phí/quota vẫn theo chính sách cấu hình, không tự fallback phát sinh phí.

Các review vòng 1–3 dưới đây là lịch sử của candidate trước thay đổi này; không tự suy ra verdict cũ bao phủ contract provider mới. Vòng xác nhận bổ sung được ghi ở cuối tài liệu.

## Cách review

Ba nhánh GPT-6.1 Sol nghiên cứu Akmatori, provider và phạm vi sản phẩm; một nhánh QA độc lập kiểm tra template. Claude Code CLI được gọi trong các session mới, chỉ có Read/Glob/Grep, không có quyền sửa, shell, MCP hoặc gọi agent. Model yêu cầu là `claude-opus-5.5`; kết luận chỉ được ghi nhận sau khi kiểm tra model metadata, process exit, result và permission denials.

Vòng đầu đọc research, release proposal và UI/UX spec. Vòng xác nhận đọc thêm template tương tác, findings cũ, phản hồi của tác giả và bằng chứng kiểm tra. Candidate được copy riêng và đối chiếu SHA-256 để tránh review nhầm bản.

## Lịch sử phản biện vòng 1–3

Bảng và debate dưới đây ghi quyết định tại thời điểm review. Các phương án upstream/gateway/spike được thay thế bởi workflow trực tiếp ở đầu tài liệu; không còn là yêu cầu triển khai hiện hành.

| Vấn đề | Quyết định và thay đổi |
|---|---|
| Evidence không có đường thu thập và citation rõ | Collector CloudOps đăng ký Kubernetes/Prometheus snapshots; agent chỉ đọc evidence đã có ID, không dùng direct Kubernetes tool trong profile release |
| Restore cần known-good nhưng thiếu cách tạo | Thêm Workload detail → Confirm known-good, health/stability check, snapshot/hash preview và API xác nhận |
| Static connector token chưa ràng buộc run | Bắt buộc trusted upstream incident context được gateway chuyển tiếp, CloudOps map server-side; thiếu binding thì từ chối |
| Tiêu chí phục hồi lẫn với benchmark | Tách rollout Ready, telemetry verification và điều kiện tải; thiếu coverage/tải trả inconclusive, không nhận công lao phục hồi |
| Alert chưa có mapping, dedup và giới hạn gọi AI | Bổ sung owner-reference mapping, episode dedup, một initial analysis tự động, queue cap/deferred và threshold rules |
| Rollback chưa rõ ai khởi động | Operator xem inverse diff và xác nhận; backend ghi approval/audit, kiểm tra trạng thái và safety lại |
| Runbook có nguy cơ hai catalog | Một nguồn repository và một CloudOps index; tắt native searcher song song |
| Bộ đánh giá thiếu pass criteria | Chốt recovery, diagnostic-only, negative và contract failure cases; tách model quality khỏi tính đúng đắn của thao tác ghi |
| KRR vượt budget lịch sử | Profile lab bắt đầu 6 giờ, coverage/gaps rõ; mở rộng cửa sổ chỉ khi budget cho phép |
| Lifecycle lẫn trạng thái | Tách Incident/Run/Plan/Execution; resolution vô hiệu plan chưa dùng, không che execution đang chạy hoặc uncertain |

## Những điểm đã debate ở vòng 1–3

**Giữ Akmatori hay gọi model trực tiếp.** Claude phản biện rằng chi phí adapter/patch có thể lớn hơn lợi ích khi CloudOps đã sở hữu evidence và planning. Chấp nhận phản biện: chỉ ưu tiên Akmatori sau spike hai ngày; direct adapter là phương án chính thức nếu contract không đạt. Tên direct Gemini adapter trong review cũ đã được thay bằng OpenAI-compatible adapter theo yêu cầu người dùng. Lịch dự kiến 22–23/10, ba gate phải cùng đạt; hết timebox thì chuyển ngay nếu còn gate lỗi, không đợi 04/11. UI, RCAResult và executor không phụ thuộc quyết định này.

**Giữ feature scope hay ép deadline.** Không tự bỏ restore/scale hoặc KRR để giữ mốc cũ. Yêu cầu mới nhất ưu tiên chốt release đủ luồng. Thêm checkpoint 04/11, thứ tự giảm công và quy tắc công bố forecast mới/đề xuất đổi scope nếu vẫn không đủ. Không hứa chắc ngày 30/11 khi chưa có estimate thực tế.

**UI đầy đủ đến đâu.** Giữ manual investigation, citations, decision preview, re-investigation, snapshot compare và handoff. Compare chỉ cần hai snapshot; handoff dùng template. Không thêm general chat, workflow builder, topology graph, collaborative editor hoặc mobile app riêng. Responsive dùng cùng giao diện; không giấu tiêu chí approval ở màn hình nhỏ.

**Kiểm soát vừa đủ.** Approval, conditional write, role/scope, duplicate prevention, audit và reconciliation là tính đúng đắn của ba thao tác ghi. Không mở rộng sang SSO, policy DSL, HA, multi-tenancy, compliance hoặc audit ledger bất biến.

## Kiểm tra template

QA độc lập kiểm tra state isolation giữa incident, execution guards, plan invalidation, run history, input escaping, partial evidence và HPA exclusions. Một lỗi chỉ lộ ra trong browser sandbox: form submit không hoạt động. Đã thay bằng explicit button dispatch qua shared validation và kiểm tra lại.

Browser walkthrough xác nhận review/approve → executing → verifying → succeeded → resolve có reason; điều tra thủ công tạo run mới. Evidence có biểu đồ demo với đơn vị, nguồn và time window; missing data không sinh số liệu giả. Kiểm tra responsive ở desktop/tablet và kích thước hẹp 390/320px, không thấy horizontal overflow trong vùng đã kiểm tra. Đây là kiểm tra tập trung, chưa là chứng nhận accessibility hoặc kiểm thử mọi route của frontend thực.

## Những bằng chứng còn phải thu khi triển khai

Endpoint protocol và chất lượng RCA của model, quota/tier thực, SQL job recovery, collector coverage, resource usage toàn stack, chi phí instance và các lượt recovery live vẫn là capability gates. Không coi source audit hoặc demo UI là bằng chứng các gate này đã đạt. Không còn gate pi SDK, upstream binding patch hoặc typed-result endpoint. Frontend acceptance còn gồm focus/keyboard, contrast, deep links, session expiry và server-driven conflicts.

## Các sửa đổi sau vòng hai

Claude xác nhận ba blocker ban đầu đã được xử lý, nhưng phát hiện một mâu thuẫn văn bản còn sót: bảng action và copy UI dùng điều kiện tải tương đương của benchmark cho post-check sản phẩm. Đã sửa cả bảng và đúng ba đoạn copy UI thành telemetry mới, ngưỡng metric version hóa và coverage; tải giảm/thiếu coverage dẫn đến inconclusive. QA độc lập xác nhận chỉ thay copy, không thay logic template đã được kiểm tra.

Các góp ý không chặn cũng được xử lý: deadline và pass/fail cho spike; confirm baseline trước fault injection; deferred là queued/capacity; rollback expiry riêng; trường phải không đổi kể từ lúc lập plan; ablation semantic tùy chọn. UI/UX spec ghi rõ những giản lược của demo để implementation React không sao chép nhầm enum/filter hoặc coi mockup đã có rollback/uncertain/no-op click-through.

Nội dung review gốc: [vòng 1](docs/reviews/claude-round1.md), [vòng 2](docs/reviews/claude-round2.md). [Manifest vòng 3](docs/reviews/round3-candidate-manifest.json) ghi hash của ba tài liệu và template được gửi xác nhận lúc đó.

## Kết quả vòng 3 trước cập nhật provider

**APPROVE cho bản kế hoạch, không còn blocker.** Claude Code đã thực hiện ba vòng: vòng 1 CHANGES_REQUESTED; vòng 2 CHANGES_REQUESTED vì mâu thuẫn post-check còn sót; vòng 3 APPROVE sau sửa. [Review cuối nguyên văn](docs/reviews/claude-final-review.md) đối chiếu F1–F8 và nêu giới hạn kiểm chứng.

Cả ba vòng đều hoàn tất process exit 0, result success, is_error=false, không có permission denial; tất cả assistant model metadata đều là `claude-opus-5.5`. Codex đối chiếu SHA-256 của bốn file tại thời điểm vòng 3, đều khớp [manifest vòng 3](docs/reviews/round3-candidate-manifest.json). Claude tự review tĩnh; kiểm browser/harness do Codex và QA độc lập thực hiện, không gán các kiểm tra đó cho Claude.

Hai nhận xét biên tập không chặn được làm rõ tại đây: threshold rules ở `deploy/` theo proposal mục 5.3; câu “patch rộng” trong research chỉ mô tả mối lo tích hợp, còn tiêu chí quyết định runtime có hiệu lực là ba gate và hạn kết thúc ở proposal mục 4. Khi đọc research cùng proposal, áp dụng contract cụ thể trong proposal. Những khác biệt này không làm thay đổi feature scope hoặc verdict.

Định hướng bàn giao: Vite với UX riêng; một Investigator và deterministic planner; năm nhóm chẩn đoán, ba hành động có phê duyệt, lifecycle/handoff hoàn chỉnh và KRR advisory. Runtime được quyết định qua spike hữu hạn. Hoàn tất nghiên cứu và thiết kế không đồng nghĩa đã triển khai app hay vượt các capability gates.

## Vòng 4: xác nhận thay đổi contract OpenAI-compatible

**APPROVE, không có blocker** cho thay đổi provider theo yêu cầu người dùng tại vòng 4. [Review contract](docs/reviews/claude-provider-contract-review.md) xác nhận không còn khóa Gemini, JSON Schema/tool calling không bắt buộc ở direct adapter, embeddings tách riêng và copy/template đồng bộ. Model metadata duy nhất `claude-opus-5.5`; exit 0, result success, is_error=false, không có permission denial, stderr rỗng. Bốn file tại thời điểm đó khớp [manifest vòng 4](docs/reviews/round4-candidate-manifest.json); verdict này không tự bao phủ thay đổi kiến trúc tiếp theo.

Hai lưu ý không chặn: arbitrary base URL qua Akmatori/pi SDK chưa được source audit chứng minh và phải qua spike, nếu không đạt thì dùng direct adapter; yêu cầu hai cấu hình endpoint/model không bắt buộc hai nhà cung cấp hay tài khoản trả phí, có thể kiểm chứng hai model trên cùng endpoint miễn phí. Không có hai cấu hình phù hợp thì ghi thiếu acceptance evidence, không tự mua dịch vụ. Protocol compatibility vẫn tách biệt với chất lượng RCA.

Kiểm tra lần này: đọc lại các chỗ thay đổi, tìm các ràng buộc Gemini/Google còn sót, kiểm tra link tài liệu và parse JavaScript bằng Node vm.Script. Chỉ copy/provider label và predicate khớp nhãn thay đổi trong template; không đổi form, incident state hay chart. Không tuyên bố đã gọi live endpoint hoặc kiểm chứng mọi model.

## Vòng 5: debate lại runtime và nghiên cứu Aurora

[Claude vòng 5](docs/reviews/claude-direct-workflow-round5.md) **APPROVE** hướng workflow trực tiếp, đồng thời nêu năm điểm P2 nên làm rõ trước chặng recovery. Metadata model duy nhất `claude-opus-5.5`, exit 0, result success, is_error=false, không permission denial, stderr rỗng. [Manifest vòng 5](docs/reviews/round5-candidate-manifest.json) là snapshot trước các chỉnh sửa dưới đây.

| Phản biện | Xử lý trong candidate tiếp theo |
|---|---|
| Thiếu lựa chọn queue library trên PostgreSQL | Thêm phương án Procrastinate vào debate, đọc tài liệu chính thức; giữ module jobs hẹp, chốt claim/lease/checkpoint/unique stage output và kiểm thử takeover; không khẳng định tự viết rẻ hơn khi chưa đo |
| Takeover giữa lúc write chưa rõ | Executing/rolling_back bị tiếp quản → uncertain, không tự gửi lại write; đọc fresh UID/fields và không suy state trùng là bằng chứng actor/causality |
| Uncertain có thể kẹt incident | Reconcile có budget; hết budget → manual intervention/outcome unknown. Operator bàn giao/resolve có reason nhưng target vẫn khóa xuyên incident đến khi xác minh outcome và loại trừ late write |
| Tham số output không đồng nhất | Cấu hình tên output-limit param hoặc omission đã được xác nhận giới hạn endpoint; mặc định không gửi temperature; fixtures riêng, httpx transport |
| Config ngoài template gây CrashLoop | Ghi reference/metadata được phép, không mở quyền Secret; thiếu history là unverified; restore không hoàn tác config ngoài template, diff rỗng trả manual-only |

Các góp ý P3 được làm rõ: baseline age/source, fresh resourceVersion, nguồn context bắt buộc/optional, hai cấu hình không tự phát sinh phí, handoff có ruled-out/unverified và giới hạn snapshot demo. API host preview không mang sang React.

Một nhận xét source của Claude được phản biện lại: Aurora có cả Redis SETNX dedup trong background chat task (`task.py:492–504`) và PostgreSQL advisory lock trong action dispatch (`executor.py:129–140`). Chúng là hai đường khác nhau; không bỏ bằng chứng Redis vì review nhắc PG. Decision record đã ghi rõ cả hai và không gán exactly-once cho Celery.

Audit độc lập GPT-6.1 Sol đóng hai lỗi research còn sót (upstream runtime compatibility và GET connector) sau sửa. Các lần kiểm tra này là source/document QA. Template chỉ đổi copy/nhãn và nội dung handoff; JavaScript parse đạt, không thay handlers/charts/state. Không gán browser test cũ cho chức năng snapshot hoặc uncertain chưa được mô phỏng.

## Vòng 6: xác nhận candidate cuối hiện hành

**APPROVE; không có P0/P1/P2 mới.** [Review cuối nguyên văn](docs/reviews/claude-direct-workflow-review.md) xác nhận năm P2 đã được xử lý, trong đó yêu cầu đọc Secret metadata chỉ được tiếp nhận một phần có chủ đích: không mở quyền đọc Secret. [Phản hồi tác giả](docs/reviews/author-response-direct-workflow.md) giữ cả điểm tiếp nhận lẫn phản biện nguồn. Claude đọc lại hai đường dedup và xác nhận correction của tác giả.

Model metadata duy nhất `claude-opus-5.5`; process exit 0, result success, is_error=false, không permission denial, stderr rỗng. Codex đối chiếu SHA-256 năm file hiện hành với [manifest cuối](docs/reviews/final-candidate-manifest.json), tất cả khớp. Claude xác nhận bằng đọc tĩnh: toàn văn proposal/architecture/UI spec, grep có chủ đích research/template; không tuyên bố reviewer đã đọc hết mọi dòng HTML dài hoặc chạy kiểm thử. Audit GPT-6.1 Sol riêng kiểm tra các contract mới, không thấy mâu thuẫn chặn kế hoạch.

Hai follow-up P3 phải chốt trong implementation state-machine spec và kiểm thử trước release:

1. **Tiêu chí kết thúc rủi ro ghi muộn:** hệ thống cần điều kiện kiểm chứng được để mở khóa target sau unknown outcome; operator không chỉ xác nhận cảm tính. Gợi ý timeout/TTL trong review là điểm xuất phát, không phải bằng chứng đủ rằng request phía Kubernetes đã kết thúc. Phải tính cả worker cũ, request đã gửi và hành vi server/network; chỉ hết client timeout không đủ. Test delayed worker/request là gate; không có bằng chứng thì giữ khóa và hướng dẫn thao tác vận hành.
2. **Transition reconcile thành công:** ghi rõ nhánh target đạt desired → verification với attribution unknown, vẫn cần telemetry/post-check; không quy công cho action khi thiếu chứng cứ. Câu “trước retry” ở mục 4 không cấp quyền gửi lại write: quy tắc cụ thể ở mục 8 cấm automatic repeat khi takeover/write uncertain. Chốt transition và acceptance khi triển khai, không lấp khoảng trống bằng auto-retry.

Các follow-up này không yêu cầu đổi runtime, thêm feature hay framework. Quyết định cuối là Vite + FastAPI, workflow hữu hạn + PostgreSQL jobs, direct OpenAI-compatible client và scoped executor. Năm nhóm chẩn đoán, ba action, lifecycle/handoff, KRR và template/UI flows vẫn thuộc release. Hoàn tất nghiên cứu/thiết kế và được duyệt kế hoạch không đồng nghĩa app đã chạy hoặc đã qua live gates.
