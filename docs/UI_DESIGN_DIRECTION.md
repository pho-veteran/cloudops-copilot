# CloudOps UI: học Aurora, áp dụng shadcn/ui

Trạng thái: design direction để review tiếp, ngày 21/10/2026. Prototype React/Vite tại apps/web hiện dùng shadcn/ui thật và là bản trải nghiệm chính để tiếp tục review. Prototype HTML cũ chỉ giữ làm lịch sử.

## Nguồn đã quan sát

Aurora được pin tại `5af24c46416ed1bc4eec057d88db0af60b6ed3c6`. Đã xem hai ảnh trong repository và đối chiếu source CSS/component. Ảnh minh họa giao diện tại thời điểm tác giả chụp, không chứng minh runtime hiện tại hoạt động đúng. Không chạy code upstream.

- [Incidents dashboard screenshot](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/.github/assets/incidents-dashboard.png): dark canvas, sidebar hẹp, danh sách incident có phân cấp title/metadata, severity màu tập trung.
- [Incident summary screenshot](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/.github/assets/incident-summary.png): header có trạng thái rõ, phần đọc dài yên tĩnh, inline citations và next steps.
- [Theme tokens](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/globals.css#L6), [sidebar](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/components/navigation.tsx#L108), [incident metadata](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/src/app/incidents/components/IncidentCard.tsx#L284).
- [components.json](https://github.com/Arvo-AI/aurora/blob/5af24c46416ed1bc4eec057d88db0af60b6ed3c6/client/components.json): Aurora cũng có cấu hình shadcn kiểu new-york/neutral/CSS variables/Lucide. CloudOps không sao chép config Next.js/RSC hoặc phiên bản component cũ.

## Visual language cho CloudOps

**Một workspace vận hành gọn, trung tính, đọc evidence dễ, hành động rõ.** Học cách Aurora giảm nhiễu thị giác; không lấy logo, tên màn hình, chat history hoặc toàn bộ layout của họ.

| Trục thiết kế | Quyết định để prototype |
|---|---|
| Theme | Dark là reference chính; light theo cùng semantic tokens, không hardcode dark trong từng component |
| Surfaces | Canvas, sidebar và panel khác nhau nhẹ; border 1px tinh tế; tránh gradient, glow, card lồng nhiều lớp |
| Navigation | Sidebar khoảng 224px trên desktop, route active có nền nhẹ; giữ đúng Work queue / Workloads / Knowledge / Connections |
| Nội dung | Queue dày vừa phải; incident có reading column và Next step rail, evidence mở theo ngữ cảnh |
| Typography | Sans 14–16px cho nội dung, metadata ít nhất 12px; monospace cho resource IDs/logs/diff; heading không lấn át dữ liệu |
| Màu | Primary trung tính tương phản rõ; blue cho citation/focus; amber/đỏ/xanh theo attention/error/verified outcome, luôn có text/icon |
| Hình khối | Radius nền tảng khoảng 8px, spacing 4/8px; điều chỉnh bằng tokens sau walkthrough |
| Interaction | Một primary action theo state; controls secondary/ghost; focus visible, hover nhẹ, reduced-motion |
| Mobile | Sidebar → Sheet/menu; Next step xuống dưới nội dung; không giấu diff/eligibility/approval khi ở 320px |

Không dùng xanh lá chỉ vì analysis đã hoàn tất. Incident status, investigation status và execution outcome phải phân biệt. Màu severity không được che freshness hoặc hành động đang chờ.

## Áp dụng shadcn/ui

Frontend đã chọn React/Vite/TypeScript + Tailwind + shadcn/ui. Dùng component source từ một preset/version được pin, có `components.json`, alias `@/`, CSS variables và Lucide; không coi một tập CSS bắt chước button là shadcn. Chọn một bộ primitives nhất quán khi dựng prototype, không trộn Base/Radix tùy hứng hoặc port `next/*` từ Aurora.

Theo [hướng dẫn Vite](https://ui.shadcn.com/docs/installation/vite), component được thêm vào project để ứng dụng sở hữu và điều chỉnh. Theo [theming](https://ui.shadcn.com/docs/theming), dùng semantic tokens để đổi theme, không sửa từng class rời rạc. Prototype `apps/web` đã pin dependencies trong package-lock.json, dùng preset Radix Nova (CLI shadcn 4.22.1), semantic variables và Lucide. Build/typecheck được chạy cho snapshot hiện tại; xem walkthrough và review log để biết phạm vi đã kiểm chứng.

| CloudOps surface | shadcn building blocks | Hành vi domain cần giữ |
|---|---|---|
| App shell | Sidebar, SidebarMenuButton, Breadcrumb, Separator, DropdownMenu | Bốn khu vực, active route, role, cluster/scope, theme |
| Work queue | Table, Input, Select, Badge, Button, Skeleton | Filters/search, thứ tự attention, freshness, empty/error, mở incident |
| Incident header | Badge, Button, Separator | Severity có nguồn; status riêng; acknowledge/re-investigate/resolve có guards |
| Findings / Activity / Compare | Tabs, Card, Collapsible/Accordion | Evidence-backed claims; ruled-out/unverified; snapshot history |
| Evidence | Sheet, Table, Tabs, Tooltip | Citation đúng snapshot, source/window/coverage; logs và metrics có giới hạn |
| Next step | Card, Badge, Button | Target, before/after, prerequisites, expected verification, manual-only reason |
| Approve / reject / resolve | AlertDialog hoặc Dialog, Label, Textarea, Alert | Version/TTL, reason, pending/conflict, focus return; preview resolution snapshot |
| Manual investigation | Dialog/Sheet, Input, Select, Textarea, Label | Scope validation, time window, symptom/question, inline errors |
| Workloads / Knowledge / Connections | Table/Card, Badge, Tabs, Alert | Known-good, recommendations exclusions, source versions, integration health |
| Feedback | Alert, Skeleton, toast/Sonner khi phù hợp | Lỗi quan trọng ở inline/banner; không chỉ toast rồi mất |

Chart wrapper có thể dùng shadcn Chart, nhưng dữ liệu/window/units/gaps là contract của Evidence. Không thêm biểu đồ trang trí hoặc model thought stream. Status colors của domain được khai báo thành tokens bổ sung; không lạm dụng variant destructive cho mọi trạng thái xấu.

## Prototype cần review trong issue UI/UX

1. Queue → incident → citation → decision diff → approve → verification → resolve/handoff.
2. Workloads → confirm known-good khi healthy → manual investigation → re-investigate → compare.
3. Partial evidence / quota / stale plan / denied / failed verification / uncertain → manual intervention.
4. Recommendations với insufficient/excluded và Connections có unavailable usage; không có zero giả.
5. Reopen giữ resolution snapshot, cross-incident target lock hiện rõ khi outcome unknown.

Prototype bằng fixtures có nhãn, không yêu cầu backend thật. Các guards và chuyển trạng thái phải theo proposal; phần chưa mô phỏng ghi rõ. Bản React/Vite tại `apps/web` là template chính, có 24 stories và các thao tác bằng fixture; hướng dẫn tại `PROTOTYPE_WALKTHROUGH.md`. Bản HTML cũ chỉ giữ làm lịch sử. User review vẫn là bước riêng sau verification kỹ thuật.

## Tiêu chí duyệt thiết kế

- Có reference board chỉ rõ học gì/từ đâu và phần nào thiết kế riêng; không copy assets/brand Aurora.
- Có component/token map và component shadcn thực sự trong prototype React/Vite, không chỉ screenshot.
- Walkthrough desktop khoảng 1440/1024px, tablet và 390/320px; dark/light, keyboard, focus/dialog, contrast và text overflow được kiểm tra.
- Người review phân biệt được “đã biết”, “đã loại trừ”, “chưa kiểm tra”, “đã ghi” và “đã xác minh”.
- Hai issue UI và proposal cùng kiểm tra các thay đổi domain; không thay đổi feature scope bằng cách thêm button không có contract.
- Người dùng chốt direction sau feedback; vẫn có thể điều chỉnh density/spacing/label, không đóng sớm chỉ vì hình đẹp.

Nếu lấy code từ upstream thay vì tham khảo thiết kế, việc đó phải được ghi riêng cùng file/commit/license. Lần nghiên cứu này chỉ đọc source và xem ảnh; không đưa ảnh/source Aurora vào repo sản phẩm.
